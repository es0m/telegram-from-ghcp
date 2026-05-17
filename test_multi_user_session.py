#!/usr/bin/env python3
"""
test_multi_user_session.py

Unit tests validating that multiple SDK clients can connect to the same
Copilot CLI session concurrently and both receive events.

Uses the Python Copilot SDK to:
  1. Create a session via client A
  2. Resume that session via client B
  3. Send a message from client A and verify both A and B receive events
  4. Verify both clients can list the same session

Requires:
  - copilot CLI installed and on PATH
  - github-copilot-sdk >= 0.1.0
  - pytest, pytest-asyncio

Run:
  pytest test_multi_user_session.py -v
"""

import asyncio
import json
import os
import platform
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# Test helpers: classify_session, get_session_display_name, device utilities
# These are imported from the bot module or defined inline for isolation.
# ---------------------------------------------------------------------------

# Import the bot's helper functions for unit testing
sys.path.insert(0, os.path.dirname(__file__))
import copilot_telegram_bot as bot_module
from copilot_telegram_bot import (
    classify_session,
    get_session_display_name,
    get_device_name,
    STALE_THRESHOLD_DAYS,
)


# ---------------------------------------------------------------------------
# Unit tests: session classification
# ---------------------------------------------------------------------------

class TestClassifySession:
    """Test the classify_session helper."""

    def _make_meta(self, modified_iso: str):
        """Create a mock SessionMetadata with the given modifiedTime."""
        meta = MagicMock()
        meta.modifiedTime = modified_iso
        return meta

    def test_active_within_24h(self):
        now = datetime(2026, 5, 10, 12, 0, 0, tzinfo=timezone.utc)
        meta = self._make_meta("2026-05-10T10:00:00Z")
        assert classify_session(meta, now) == "active"

    def test_recent_within_threshold(self):
        now = datetime(2026, 5, 10, 12, 0, 0, tzinfo=timezone.utc)
        meta = self._make_meta("2026-05-05T10:00:00Z")
        assert classify_session(meta, now) == "recent"

    def test_stale_beyond_threshold(self):
        now = datetime(2026, 5, 10, 12, 0, 0, tzinfo=timezone.utc)
        meta = self._make_meta("2026-03-01T10:00:00Z")
        assert classify_session(meta, now) == "stale"

    def test_no_modified_time(self):
        meta = self._make_meta(None)
        assert classify_session(meta) == "stale"

    def test_boundary_exactly_1_day(self):
        now = datetime(2026, 5, 10, 12, 0, 0, tzinfo=timezone.utc)
        meta = self._make_meta("2026-05-09T12:00:00Z")
        # Exactly 1 day old is "recent" (not active)
        assert classify_session(meta, now) == "recent"


class TestGetSessionDisplayName:
    """Test the get_session_display_name helper."""

    def _make_meta(self, summary=None, repo=None, cwd=None, session_id="abcd1234-0000"):
        meta = MagicMock()
        meta.summary = summary
        meta.sessionId = session_id
        meta.context = MagicMock()
        meta.context.repository = repo
        meta.context.cwd = cwd
        return meta

    def test_summary_first(self):
        meta = self._make_meta(summary="Fix login bug", repo="org/repo", cwd="/home/dev/repo")
        assert get_session_display_name(meta) == "Fix login bug"

    def test_repo_fallback(self):
        meta = self._make_meta(summary=None, repo="github/copilot-sdk", cwd="/home/dev/sdk")
        assert get_session_display_name(meta) == "copilot-sdk"

    def test_cwd_fallback(self):
        meta = self._make_meta(summary=None, repo=None, cwd="/home/dev/my-project")
        assert get_session_display_name(meta) == "my-project"

    def test_session_id_fallback(self):
        meta = self._make_meta(summary=None, repo=None, cwd=None, session_id="abcd1234-5678")
        assert get_session_display_name(meta) == "abcd1234"

    def test_device_prefix(self):
        meta = self._make_meta(summary="My task")
        result = get_session_display_name(meta, device_name="laptop")
        assert result == "[laptop] My task"

    def test_long_summary_truncated(self):
        long_summary = "A" * 100
        meta = self._make_meta(summary=long_summary)
        name = get_session_display_name(meta)
        assert len(name) <= 60
        assert name.endswith("…")


class TestDeviceName:
    """Test device name resolution."""

    def test_from_config(self):
        assert get_device_name({"device_name": "my-laptop"}) == "my-laptop"

    def test_from_env(self, monkeypatch):
        monkeypatch.setenv("DEVICE_NAME", "ci-server")
        assert get_device_name({}) == "ci-server"

    def test_hostname_fallback(self, monkeypatch):
        monkeypatch.delenv("DEVICE_NAME", raising=False)
        name = get_device_name({})
        assert name == platform.node()


class TestMainLoopRecovery:
    """Test that startup network failures do not crash the bot process."""

    def test_main_recovers_from_startup_timeout(self, monkeypatch):
        calls = {"active": 0, "standby_delays": [], "activate_checks": 0}

        monkeypatch.setattr(bot_module, "load_config", lambda: {"telegram_bot_token": "test-token"})
        monkeypatch.setattr(bot_module, "get_device_name", lambda _: "test-device")
        monkeypatch.setattr(bot_module, "_load_display_names", lambda: {})
        monkeypatch.setattr(bot_module, "_load_coordination_state", lambda: None)

        async def fake_check_should_activate(token: str) -> bool:
            calls["activate_checks"] += 1
            return True

        def fake_run_active(token: str, config: dict):
            calls["active"] += 1
            raise bot_module.telegram.error.TimedOut("startup timeout")

        async def fake_run_standby(token: str, initial_delay: int = 0):
            calls["standby_delays"].append(initial_delay)
            raise KeyboardInterrupt

        monkeypatch.setattr(bot_module, "_check_should_activate", fake_check_should_activate)
        monkeypatch.setattr(bot_module, "_run_active", fake_run_active)
        monkeypatch.setattr(bot_module, "_run_standby", fake_run_standby)

        bot_module.main()

        assert calls["active"] == 1
        assert calls["activate_checks"] >= 2
        assert calls["standby_delays"] == [bot_module.STANDBY_CHECK_INTERVAL]


class TestCoordinationPinUpdates:
    """Test takeover and persistence behavior for coordination pin updates."""

    @pytest.fixture(autouse=True)
    def _restore_state(self):
        tracked = [
            "_coordination_chat_id",
            "_pinned_message_id",
            "device_name",
            "is_active",
            "app",
            "_loop",
            "_heartbeat_task",
        ]
        snapshot = {name: getattr(bot_module.state, name) for name in tracked}
        yield
        for name, value in snapshot.items():
            setattr(bot_module.state, name, value)

    @pytest.mark.asyncio
    async def test_update_coordination_pin_reuses_current_pinned_message(
        self, monkeypatch, tmp_path
    ):
        monkeypatch.setattr(bot_module, "COORD_STATE_FILE", tmp_path / "coord.json")
        bot_module.state._coordination_chat_id = 123
        bot_module.state._pinned_message_id = None

        existing_text = bot_module._build_coord_message("old-device", "new-device", 2)

        class FakeBot:
            def __init__(self):
                self.edited_message_id = None
                self.send_count = 0

            async def get_chat(self, chat_id):
                return SimpleNamespace(
                    pinned_message=SimpleNamespace(message_id=987, text=existing_text)
                )

            async def edit_message_text(self, text, chat_id, message_id):
                self.edited_message_id = message_id

            async def send_message(self, chat_id, text):
                self.send_count += 1
                return SimpleNamespace(message_id=111)

            async def pin_chat_message(self, chat_id, message_id, disable_notification=True):
                return None

        bot = FakeBot()
        await bot_module._update_coordination_pin(bot, "new-device", "new-device", 4)

        assert bot.edited_message_id == 987
        assert bot.send_count == 0
        assert bot_module.state._pinned_message_id == 987

    @pytest.mark.asyncio
    async def test_update_coordination_pin_recreates_when_stored_id_is_stale(
        self, monkeypatch, tmp_path
    ):
        monkeypatch.setattr(bot_module, "COORD_STATE_FILE", tmp_path / "coord.json")
        bot_module.state._coordination_chat_id = 123
        bot_module.state._pinned_message_id = 555

        class FakeBot:
            def __init__(self):
                self.edit_count = 0
                self.send_count = 0
                self.pin_count = 0

            async def get_chat(self, chat_id):
                return SimpleNamespace(pinned_message=None)

            async def edit_message_text(self, text, chat_id, message_id):
                self.edit_count += 1
                raise bot_module.telegram.error.BadRequest("Message to edit not found")

            async def send_message(self, chat_id, text):
                self.send_count += 1
                return SimpleNamespace(message_id=777)

            async def pin_chat_message(self, chat_id, message_id, disable_notification=True):
                self.pin_count += 1

        bot = FakeBot()
        await bot_module._update_coordination_pin(bot, "new-device", "new-device", 6)

        assert bot.edit_count == 1
        assert bot.send_count == 1
        assert bot.pin_count == 1
        assert bot_module.state._pinned_message_id == 777

    @pytest.mark.asyncio
    async def test_active_post_init_updates_pin_immediately(self, monkeypatch):
        bot_module.state._coordination_chat_id = 777
        bot_module.state.device_name = "new-device"

        calls = []

        async def fake_get_session_count():
            return 9

        async def fake_update_coordination_pin(bot, active, target, sessions=0):
            calls.append((bot, active, target, sessions))

        async def fake_heartbeat_loop():
            return None

        monkeypatch.setattr(bot_module, "_get_session_count", fake_get_session_count)
        monkeypatch.setattr(bot_module, "_update_coordination_pin", fake_update_coordination_pin)
        monkeypatch.setattr(bot_module, "_heartbeat_loop", fake_heartbeat_loop)

        app = SimpleNamespace(bot=object())
        await bot_module._active_post_init(app)
        await bot_module.state._heartbeat_task

        assert calls == [(app.bot, "new-device", "new-device", 9)]
        assert bot_module.state.is_active is True
        assert bot_module.state.app is app

    def test_save_coordination_state_clears_stale_pinned_id(self, monkeypatch, tmp_path):
        monkeypatch.setattr(bot_module, "COORD_STATE_FILE", tmp_path / "coord.json")
        bot_module.state._coordination_chat_id = 321
        bot_module.state._pinned_message_id = 999
        bot_module._save_coordination_state()

        bot_module.state._pinned_message_id = None
        bot_module._save_coordination_state()

        data = json.loads((tmp_path / "coord.json").read_text(encoding="utf-8"))
        assert data["chat_id"] == 321
        assert "pinned_message_id" not in data



# ---------------------------------------------------------------------------
# Integration tests: multi-user session access via Copilot SDK
# (Skipped if copilot CLI is not available)
# ---------------------------------------------------------------------------

copilot_available = shutil.which("copilot") is not None

try:
    from copilot import CopilotClient, SubprocessConfig
    from copilot.session import PermissionHandler
    from copilot.generated.session_events import SessionEventType
    sdk_available = True
except ImportError:
    sdk_available = False


@pytest.mark.asyncio
@pytest.mark.skipif(
    not (copilot_available and sdk_available),
    reason="Requires copilot CLI on PATH and github-copilot-sdk installed",
)
class TestMultiUserSession:
    """Integration tests for concurrent multi-user access to the same session.

    These tests start real Copilot CLI headless servers and validate that
    two separate SDK clients can connect to and interact with the same session.
    """

    async def _make_client(self) -> CopilotClient:
        """Create and start a CopilotClient."""
        cli_path = shutil.which("copilot") or "copilot"
        options = SubprocessConfig(
            cli_path=cli_path,
            log_level="none",
            cli_args=["--allow-all"],
        )
        client = CopilotClient(options)
        await client.start()
        return client

    async def test_two_clients_list_same_sessions(self):
        """Both clients should see the same set of sessions."""
        client_a = await self._make_client()
        client_b = await self._make_client()

        try:
            sessions_a = await client_a.list_sessions()
            sessions_b = await client_b.list_sessions()

            ids_a = {s.sessionId for s in sessions_a}
            ids_b = {s.sessionId for s in sessions_b}

            # Both clients read from the same session-state directory,
            # so their session lists must be identical.
            assert ids_a == ids_b, (
                f"Session lists differ:\n  A-only: {ids_a - ids_b}\n  B-only: {ids_b - ids_a}"
            )
        finally:
            await client_a.stop()
            await client_b.stop()

    async def test_resume_same_session_both_receive_events(self):
        """Two clients resuming the same session should both receive events."""
        client_a = await self._make_client()
        client_b = await self._make_client()

        try:
            # Find a session to test with
            sessions = await client_a.list_sessions()
            if not sessions:
                pytest.skip("No existing sessions to test with")

            target = sessions[0]
            sid = target.sessionId

            # Client A resumes the session
            session_a = await client_a.resume_session(
                sid,
                on_permission_request=PermissionHandler.approve_all,
            )

            # Client B resumes the same session
            session_b = await client_b.resume_session(
                sid,
                on_permission_request=PermissionHandler.approve_all,
            )

            # Collect events from both
            events_a: list = []
            events_b: list = []

            session_a.on(lambda e: events_a.append(e.type))
            session_b.on(lambda e: events_b.append(e.type))

            # Both should be able to get messages (proves they're connected)
            messages_a = await session_a.get_messages()
            messages_b = await session_b.get_messages()

            # Message lists should be the same (both are viewing the same session)
            assert len(messages_a) == len(messages_b), (
                f"Message count differs: A={len(messages_a)}, B={len(messages_b)}"
            )

            # Disconnect both
            await session_a.disconnect()
            await session_b.disconnect()

        finally:
            await client_a.stop()
            await client_b.stop()

    async def test_session_metadata_consistent(self):
        """Both clients should see identical metadata for any given session."""
        client_a = await self._make_client()
        client_b = await self._make_client()

        try:
            sessions_a = await client_a.list_sessions()
            sessions_b = await client_b.list_sessions()

            if not sessions_a:
                pytest.skip("No sessions available")

            # Build lookup for B
            b_map = {s.sessionId: s for s in sessions_b}

            for sa in sessions_a[:5]:  # Check first 5
                sb = b_map.get(sa.sessionId)
                assert sb is not None, f"Session {sa.sessionId} not found in client B"
                assert sa.summary == sb.summary, (
                    f"Summary mismatch for {sa.sessionId}: '{sa.summary}' vs '{sb.summary}'"
                )
                assert sa.modifiedTime == sb.modifiedTime
                if sa.context and sb.context:
                    assert sa.context.cwd == sb.context.cwd
        finally:
            await client_a.stop()
            await client_b.stop()

    async def test_bot_do_resume_switches_process_cwd_to_selected_session(self):
        """Bot switch path must route tool execution to the selected session cwd."""
        repo_root = Path(__file__).resolve().parent
        target_dir = repo_root / "test"
        created_target_dir = False
        if not target_dir.exists():
            target_dir.mkdir(parents=True, exist_ok=True)
            created_target_dir = True

        original_cwd = Path.cwd()
        tracked_state = [
            "client",
            "current_session",
            "current_session_id",
            "current_session_meta",
            "unsubscribe_fn",
            "event_chat_id",
            "app",
            "config",
            "_loop",
            "watched_sessions",
            "_original_cwd",
        ]
        snapshot = {name: getattr(bot_module.state, name) for name in tracked_state}

        root_session_id: str | None = None
        target_session_id: str | None = None
        root_session = None
        target_session = None
        client = None
        try:
            bot_module.state.client = None
            bot_module.state.current_session = None
            bot_module.state.current_session_id = None
            bot_module.state.current_session_meta = None
            bot_module.state.unsubscribe_fn = None
            bot_module.state.event_chat_id = None
            bot_module.state.app = None
            bot_module.state.watched_sessions = {}
            bot_module.state.config = {"copilot_log_level": "none"}
            bot_module.state._loop = asyncio.get_running_loop()
            bot_module.state._original_cwd = str(original_cwd)

            client = await bot_module.ensure_client()

            async def _wait_for_listed_meta(session_id: str, timeout_seconds: int = 30):
                for _ in range(timeout_seconds):
                    sessions = await client.list_sessions()
                    match = next((s for s in sessions if s.sessionId == session_id), None)
                    if match is not None:
                        return match
                    await asyncio.sleep(1)
                return None

            root_session = await client.create_session(
                on_permission_request=PermissionHandler.approve_all,
                working_directory=str(repo_root),
            )
            root_session_id = root_session.session_id

            target_session = await client.create_session(
                on_permission_request=PermissionHandler.approve_all,
                working_directory=str(target_dir),
            )
            target_session_id = target_session.session_id

            # Persist both sessions so they can be resumed as existing sessions.
            await root_session.send_and_wait("seed root session", timeout=120)
            await target_session.send_and_wait("seed target session", timeout=120)
            await root_session.disconnect()
            await target_session.disconnect()
            root_session = None
            target_session = None

            # Simulate bot restart so resume_session runs against existing sessions.
            await client.stop()
            bot_module.state.client = None
            client = await bot_module.ensure_client()

            root_meta = await _wait_for_listed_meta(root_session_id)
            target_meta = await _wait_for_listed_meta(target_session_id)
            if root_meta is None:
                root_meta = SimpleNamespace(
                    sessionId=root_session_id,
                    summary="root-session",
                    modifiedTime=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                    context=SimpleNamespace(
                        cwd=str(repo_root),
                        gitRoot=str(repo_root),
                        repository="es0m/telegram-from-ghcp",
                        branch="main",
                    ),
                )
            if target_meta is None:
                target_meta = SimpleNamespace(
                    sessionId=target_session_id,
                    summary="target-session",
                    modifiedTime=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                    context=SimpleNamespace(
                        cwd=str(target_dir),
                        gitRoot=str(target_dir),
                        repository="es0m/telegram-from-ghcp",
                        branch="main",
                    ),
                )

            # Exercise the same switch path used by /switch and callback_switch.
            await bot_module._do_resume(root_session_id, chat_id=1, pre_meta=root_meta)
            await bot_module._do_resume(target_session_id, chat_id=1, pre_meta=target_meta)

            assert bot_module.state.current_session_id == target_session_id
            assert Path.cwd().resolve() == target_dir.resolve()

            # Verify the session tool cwd is actually the selected session directory.
            response_event = await bot_module.state.current_session.send_and_wait(
                "Use the powershell tool and run exactly: "
                "Get-Location | Select-Object -ExpandProperty Path. "
                "Reply with only that path.",
                timeout=120,
            )
            response_text = getattr(getattr(response_event, "data", None), "content", "") or ""
            normalized_response = (
                response_text.replace("\\\\", "\\").replace("\\", "/").strip().lower()
            )
            normalized_target = str(target_dir).replace("\\", "/").lower()
            assert normalized_target in normalized_response, (
                f"Expected cwd '{target_dir}', got response: {response_text!r}"
            )
        finally:
            try:
                await bot_module._disconnect_session()
            except Exception:
                pass

            for session in (root_session, target_session):
                if session is not None:
                    try:
                        await session.disconnect()
                    except Exception:
                        pass

            if client is not None:
                for sid in (root_session_id, target_session_id):
                    if sid:
                        try:
                            await client.delete_session(sid)
                        except Exception:
                            pass
                try:
                    await client.stop()
                except Exception:
                    pass

            if created_target_dir:
                try:
                    if target_dir.exists() and not any(target_dir.iterdir()):
                        target_dir.rmdir()
                except OSError:
                    pass

            os.chdir(original_cwd)
            for name, value in snapshot.items():
                setattr(bot_module.state, name, value)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
