# Copilot Session Tools

Tools for enumerating and interacting with local [GitHub Copilot CLI](https://githubnext.com/projects/copilot-cli) sessions from the command line or via Telegram.

## Prerequisites

- Python 3.11+
- [Copilot CLI](https://github.com/github/copilot-cli) installed and authenticated (`copilot auth login`)

### Install dependencies

**With [uv](https://docs.astral.sh/uv/) (recommended)** — no install step needed. Both scripts include [PEP 723](https://peps.python.org/pep-0723/) inline metadata, so `uv run` handles everything:

```bash
uv run copilot_telegram_bot.py
uv run copilot_sessions.py --active
```

**With pip:**

```bash
pip install -r requirements.txt
python copilot_telegram_bot.py
```

## Scripts

### `copilot_sessions.py` — Session Enumeration

Lists all Copilot CLI sessions on your device by scanning the local session state directory, SQLite database, and optionally querying the SDK.

```bash
# List all sessions
python copilot_sessions.py

# Only active (currently connected) sessions
python copilot_sessions.py --active

# JSON output
python copilot_sessions.py --json

# Verbose (full session IDs, extra details)
python copilot_sessions.py --verbose

# Query via headless SDK server (richer metadata)
python copilot_sessions.py --use-sdk

# SDK only (skip filesystem/DB scan)
python copilot_sessions.py --sdk-only
```

**Data sources:**
1. `~/.copilot/session-state/*/workspace.yaml` — session metadata
2. `~/.copilot/session-state/*/inuse.*.lock` — active session detection (PID cross-reference)
3. `~/.copilot/session-store.db` — historical session data (SQLite)
4. Copilot SDK `session.list` RPC via headless server (`--use-sdk`)

### `copilot_telegram_bot.py` — Telegram Bot

A Telegram bot that connects to local Copilot sessions as an additional "head" using the [Python Copilot SDK](https://pypi.org/project/github-copilot-sdk/). Monitor and interact with sessions remotely.

**Commands:**

| Command | Description |
|---|---|
| `/list` | List all sessions with inline switch buttons |
| `/active [days]` | Sessions active in the last N days (default: 7) |
| `/switch <id>` | Connect to a session (prefix match, e.g. `698bd767`) |
| `/status` | Show current session info |
| `/send <message>` | Send a prompt to the connected session |
| `/disconnect` | Disconnect from current session |

Plain text messages are forwarded as prompts when connected to a session.

**Event streaming:** When connected, the bot forwards session events to your Telegram chat in real time — assistant messages, tool executions, intent updates, errors, and more.

## Configuration

The bot reads configuration from a JSON file and/or environment variables. **Environment variables always take precedence.**

### Config file

Create `copilot_bot_config.json` in the working directory or `~/.copilot/copilot_bot_config.json`:

```json
{
  "telegram_bot_token": "123456:ABC-DEF...",
  "allowed_usernames": ["your_telegram_username"],
  "copilot_cli_path": "copilot",
  "copilot_log_level": "none"
}
```

| Field | Required | Description |
|---|---|---|
| `telegram_bot_token` | Yes | Bot token from [@BotFather](https://t.me/BotFather) |
| `allowed_usernames` | No | Telegram usernames allowed to use the bot. Empty = no restriction. |
| `copilot_cli_path` | No | Path to copilot binary (default: `copilot`) |
| `copilot_log_level` | No | CLI log level: `none`, `error`, `warn`, `info`, `debug` (default: `none`) |

Config file search order:
1. `$COPILOT_BOT_CONFIG` (explicit path via env var)
2. `./copilot_bot_config.json`
3. `~/.copilot/copilot_bot_config.json`

### Environment variables

| Variable | Description |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Bot token (overrides config file) |
| `ALLOWED_USERNAMES` | Comma-separated usernames (overrides config file) |
| `COPILOT_CLI_PATH` | Path to copilot binary |
| `COPILOT_LOG_LEVEL` | CLI log level |
| `COPILOT_BOT_CONFIG` | Custom path to config JSON file |

## Quick Start

1. **Create a Telegram bot** — message [@BotFather](https://t.me/BotFather), send `/newbot`, copy the token.

2. **Create config file:**
   ```json
   {
     "telegram_bot_token": "YOUR_TOKEN_HERE",
     "allowed_usernames": ["your_username"]
   }
   ```

3. **Run the bot:**
   ```bash
   uv run copilot_telegram_bot.py
   # or: python copilot_telegram_bot.py
   ```

4. **Open your bot in Telegram** and send `/active` to see recent sessions, then tap one to connect.

## Architecture

```
Telegram ←→ python-telegram-bot ←→ CopilotClient (Python SDK)
                                        ↕
                                  copilot --headless (server process)
                                        ↕
                                  ~/.copilot/session-state/ (on disk)
```

The bot starts a headless Copilot CLI server via the Python SDK. The SDK communicates with the server over stdio using the JSON-RPC protocol (vscode-jsonrpc framing). Session data is stored on disk by the CLI — the bot connects as an additional client head, reading events and sending prompts alongside any existing CLI sessions.

## Notes

- The bot auto-repairs a known Copilot CLI bug where `"attachments": null` in session event files causes resume failures.
- Session switching changes the bot process's working directory to match the session's `cwd`.
- The `.gitignore` excludes `copilot_bot_config.json` to prevent committing your bot token.
