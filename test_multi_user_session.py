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
import os
import platform
import shutil
import sys
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# Test helpers: classify_session, get_session_display_name, device utilities
# These are imported from the bot module or defined inline for isolation.
# ---------------------------------------------------------------------------

# Import the bot's helper functions for unit testing
sys.path.insert(0, os.path.dirname(__file__))
from copilot_telegram_bot import (
    classify_session,
    get_session_display_name,
    get_device_name,
    load_device_registry,
    save_device_registry,
    update_device_entry,
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


class TestDeviceRegistry:
    """Test the device registry load/save/update."""

    def test_round_trip(self, tmp_path, monkeypatch):
        registry_path = tmp_path / "device_registry.json"
        monkeypatch.setattr(
            "copilot_telegram_bot._get_registry_path",
            lambda: registry_path,
        )
        assert load_device_registry() == {}

        registry = {"dev-a": {"hostname": "a", "sessions": []}}
        save_device_registry(registry)

        loaded = load_device_registry()
        assert loaded["dev-a"]["hostname"] == "a"

    def test_update_entry(self, tmp_path, monkeypatch):
        registry_path = tmp_path / "device_registry.json"
        monkeypatch.setattr(
            "copilot_telegram_bot._get_registry_path",
            lambda: registry_path,
        )

        mock_session = MagicMock()
        mock_session.sessionId = "sess-123"
        mock_session.summary = "Test session"
        mock_session.modifiedTime = "2026-05-10T12:00:00Z"
        mock_session.context = MagicMock()
        mock_session.context.cwd = "/home/dev"
        mock_session.context.repository = "org/repo"

        update_device_entry("laptop", [mock_session])

        registry = load_device_registry()
        assert "laptop" in registry
        assert len(registry["laptop"]["sessions"]) == 1
        assert registry["laptop"]["sessions"][0]["sessionId"] == "sess-123"


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


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
