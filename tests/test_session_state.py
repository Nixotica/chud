from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from chud.options import OPT_MAKE_DRAFT_PR, OPT_SELF_CLEANUP, default_options
from chud.types import SessionState, SessionStatus, Worktree


def test_session_state_roundtrip():
    s = SessionState(
        id="abc123",
        workspace_dir=Path("/tmp/chud-ws/abc123"),
        status=SessionStatus.EXECUTING,
        initial_prompt="hello",
    )
    s.attached_repos["myrepo"] = Worktree(
        repo_path=Path("/tmp/myrepo"),
        worktree_path=Path("/tmp/chud-ws/abc123/myrepo"),
        branch="chud/abc123",
    )
    d = s.to_dict()
    s2 = SessionState.from_dict(d)
    assert s2.id == s.id
    assert s2.status == SessionStatus.EXECUTING
    assert s2.workspace_dir == s.workspace_dir
    assert s2.attached_repos["myrepo"].branch == "chud/abc123"
    assert s2.attached_repos["myrepo"].repo_path == Path("/tmp/myrepo")


def test_session_state_serializes_options():
    s = SessionState(
        id="abc123",
        workspace_dir=Path("/tmp/chud-ws/abc123"),
        options={OPT_MAKE_DRAFT_PR: True, OPT_SELF_CLEANUP: False},
    )
    d = s.to_dict()
    assert d["options"] == {OPT_MAKE_DRAFT_PR: True, OPT_SELF_CLEANUP: False}
    s2 = SessionState.from_dict(d)
    assert s2.options[OPT_MAKE_DRAFT_PR] is True
    assert s2.options[OPT_SELF_CLEANUP] is False


def test_legacy_session_without_options_loads_with_defaults():
    # Simulate a sessions.json entry written before options existed.
    legacy = {
        "id": "old1",
        "workspace_dir": "/tmp/chud-ws/old1",
        "status": SessionStatus.DONE.value,
        "initial_prompt": "legacy",
        "attached_repos": {},
        "created_at": datetime.now(timezone.utc).isoformat(),
        "last_activity_at": datetime.now(timezone.utc).isoformat(),
        "pending_question": None,
        "error": None,
        # no "options" key
    }
    s = SessionState.from_dict(legacy)
    assert s.options == default_options()


def test_session_state_drops_unknown_persisted_options():
    s = SessionState(id="x", workspace_dir=Path("/tmp/x"))
    d = s.to_dict()
    d["options"] = {"some_removed_option": True, OPT_SELF_CLEANUP: True}
    s2 = SessionState.from_dict(d)
    assert "some_removed_option" not in s2.options
    assert s2.options[OPT_SELF_CLEANUP] is True
