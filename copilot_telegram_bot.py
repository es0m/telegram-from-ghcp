#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "github-copilot-sdk>=0.1.0",
#     "python-telegram-bot>=22.0",
# ]
# ///
"""
Copilot Sessions Telegram Bot

A Telegram bot that acts as an additional "head" for local Copilot CLI sessions.
Uses the Python Copilot SDK to connect to a headless CLI server and forward
session events to a Telegram chat in real time.

Commands:
  /start         — Welcome message and usage help
  /list          — List all sessions (active ones highlighted)
  /active [days] — Show sessions active in the last N days (default: 7)
  /switch <id>   — Connect to a session and subscribe to its events
  /status        — Show current session info
  /send <msg>    — Send a prompt to the current session
  /disconnect    — Disconnect from current session
  /help          — Show available commands

Environment:
  TELEGRAM_BOT_TOKEN   — Required. Your Telegram bot token from @BotFather
  COPILOT_CLI_PATH     — Optional. Path to copilot binary (default: "copilot")
  COPILOT_LOG_LEVEL    — Optional. CLI log level (default: "none")
  ALLOWED_USERNAMES    — Optional. Comma-separated Telegram usernames allowed to use the bot

Config file (JSON):
  Reads from ./copilot_bot_config.json or path set in COPILOT_BOT_CONFIG env var.
  Environment variables take precedence over config file values.
  Example config:
    {
      "telegram_bot_token": "123456:ABC...",
      "allowed_usernames": ["your_telegram_username"],
      "copilot_cli_path": "copilot",
      "copilot_log_level": "none"
    }
"""

import asyncio
import html
import json
import logging
import os
import shutil
import sys
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from copilot import (
    CopilotClient,
    CopilotSession,
    MessageOptions,
    PermissionHandler,
    ResumeSessionConfig,
    SessionListFilter,
    SessionMetadata,
)
from copilot.types import CopilotClientOptions
from copilot.generated.session_events import (
    SessionEvent,
    SessionEventType,
)
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

logging.basicConfig(
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("copilot-telegram")


# ── Helpers ──────────────────────────────────────────────────────────────────

def escape(text: str) -> str:
    """Escape text for Telegram HTML parse mode."""
    return html.escape(text or "")


def truncate(text: str, max_len: int = 4000) -> str:
    """Truncate text to fit Telegram message limits."""
    if not text:
        return ""
    if len(text) <= max_len:
        return text
    return text[: max_len - 20] + "\n\n…(truncated)"


def format_time_ago(iso_time: str) -> str:
    """Convert ISO timestamp to human-readable relative time."""
    try:
        dt = datetime.fromisoformat(iso_time.replace("Z", "+00:00"))
        delta = datetime.now(timezone.utc) - dt
        secs = int(delta.total_seconds())
        if secs < 60:
            return f"{secs}s ago"
        mins = secs // 60
        if mins < 60:
            return f"{mins}m ago"
        hours = mins // 60
        if hours < 24:
            return f"{hours}h ago"
        return f"{hours // 24}d ago"
    except Exception:
        return iso_time[:19] if iso_time else "unknown"


# ── Bot State ────────────────────────────────────────────────────────────────

class BotState:
    """Shared state for the Telegram bot."""

    def __init__(self):
        self.client: Optional[CopilotClient] = None
        self.current_session: Optional[CopilotSession] = None
        self.current_session_id: Optional[str] = None
        self.current_session_meta: Optional[SessionMetadata] = None
        self.unsubscribe_fn: Optional[callable] = None
        # Chat ID where events should be forwarded
        self.event_chat_id: Optional[int] = None
        # Reference to the Telegram application for sending messages
        self.app: Optional[Application] = None
        # Allowed chat IDs (empty = allow all)
        self.allowed_usernames: set[str] = set()
        # Merged config (file + env overrides)
        self.config: dict[str, Any] = {}
        # Track last event type to avoid spamming identical statuses
        self._last_event_type: Optional[str] = None
        # Buffer for streaming deltas
        self._delta_buffer: str = ""
        self._delta_message_id: Optional[int] = None
        # Lock for async operations
        self._lock = asyncio.Lock()
        # Event loop reference for thread-safe callbacks
        self._loop: Optional[asyncio.AbstractEventLoop] = None


state = BotState()


# ── Access Control ───────────────────────────────────────────────────────────

def is_authorized(update: Update) -> bool:
    """Check if the user is authorized to use this bot."""
    if not state.allowed_usernames:
        return True
    user = update.effective_user
    if not user or not user.username:
        return False
    return user.username.lower() in state.allowed_usernames


# ── Copilot Client Management ───────────────────────────────────────────────

async def ensure_client() -> CopilotClient:
    """Ensure the Copilot client is started and connected."""
    if state.client is not None:
        # Check if existing client is still healthy
        try:
            if state.client.get_state() != "connected":
                logger.warning("Client in bad state, restarting...")
                try:
                    await state.client.stop()
                except Exception:
                    pass
                state.client = None
        except Exception:
            state.client = None

    if state.client is None:
        cli_path = state.config.get("copilot_cli_path", "copilot")
        log_level = state.config.get("copilot_log_level", "none")

        # The SDK checks os.path.exists() but doesn't resolve PATH lookups,
        # so we resolve it here.
        resolved = shutil.which(cli_path)
        if resolved:
            cli_path = resolved

        options = CopilotClientOptions(
            cli_path=cli_path,
            log_level=log_level,
            cli_args=["--allow-all"],
        )
        state.client = CopilotClient(options)
        logger.info("Starting Copilot headless server...")
        await state.client.start()
        logger.info("Copilot headless server started")

    return state.client


# ── Event Handler ────────────────────────────────────────────────────────────

def on_session_event(event: SessionEvent):
    """
    Handle a session event from the Copilot SDK.
    This is called from the SDK's event thread, so we schedule
    the async Telegram send on the bot's event loop.
    """
    if state.event_chat_id is None or state.app is None:
        return
    if state._loop is None:
        return

    asyncio.run_coroutine_threadsafe(
        _forward_event(event),
        state._loop,
    )


async def _forward_event(event: SessionEvent):
    """Forward a session event to the Telegram chat."""
    if state.event_chat_id is None or state.app is None:
        return

    chat_id = state.event_chat_id
    bot = state.app.bot
    event_type = event.type.value if hasattr(event.type, "value") else str(event.type)

    # Only play a notification sound for terminal events (idle / task complete / error)
    terminal_events = {
        SessionEventType.SESSION_IDLE.value,
        SessionEventType.SESSION_TASK_COMPLETE.value,
        SessionEventType.SESSION_ERROR.value,
    }
    silent = event_type not in terminal_events

    try:
        msg = _format_event(event, event_type)
        if msg:
            await bot.send_message(
                chat_id=chat_id,
                text=msg,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
                disable_notification=silent,
            )
    except Exception as e:
        logger.error(f"Failed to forward event {event_type}: {e}")


def _format_event(event: SessionEvent, event_type: str) -> Optional[str]:
    """Format a session event as a Telegram HTML message. Returns None to skip."""
    data = event.data

    if event_type == SessionEventType.ASSISTANT_MESSAGE.value:
        content = getattr(data, "content", None) or ""
        if not content.strip():
            return None
        return f"🤖 <b>Assistant</b>\n<pre>{escape(truncate(content))}</pre>"

    elif event_type == SessionEventType.ASSISTANT_INTENT.value:
        intent = getattr(data, "intent", None) or ""
        if intent:
            return f"🎯 <i>{escape(intent)}</i>"
        return None

    elif event_type == SessionEventType.TOOL_EXECUTION_START.value:
        tool_name = getattr(data, "tool_name", None) or getattr(data, "toolName", "") or ""
        return f"🔧 <b>Tool:</b> <code>{escape(tool_name)}</code>"

    elif event_type == SessionEventType.TOOL_EXECUTION_COMPLETE.value:
        tool_name = getattr(data, "tool_name", None) or getattr(data, "toolName", "") or ""
        # Don't send full tool output (can be huge), just confirm completion
        return f"✅ <code>{escape(tool_name)}</code> completed"

    elif event_type == SessionEventType.SESSION_IDLE.value:
        return "⏸️ <i>Session idle — ready for input</i>"

    elif event_type == SessionEventType.SESSION_ERROR.value:
        message = getattr(data, "message", None) or str(data)
        return f"❌ <b>Error:</b> {escape(truncate(message, 500))}"

    elif event_type == SessionEventType.SESSION_WARNING.value:
        message = getattr(data, "message", None) or str(data)
        return f"⚠️ <b>Warning:</b> {escape(truncate(message, 500))}"

    elif event_type == SessionEventType.USER_MESSAGE.value:
        content = getattr(data, "content", None) or getattr(data, "prompt", "") or ""
        if content.strip():
            return f"👤 <b>User</b>\n{escape(truncate(content, 500))}"
        return None

    elif event_type == SessionEventType.SESSION_TASK_COMPLETE.value:
        summary = getattr(data, "summary", None) or ""
        return f"🏁 <b>Task complete</b>\n{escape(truncate(summary, 1000))}"

    elif event_type == SessionEventType.ASSISTANT_TURN_START.value:
        return "⚡ <i>Agent working...</i>"

    elif event_type == SessionEventType.SUBAGENT_STARTED.value:
        description = getattr(data, "description", None) or ""
        return f"🔀 <b>Subagent:</b> {escape(description)}"

    elif event_type == SessionEventType.SUBAGENT_COMPLETED.value:
        return "🔀 Subagent completed"

    # Skip noisy/ephemeral events
    return None


# ── Telegram Command Handlers ───────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start command."""
    if not is_authorized(update):
        return
    await update.message.reply_text(
        "🤖 <b>Copilot Sessions Bot</b>\n\n"
        "I connect to your local Copilot CLI sessions and let you "
        "monitor and interact with them via Telegram.\n\n"
        "<b>Commands:</b>\n"
        "/list — List all sessions\n"
        "/active [days] — Recent sessions (default: 7 days)\n"
        "/switch &lt;id&gt; — Connect to a session\n"
        "/status — Current session info\n"
        "/send &lt;message&gt; — Send a prompt\n"
        "/disconnect — Disconnect from session\n"
        "/help — Show this help",
        parse_mode=ParseMode.HTML,
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /help command."""
    await cmd_start(update, context)


async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /list command — list all sessions."""
    if not is_authorized(update):
        return

    try:
        client = await ensure_client()
        sessions = await client.list_sessions()

        if not sessions:
            await update.message.reply_text("No sessions found.")
            return

        # Sort by modified time descending
        sessions.sort(key=lambda s: s.modifiedTime or "", reverse=True)

        lines = [f"📋 <b>Sessions</b> ({len(sessions)} total)\n"]
        buttons = []
        for i, s in enumerate(sessions[:20]):  # Limit to 20
            sid_short = s.sessionId[:8]
            summary = (s.summary or "—")[:60]
            age = format_time_ago(s.modifiedTime)
            cwd = ""
            if s.context and s.context.cwd:
                cwd = s.context.cwd.split("\\")[-1] or s.context.cwd.split("/")[-1]
                cwd = f" 📁{cwd}"

            marker = "▶️" if s.sessionId == state.current_session_id else "  "
            lines.append(
                f"{marker}<code>{sid_short}</code> {escape(summary)}"
                f"\n    <i>{age}{cwd}</i>"
            )

            # Inline keyboard button to switch to this session
            btn_label = f"{sid_short} — {(s.summary or '—')[:30]}"
            buttons.append([InlineKeyboardButton(btn_label, callback_data=f"switch:{s.sessionId}")])

        if len(sessions) > 20:
            lines.append(f"\n<i>...and {len(sessions) - 20} more</i>")

        keyboard = InlineKeyboardMarkup(buttons) if buttons else None

        await update.message.reply_text(
            "\n".join(lines),
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
        )

    except Exception as e:
        logger.error(f"Error listing sessions: {e}")
        await update.message.reply_text(f"❌ Error: {escape(str(e))}", parse_mode=ParseMode.HTML)


async def cmd_active(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /active [days] — list sessions active within the last N days."""
    if not is_authorized(update):
        return

    days = 7
    if context.args:
        try:
            days = int(context.args[0])
        except ValueError:
            await update.message.reply_text("Usage: /active [days]  (default: 7)")
            return

    try:
        client = await ensure_client()
        sessions = await client.list_sessions()

        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        recent = []
        for s in sessions:
            if s.modifiedTime:
                try:
                    mod = datetime.fromisoformat(s.modifiedTime.replace("Z", "+00:00"))
                    if mod >= cutoff:
                        recent.append(s)
                except Exception:
                    pass

        recent.sort(key=lambda s: s.modifiedTime or "", reverse=True)

        if not recent:
            await update.message.reply_text(f"No sessions active in the last {days} day(s).")
            return

        lines = [f"📋 <b>Active Sessions</b> (last {days}d): {len(recent)}\n"]
        buttons = []
        for s in recent[:20]:
            sid_short = s.sessionId[:8]
            summary = (s.summary or "—")[:60]
            age = format_time_ago(s.modifiedTime)
            cwd = ""
            if s.context and s.context.cwd:
                cwd = s.context.cwd.split("\\")[-1] or s.context.cwd.split("/")[-1]
                cwd = f" 📁{cwd}"

            marker = "▶️" if s.sessionId == state.current_session_id else "  "
            lines.append(
                f"{marker}<code>{sid_short}</code> {escape(summary)}"
                f"\n    <i>{age}{cwd}</i>"
            )

            btn_label = f"{sid_short} — {(s.summary or '—')[:30]}"
            buttons.append([InlineKeyboardButton(btn_label, callback_data=f"switch:{s.sessionId}")])

        if len(recent) > 20:
            lines.append(f"\n<i>...and {len(recent) - 20} more</i>")

        keyboard = InlineKeyboardMarkup(buttons) if buttons else None

        await update.message.reply_text(
            "\n".join(lines),
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
        )

    except Exception as e:
        logger.error(f"Error listing active sessions: {e}")
        await update.message.reply_text(f"❌ Error: {escape(str(e))}", parse_mode=ParseMode.HTML)


def _chdir_to_session(meta) -> None:
    """Change the process working directory to the session's cwd."""
    if meta and meta.context and meta.context.cwd:
        target = meta.context.cwd
        if os.path.isdir(target):
            os.chdir(target)
            logger.info(f"Changed directory to {target}")
        else:
            logger.warning(f"Session cwd does not exist: {target}")


def _repair_session_file(session_id: str) -> bool:
    """Fix known corruption in session events.jsonl (null attachments → []).

    Returns True if a repair was made.
    """
    session_dir = Path.home() / ".copilot" / "session-state" / session_id
    events_file = session_dir / "events.jsonl"
    if not events_file.is_file():
        return False
    try:
        content = events_file.read_text(encoding="utf-8")
        if '"attachments":null' not in content and '"attachments": null' not in content:
            return False
        import re
        fixed = re.sub(r'"attachments"\s*:\s*null', '"attachments":[]', content)
        events_file.write_text(fixed, encoding="utf-8")
        logger.info(f"Repaired corrupted events.jsonl for session {session_id[:8]}")
        return True
    except Exception as e:
        logger.warning(f"Failed to repair session {session_id[:8]}: {e}")
        return False


async def _do_resume(session_id: str, chat_id: int) -> tuple:
    """Shared logic for resuming a session (used by both /switch and button callback).

    Returns (session, meta) on success.
    Raises on failure.
    """
    async with state._lock:
        client = await ensure_client()

        if state.current_session:
            await _disconnect_session()

        # Pre-repair: fix known corruption before attempting resume
        _repair_session_file(session_id)

        config = ResumeSessionConfig(
            on_permission_request=PermissionHandler.approve_all,
        )
        session = await client.resume_session(session_id, config)

        state.current_session = session
        state.current_session_id = session_id
        state.event_chat_id = chat_id
        state.unsubscribe_fn = session.on(on_session_event)

        # Fetch metadata (non-critical)
        meta = None
        try:
            sessions = await client.list_sessions()
            meta = next((s for s in sessions if s.sessionId == session_id), None)
            state.current_session_meta = meta
        except Exception:
            pass

        _chdir_to_session(meta)
        return session, meta


async def cmd_switch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /switch <session_id> — connect to a session."""
    if not is_authorized(update):
        return

    args = context.args
    if not args:
        await update.message.reply_text(
            "Usage: /switch &lt;session_id&gt;\n"
            "Use /list to see available sessions.\n"
            "You can use a prefix (e.g., <code>/switch 698bd767</code>).",
            parse_mode=ParseMode.HTML,
        )
        return

    target_prefix = args[0].lower().strip()

    try:
        client = await ensure_client()

        # Resolve prefix to full session ID
        sessions = await client.list_sessions()
        match = None
        for s in sessions:
            if s.sessionId.lower().startswith(target_prefix):
                if match is not None:
                    await update.message.reply_text(
                        f"❌ Ambiguous prefix <code>{escape(target_prefix)}</code> — "
                        "please provide more characters.",
                        parse_mode=ParseMode.HTML,
                    )
                    return
                match = s

        if match is None:
            await update.message.reply_text(
                f"❌ No session matching <code>{escape(target_prefix)}</code>",
                parse_mode=ParseMode.HTML,
            )
            return

        await update.message.reply_text(
            f"⏳ Connecting to session <code>{match.sessionId[:8]}</code>...",
            parse_mode=ParseMode.HTML,
        )

        _, meta = await _do_resume(match.sessionId, update.effective_chat.id)
        meta = meta or match

        summary = escape((meta.summary if meta else None) or "—")
        cwd = ""
        if meta and meta.context and meta.context.cwd:
            cwd = f"\n📁 <code>{escape(meta.context.cwd)}</code>"

        await update.message.reply_text(
            f"✅ Connected to session <code>{match.sessionId[:8]}</code>\n"
            f"📝 {summary}{cwd}\n\n"
            f"Session events will be forwarded here.\n"
            f"Use /send &lt;message&gt; to interact.",
            parse_mode=ParseMode.HTML,
        )

    except Exception as e:
        logger.error(f"Error switching session: {e}\n{traceback.format_exc()}")
        hint = ""
        if "corrupted" in str(e).lower() or "-32603" in str(e):
            hint = "\n\n💡 The session file may be corrupted. Try a different session."
        await update.message.reply_text(
            f"❌ Error connecting: {escape(str(e))}{hint}",
            parse_mode=ParseMode.HTML,
        )


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /status command — show current session info."""
    if not is_authorized(update):
        return

    if state.current_session is None:
        await update.message.reply_text(
            "No active session. Use /switch to connect to one.",
        )
        return

    try:
        meta = state.current_session_meta
        lines = ["📊 <b>Current Session</b>\n"]
        lines.append(f"<b>ID:</b> <code>{meta.sessionId}</code>")
        lines.append(f"<b>Summary:</b> {escape(meta.summary or '—')}")
        lines.append(f"<b>Modified:</b> {format_time_ago(meta.modifiedTime)}")

        if meta.context:
            if meta.context.cwd:
                lines.append(f"<b>CWD:</b> <code>{escape(meta.context.cwd)}</code>")
            if meta.context.repository:
                lines.append(f"<b>Repo:</b> {escape(meta.context.repository)}")
            if meta.context.branch:
                lines.append(f"<b>Branch:</b> {escape(meta.context.branch)}")

        # Get message history count
        try:
            messages = await state.current_session.get_messages()
            lines.append(f"<b>Events:</b> {len(messages)}")
        except Exception:
            pass

        await update.message.reply_text(
            "\n".join(lines),
            parse_mode=ParseMode.HTML,
        )
    except Exception as e:
        await update.message.reply_text(
            f"❌ Error: {escape(str(e))}",
            parse_mode=ParseMode.HTML,
        )


async def cmd_send(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /send <message> — send a prompt to the current session."""
    if not is_authorized(update):
        return

    if state.current_session is None:
        await update.message.reply_text(
            "No active session. Use /switch to connect first.",
        )
        return

    prompt = " ".join(context.args) if context.args else ""
    if not prompt.strip():
        await update.message.reply_text("Usage: /send &lt;your message&gt;", parse_mode=ParseMode.HTML)
        return

    try:
        await update.message.reply_text(
            f"📤 Sending to session...",
        )
        message_id = await state.current_session.send(MessageOptions(prompt=prompt))
        logger.info(f"Sent message {message_id} to session {state.current_session_id[:8]}")
    except Exception as e:
        logger.error(f"Error sending message: {e}")
        await update.message.reply_text(
            f"❌ Error sending: {escape(str(e))}",
            parse_mode=ParseMode.HTML,
        )


async def cmd_disconnect(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /disconnect command — disconnect from current session."""
    if not is_authorized(update):
        return

    if state.current_session is None:
        await update.message.reply_text("No active session.")
        return

    sid = state.current_session_id[:8] if state.current_session_id else "?"
    await _disconnect_session()
    await update.message.reply_text(
        f"🔌 Disconnected from session <code>{sid}</code>",
        parse_mode=ParseMode.HTML,
    )


async def _disconnect_session():
    """Internal: disconnect from the current session."""
    if state.unsubscribe_fn:
        try:
            state.unsubscribe_fn()
        except Exception:
            pass
        state.unsubscribe_fn = None

    if state.current_session:
        try:
            await state.current_session.disconnect()
        except Exception:
            pass

    state.current_session = None
    state.current_session_id = None
    state.current_session_meta = None
    state.event_chat_id = None


async def callback_switch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle inline keyboard button presses from /list to switch sessions."""
    query = update.callback_query
    if query is None:
        return

    if not is_authorized(update):
        await query.answer("Unauthorized", show_alert=True)
        return

    data = query.data or ""
    if not data.startswith("switch:"):
        await query.answer()
        return

    session_id = data.removeprefix("switch:")
    await query.answer(f"Connecting to {session_id[:8]}...")

    try:
        # If callback data is a short prefix (from older /list messages),
        # resolve it to the full session ID first.
        if len(session_id) < 36:
            client = await ensure_client()
            sessions = await client.list_sessions()
            matches = [s for s in sessions if s.sessionId.lower().startswith(session_id.lower())]
            if len(matches) == 0:
                await query.edit_message_text(
                    f"❌ No session matching <code>{escape(session_id)}</code>",
                    parse_mode=ParseMode.HTML,
                )
                return
            if len(matches) > 1:
                await query.edit_message_text(
                    f"❌ Ambiguous prefix <code>{escape(session_id)}</code>",
                    parse_mode=ParseMode.HTML,
                )
                return
            session_id = matches[0].sessionId

        _, meta = await _do_resume(session_id, query.message.chat_id)

        summary = escape((meta.summary if meta else None) or "—")
        cwd = ""
        if meta and meta.context and meta.context.cwd:
            cwd = f"\n📁 <code>{escape(meta.context.cwd)}</code>"

        await query.edit_message_text(
            f"✅ Connected to session <code>{session_id[:8]}</code>\n"
            f"📝 {summary}{cwd}\n\n"
            f"Session events will be forwarded here.\n"
            f"Use /send &lt;message&gt; to interact.",
            parse_mode=ParseMode.HTML,
        )

    except Exception as e:
        logger.error(f"Error switching via button: {e}\n{traceback.format_exc()}")
        hint = ""
        if "corrupted" in str(e).lower() or "-32603" in str(e):
            hint = "\n\n💡 Session file may be corrupted. Try a different session."
        try:
            await query.edit_message_text(
                f"❌ Error connecting: {escape(str(e))}{hint}",
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle plain text messages — forward to session if connected."""
    if not is_authorized(update):
        return

    if state.current_session is None:
        await update.message.reply_text(
            "No active session. Use /switch to connect, then /send or just type.",
        )
        return

    prompt = update.message.text
    if not prompt or not prompt.strip():
        return

    try:
        message_id = await state.current_session.send(MessageOptions(prompt=prompt))
        logger.info(f"Sent message {message_id} to session {state.current_session_id[:8]}")
        await update.message.reply_text("📤 Sent")
    except Exception as e:
        logger.error(f"Error sending message: {e}")
        await update.message.reply_text(
            f"❌ Error: {escape(str(e))}",
            parse_mode=ParseMode.HTML,
        )


# ── Application Lifecycle ───────────────────────────────────────────────────

async def post_init(application: Application):
    """Called after the application is initialized."""
    state.app = application
    state._loop = asyncio.get_event_loop()
    logger.info("Telegram bot initialized")


async def post_shutdown(application: Application):
    """Called during application shutdown."""
    logger.info("Shutting down...")
    await _disconnect_session()
    if state.client:
        try:
            await state.client.stop()
        except Exception:
            pass
        state.client = None
    logger.info("Shutdown complete")


DEFAULT_CONFIG_PATH = "copilot_bot_config.json"


def load_config() -> dict[str, Any]:
    """Load configuration from JSON file, then overlay environment variables.

    Search order for config file:
      1. COPILOT_BOT_CONFIG env var (explicit path)
      2. ./copilot_bot_config.json (current directory)
      3. ~/.copilot/copilot_bot_config.json (home directory)

    Env vars always take precedence over the config file.
    """
    config: dict[str, Any] = {}

    # Find and load config file
    config_path = os.environ.get("COPILOT_BOT_CONFIG")
    if config_path:
        candidates = [Path(config_path)]
    else:
        candidates = [
            Path(DEFAULT_CONFIG_PATH),
            Path.home() / ".copilot" / DEFAULT_CONFIG_PATH,
        ]

    for path in candidates:
        if path.is_file():
            try:
                config = json.loads(path.read_text(encoding="utf-8"))
                logger.info(f"Loaded config from {path}")
            except (json.JSONDecodeError, OSError) as e:
                print(f"Warning: Failed to read config {path}: {e}", file=sys.stderr)
            break

    # Env vars override config file values
    if env_token := os.environ.get("TELEGRAM_BOT_TOKEN"):
        config["telegram_bot_token"] = env_token
    if env_cli := os.environ.get("COPILOT_CLI_PATH"):
        config["copilot_cli_path"] = env_cli
    if env_log := os.environ.get("COPILOT_LOG_LEVEL"):
        config["copilot_log_level"] = env_log
    if env_users := os.environ.get("ALLOWED_USERNAMES"):
        config["allowed_usernames"] = [x.strip().lstrip("@") for x in env_users.split(",") if x.strip()]

    return config


def main():
    config = load_config()

    token = config.get("telegram_bot_token")
    if not token:
        print("Error: No bot token found.", file=sys.stderr)
        print("Set TELEGRAM_BOT_TOKEN env var or add 'telegram_bot_token' to config file.", file=sys.stderr)
        print(f"Config file locations: ./{DEFAULT_CONFIG_PATH} or ~/.copilot/{DEFAULT_CONFIG_PATH}", file=sys.stderr)
        sys.exit(1)

    # Store config for use by ensure_client()
    state.config = config

    # Parse allowed chat IDs
    allowed = config.get("allowed_usernames", [])
    if allowed:
        state.allowed_usernames = {u.lower().lstrip("@") for u in allowed}
        logger.info(f"Access restricted to usernames: {state.allowed_usernames}")

    # Build Telegram application
    app = Application.builder().token(token).post_init(post_init).post_shutdown(post_shutdown).build()

    # Register command handlers
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("active", cmd_active))
    app.add_handler(CommandHandler("switch", cmd_switch))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("send", cmd_send))
    app.add_handler(CommandHandler("disconnect", cmd_disconnect))

    # Handle inline keyboard button presses (e.g., session switch from /list)
    app.add_handler(CallbackQueryHandler(callback_switch, pattern=r"^switch:"))

    # Handle plain text messages as prompts when connected
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    logger.info("Starting Copilot Sessions Telegram Bot...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
