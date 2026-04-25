from __future__ import annotations

from pathlib import Path

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
