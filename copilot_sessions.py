#!/usr/bin/env python3
"""
copilot-sessions: Enumerate all Copilot CLI sessions on this device.

Combines up to four data sources:
  1. ~/.copilot/session-state/*/workspace.yaml  (session metadata)
  2. ~/.copilot/session-state/*/inuse.*.lock    (active session detection)
  3. ~/.copilot/session-store.db                (historical session data)
  4. Copilot SDK session.list RPC via headless server (--use-sdk mode)

Usage:
  python copilot_sessions.py              # list all sessions (filesystem + DB)
  python copilot_sessions.py --active     # only active/connected sessions
  python copilot_sessions.py --json       # JSON output
  python copilot_sessions.py --verbose    # include extra details
  python copilot_sessions.py --use-sdk    # start headless server, query via RPC
"""

import argparse
import json
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


@dataclass
class SessionInfo:
    session_id: str
    cwd: Optional[str] = None
    git_root: Optional[str] = None
    repository: Optional[str] = None
    branch: Optional[str] = None
    summary: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    state: str = "inactive"  # active, inactive, stale
    owner_pids: list = field(default_factory=list)
    source: str = "filesystem"  # filesystem, database, both
    turn_count: int = 0


def get_copilot_dir() -> Path:
    return Path.home() / ".copilot"


def get_running_copilot_pids() -> set:
    """Get PIDs of all running copilot processes."""
    pids = set()
    try:
        if sys.platform == "win32":
            import subprocess
            result = subprocess.run(
                ["powershell", "-Command",
                 "Get-Process -Name copilot -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Id"],
                capture_output=True, text=True, timeout=5
            )
            for line in result.stdout.strip().splitlines():
                line = line.strip()
                if line.isdigit():
                    pids.add(int(line))
        else:
            import subprocess
            result = subprocess.run(
                ["pgrep", "-x", "copilot"],
                capture_output=True, text=True, timeout=5
            )
            for line in result.stdout.strip().splitlines():
                line = line.strip()
                if line.isdigit():
                    pids.add(int(line))
    except Exception:
        pass
    return pids


def parse_workspace_yaml(path: Path) -> dict:
    """Parse a workspace.yaml file (simple key-value YAML)."""
    data = {}
    try:
        content = path.read_text(encoding="utf-8")
        for line in content.splitlines():
            match = re.match(r'^(\w+):\s*(.*)', line)
            if match:
                key, value = match.group(1), match.group(2).strip()
                # Strip surrounding quotes
                if value.startswith('"') and value.endswith('"'):
                    value = value[1:-1]
                if value == "":
                    value = None
                data[key] = value
    except Exception:
        pass
    return data


def detect_lock_files(session_dir: Path) -> list:
    """Find inuse lock files and return (server_pid, client_pid) tuples."""
    locks = []
    try:
        for lock_file in session_dir.glob("inuse.*.lock"):
            match = re.search(r'inuse\.(\d+)\.lock', lock_file.name)
            if match:
                server_pid = int(match.group(1))
                try:
                    client_pid = int(lock_file.read_text().strip())
                except (ValueError, OSError):
                    client_pid = None
                locks.append((server_pid, client_pid))
    except Exception:
        pass
    return locks


def scan_filesystem(copilot_dir: Path, running_pids: set) -> dict:
    """Scan session-state directories for session metadata."""
    sessions = {}
    session_state_dir = copilot_dir / "session-state"
    if not session_state_dir.exists():
        return sessions

    for session_dir in session_state_dir.iterdir():
        if not session_dir.is_dir():
            continue

        session_id = session_dir.name
        workspace_yaml = session_dir / "workspace.yaml"
        if not workspace_yaml.exists():
            continue

        meta = parse_workspace_yaml(workspace_yaml)
        info = SessionInfo(
            session_id=session_id,
            cwd=meta.get("cwd"),
            git_root=meta.get("git_root"),
            repository=meta.get("repository"),
            branch=meta.get("branch"),
            summary=meta.get("summary"),
            created_at=meta.get("created_at"),
            updated_at=meta.get("updated_at"),
            source="filesystem",
        )

        # Check lock files for active status
        locks = detect_lock_files(session_dir)
        if locks:
            active_pids = []
            for server_pid, client_pid in locks:
                if server_pid in running_pids or (client_pid and client_pid in running_pids):
                    active_pids.extend(
                        p for p in [server_pid, client_pid]
                        if p and p in running_pids
                    )
            if active_pids:
                info.state = "active"
                info.owner_pids = list(set(active_pids))
            else:
                info.state = "stale"  # lock file exists but process is dead

        sessions[session_id] = info
    return sessions


def query_database(copilot_dir: Path) -> dict:
    """Query session-store.db for session data."""
    sessions = {}
    db_path = copilot_dir / "session-store.db"
    if not db_path.exists():
        return sessions

    try:
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        cursor.execute("""
            SELECT s.id, s.cwd, s.repository, s.branch, s.summary,
                   s.created_at, s.updated_at,
                   COUNT(t.id) as turn_count
            FROM sessions s
            LEFT JOIN turns t ON t.session_id = s.id
            GROUP BY s.id
            ORDER BY s.updated_at DESC
        """)

        for row in cursor.fetchall():
            info = SessionInfo(
                session_id=row["id"],
                cwd=row["cwd"],
                repository=row["repository"],
                branch=row["branch"],
                summary=row["summary"],
                created_at=row["created_at"],
                updated_at=row["updated_at"],
                source="database",
                turn_count=row["turn_count"],
            )
            sessions[row["id"]] = info

        conn.close()
    except Exception as e:
        print(f"Warning: Could not read session-store.db: {e}", file=sys.stderr)

    return sessions


# ---------------------------------------------------------------------------
# Approach D: Copilot SDK headless server + session.list JSON-RPC
# ---------------------------------------------------------------------------

class CopilotHeadlessServer:
    """
    Manages the lifecycle of a Copilot CLI process running in headless/server mode.
    Communicates using the vscode-jsonrpc protocol (Content-Length framed JSON-RPC)
    over a TCP socket — the same protocol used by the official Copilot SDKs.
    """

    def __init__(self, cli_path: str = "copilot", log_level: str = "none"):
        self.cli_path = cli_path
        self.log_level = log_level
        self._process: Optional[subprocess.Popen] = None
        self._port: Optional[int] = None
        self._socket: Optional[socket.socket] = None
        self._rpc_id = 0

    @property
    def port(self) -> Optional[int]:
        return self._port

    def start(self, timeout: float = 15.0) -> int:
        """Start the headless CLI server and return the port it's listening on."""
        args = [
            self.cli_path,
            "--headless",
            "--port", "0",           # auto-select available port
            "--no-auto-update",
            "--log-level", self.log_level,
        ]

        self._process = subprocess.Popen(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            text=True,
        )

        # Read stdout until we see the port announcement
        deadline = time.monotonic() + timeout
        port_pattern = re.compile(r"listening on port (\d+)", re.IGNORECASE)

        while time.monotonic() < deadline:
            if self._process.poll() is not None:
                stderr = self._process.stderr.read() if self._process.stderr else ""
                raise RuntimeError(
                    f"Copilot CLI exited unexpectedly (code {self._process.returncode}): {stderr}"
                )
            line = self._process.stdout.readline()
            if not line:
                time.sleep(0.1)
                continue
            m = port_pattern.search(line)
            if m:
                self._port = int(m.group(1))
                return self._port

        self.stop()
        raise TimeoutError(f"Copilot CLI did not announce port within {timeout}s")

    def connect(self, timeout: float = 5.0):
        """Connect to the headless server via TCP."""
        if self._port is None:
            raise RuntimeError("Server not started — call start() first")
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._socket.settimeout(timeout)
        self._socket.connect(("localhost", self._port))

    def _send_rpc(self, method: str, params: dict) -> dict:
        """Send a JSON-RPC request and return the response (vscode-jsonrpc framing)."""
        if self._socket is None:
            raise RuntimeError("Not connected — call connect() first")

        self._rpc_id += 1
        request = {
            "jsonrpc": "2.0",
            "id": self._rpc_id,
            "method": method,
            "params": [params],
        }
        body = json.dumps(request).encode("utf-8")
        header = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
        self._socket.sendall(header + body)

        return self._read_rpc_response()

    def _read_rpc_response(self, timeout: float = 30.0) -> dict:
        """Read a single JSON-RPC response with Content-Length framing."""
        self._socket.settimeout(timeout)
        buf = b""

        # Read headers until we find Content-Length and the blank line separator
        content_length = None
        while True:
            chunk = self._socket.recv(1)
            if not chunk:
                raise ConnectionError("Server closed connection")
            buf += chunk
            # Look for end of headers
            if buf.endswith(b"\r\n\r\n"):
                for line in buf.decode("ascii").split("\r\n"):
                    if line.lower().startswith("content-length:"):
                        content_length = int(line.split(":", 1)[1].strip())
                        break
                if content_length is not None:
                    break
                # If we got a double CRLF but no Content-Length, this might be
                # a notification — reset and keep reading
                buf = b""

        # Read the body
        body = b""
        while len(body) < content_length:
            remaining = content_length - len(body)
            chunk = self._socket.recv(remaining)
            if not chunk:
                raise ConnectionError("Server closed connection while reading body")
            body += chunk

        return json.loads(body.decode("utf-8"))

    def list_sessions(self, filter_opts: Optional[dict] = None) -> list:
        """Call session.list RPC and return a list of SessionMetadata dicts."""
        params = {}
        if filter_opts:
            params["filter"] = filter_opts
        response = self._send_rpc("session.list", params)

        if "error" in response:
            raise RuntimeError(f"RPC error: {response['error']}")

        result = response.get("result", {})
        return result.get("sessions", [])

    def stop(self):
        """Shut down the headless server and clean up."""
        if self._socket:
            try:
                self._socket.close()
            except Exception:
                pass
            self._socket = None

        if self._process:
            try:
                self._process.terminate()
                self._process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._process.kill()
                self._process.wait(timeout=3)
            except Exception:
                pass
            self._process = None
        self._port = None

    def __enter__(self):
        self.start()
        self.connect()
        return self

    def __exit__(self, *exc):
        self.stop()


def query_via_sdk(cli_path: str = "copilot", filter_opts: Optional[dict] = None) -> dict:
    """
    Start a headless Copilot CLI server, list sessions via session.list RPC,
    then tear down the server. Returns a dict of session_id -> SessionInfo.
    """
    sessions = {}

    try:
        with CopilotHeadlessServer(cli_path=cli_path) as server:
            raw_sessions = server.list_sessions(filter_opts)

            for s in raw_sessions:
                sid = s.get("sessionId", "")
                ctx = s.get("context") or {}
                info = SessionInfo(
                    session_id=sid,
                    cwd=ctx.get("cwd"),
                    git_root=ctx.get("gitRoot"),
                    repository=ctx.get("repository"),
                    branch=ctx.get("branch"),
                    summary=s.get("summary"),
                    created_at=s.get("startTime"),
                    updated_at=s.get("modifiedTime"),
                    source="sdk",
                    state="inactive",  # SDK doesn't distinguish active vs inactive
                )
                sessions[sid] = info

    except FileNotFoundError:
        print("Error: 'copilot' CLI not found. Install it or use --cli-path.", file=sys.stderr)
    except Exception as e:
        print(f"Warning: SDK query failed: {e}", file=sys.stderr)

    return sessions


def merge_sessions(fs_sessions: dict, db_sessions: dict, sdk_sessions: Optional[dict] = None) -> list:
    """Merge filesystem, database, and SDK session data. Filesystem wins for state."""
    # Start with all known IDs
    all_sources = [fs_sessions, db_sessions]
    if sdk_sessions:
        all_sources.append(sdk_sessions)
    all_ids = set()
    for src in all_sources:
        all_ids.update(src.keys())

    merged = []

    for sid in all_ids:
        fs = fs_sessions.get(sid)
        db = db_sessions.get(sid)
        sdk = sdk_sessions.get(sid) if sdk_sessions else None

        # Pick best base record (filesystem > database > sdk)
        base = fs or db or sdk
        if base is None:
            continue

        # Merge data from all sources
        sources = []
        if fs:
            sources.append("filesystem")
        if db:
            sources.append("database")
        if sdk:
            sources.append("sdk")

        result = SessionInfo(
            session_id=sid,
            cwd=fs.cwd if fs else (db.cwd if db else (sdk.cwd if sdk else None)),
            git_root=fs.git_root if fs else (sdk.git_root if sdk else None),
            repository=(fs.repository if fs and fs.repository else None)
                       or (db.repository if db and db.repository else None)
                       or (sdk.repository if sdk and sdk.repository else None),
            branch=(fs.branch if fs and fs.branch else None)
                   or (db.branch if db and db.branch else None)
                   or (sdk.branch if sdk and sdk.branch else None),
            summary=(fs.summary if fs and fs.summary else None)
                    or (db.summary if db and db.summary else None)
                    or (sdk.summary if sdk and sdk.summary else None),
            created_at=fs.created_at if fs else (db.created_at if db else (sdk.created_at if sdk else None)),
            updated_at=fs.updated_at if fs else (db.updated_at if db else (sdk.updated_at if sdk else None)),
            state=fs.state if fs else "inactive",
            owner_pids=fs.owner_pids if fs else [],
            source="+".join(sources),
            turn_count=db.turn_count if db else (fs.turn_count if fs else 0),
        )
        merged.append(result)

    # Sort: active first, then by updated_at descending
    state_order = {"active": 0, "stale": 1, "inactive": 2}
    merged.sort(key=lambda s: (
        state_order.get(s.state, 3),
        s.updated_at or "",
    ), reverse=False)
    # Within each state group, sort by updated_at descending
    merged.sort(key=lambda s: (
        state_order.get(s.state, 3),
        -(datetime.fromisoformat(s.updated_at.replace("Z", "+00:00")).timestamp()
          if s.updated_at else 0),
    ))

    return merged


def format_time_ago(iso_time: str) -> str:
    """Convert ISO timestamp to human-readable relative time."""
    if not iso_time:
        return "unknown"
    try:
        dt = datetime.fromisoformat(iso_time.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        delta = now - dt
        seconds = int(delta.total_seconds())
        if seconds < 60:
            return f"{seconds}s ago"
        minutes = seconds // 60
        if minutes < 60:
            return f"{minutes}m ago"
        hours = minutes // 60
        if hours < 24:
            return f"{hours}h ago"
        days = hours // 24
        return f"{days}d ago"
    except Exception:
        return iso_time[:19]


def truncate(s: str, max_len: int) -> str:
    if not s:
        return ""
    # Collapse whitespace
    s = re.sub(r'\s+', ' ', s).strip()
    if len(s) <= max_len:
        return s
    return s[:max_len - 1] + "…"


STATE_COLORS = {
    "active": "\033[92m",   # green
    "stale": "\033[93m",    # yellow
    "inactive": "\033[90m", # gray
}
RESET = "\033[0m"
BOLD = "\033[1m"


def print_table(sessions: list, verbose: bool = False, use_color: bool = True):
    """Print sessions in a formatted table."""
    if not sessions:
        print("No sessions found.")
        return

    active = [s for s in sessions if s.state == "active"]
    stale = [s for s in sessions if s.state == "stale"]
    inactive = [s for s in sessions if s.state == "inactive"]

    c = STATE_COLORS if use_color else {k: "" for k in STATE_COLORS}
    r = RESET if use_color else ""
    b = BOLD if use_color else ""

    # Use ASCII-safe symbols when output may not support Unicode
    try:
        "●○─".encode(sys.stdout.encoding or "utf-8")
        dot_filled, dot_empty, line_char = "●", "○", "─"
    except (UnicodeEncodeError, LookupError):
        dot_filled, dot_empty, line_char = "*", "o", "-"

    print(f"\n{b}Copilot CLI Sessions{r}")
    print(f"  Total: {len(sessions)} | "
          f"{c['active']}{dot_filled} Active: {len(active)}{r} | "
          f"{c['stale']}{dot_filled} Stale: {len(stale)}{r} | "
          f"{c['inactive']}{dot_empty} Inactive: {len(inactive)}{r}\n")

    # Header
    if verbose:
        fmt = "  {state:<8} {sid:<38} {updated:<10} {turns:<6} {cwd:<40} {summary}"
        print(fmt.format(state="STATE", sid="SESSION ID", updated="UPDATED",
                         turns="TURNS", cwd="CWD", summary="SUMMARY"))
        print("  " + line_char * 120)
    else:
        fmt = "  {state:<8} {sid:<12} {updated:<10} {turns:<6} {summary}"
        print(fmt.format(state="STATE", sid="SESSION ID", updated="UPDATED",
                         turns="TURNS", summary="SUMMARY"))
        print("  " + line_char * 80)

    for s in sessions:
        sc = c.get(s.state, "")
        state_icon = dot_filled if s.state in ("active", "stale") else dot_empty
        state_label = f"{sc}{state_icon} {s.state}{r}"

        pid_suffix = ""
        if s.state == "active" and s.owner_pids:
            pid_suffix = f" (PID {','.join(str(p) for p in s.owner_pids)})"

        if verbose:
            print(f"  {state_label:<20} {s.session_id:<38} "
                  f"{format_time_ago(s.updated_at):<10} "
                  f"{s.turn_count:<6} "
                  f"{truncate(s.cwd or '', 40):<40} "
                  f"{truncate(s.summary or '', 50)}{pid_suffix}")
        else:
            short_id = s.session_id[:8] + "…"
            print(f"  {state_label:<20} {short_id:<12} "
                  f"{format_time_ago(s.updated_at):<10} "
                  f"{s.turn_count:<6} "
                  f"{truncate(s.summary or '', 50)}{pid_suffix}")

    print()


def main():
    parser = argparse.ArgumentParser(
        description="List all Copilot CLI sessions on this device"
    )
    parser.add_argument("--active", action="store_true",
                        help="Show only active/connected sessions")
    parser.add_argument("--json", action="store_true",
                        help="Output as JSON")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Show full session IDs and extra details")
    parser.add_argument("--no-color", action="store_true",
                        help="Disable color output")
    parser.add_argument("--config-dir", type=str, default=None,
                        help="Copilot config directory (default: ~/.copilot)")
    parser.add_argument("--use-sdk", action="store_true",
                        help="Also query via headless CLI server (session.list RPC)")
    parser.add_argument("--sdk-only", action="store_true",
                        help="Only use headless CLI server (skip filesystem/DB)")
    parser.add_argument("--cli-path", type=str, default="copilot",
                        help="Path to copilot CLI binary (default: copilot)")
    args = parser.parse_args()

    copilot_dir = Path(args.config_dir) if args.config_dir else get_copilot_dir()

    if args.sdk_only:
        # SDK-only mode: start headless server, query, teardown
        sdk_sessions = query_via_sdk(cli_path=args.cli_path)
        sessions = list(sdk_sessions.values())
        # Sort by updated_at descending
        sessions.sort(
            key=lambda s: s.updated_at or "", reverse=True
        )
    else:
        if not copilot_dir.exists():
            print(f"Copilot directory not found: {copilot_dir}", file=sys.stderr)
            sys.exit(1)

        running_pids = get_running_copilot_pids()
        fs_sessions = scan_filesystem(copilot_dir, running_pids)
        db_sessions = query_database(copilot_dir)

        sdk_sessions = None
        if args.use_sdk:
            sdk_sessions = query_via_sdk(cli_path=args.cli_path)

        sessions = merge_sessions(fs_sessions, db_sessions, sdk_sessions)

    if args.active:
        sessions = [s for s in sessions if s.state == "active"]

    if args.json:
        print(json.dumps([asdict(s) for s in sessions], indent=2))
    else:
        use_color = not args.no_color and sys.stdout.isatty()
        print_table(sessions, verbose=args.verbose, use_color=use_color)


if __name__ == "__main__":
    main()
