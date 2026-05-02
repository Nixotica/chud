"""Tests for the filesystem-access gate in AgentSession._on_tool_request.

The gate enforces two layers:

* Layer 1 (pre-attach): with no repos attached, deny Edit/Write/NotebookEdit
  and Bash so the agent must call ``mcp__chud__attach_repo`` before any
  filesystem mutation.
* Layer 2 (path gate): once repos are attached, deny path-typed tools whose
  resolved target falls outside any attached worktree.

Plan-mode statuses bypass the gate (the SDK's plan permission mode already
disables edits, and re-denying here would mask plan-time tool calls).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

from claude_agent_sdk.types import PermissionResultAllow, PermissionResultDeny

from chud.session import AgentSession
from chud.types import SessionState, SessionStatus, Worktree


def _make_session(
    tmp_path: Path,
    *,
    status: SessionStatus = SessionStatus.EXECUTING,
    repos: dict[str, Worktree] | None = None,
) -> AgentSession:
    del tmp_path
    state = SessionState(id="t-fs-gate")
    state.status = status
    if repos:
        state.attached_repos = repos
    return AgentSession(state)


def _worktree(tmp_path: Path, name: str) -> Worktree:
    repo = tmp_path / f"{name}-repo"
    wt = tmp_path / f"{name}-wt"
    repo.mkdir()
    wt.mkdir()
    return Worktree(repo_path=repo, worktree_path=wt, branch=f"chud/{name}")


# ---------------------------------------------------------------- Layer 1: pre-attach


async def test_pre_attach_denies_edit(tmp_path):
    sess = _make_session(tmp_path)
    result = await sess._on_tool_request(
        "Edit",
        {"file_path": str(tmp_path / "anything.txt")},
        context=None,  # type: ignore[arg-type]
    )
    assert isinstance(result, PermissionResultDeny)
    assert "before any repo is attached" in result.message
    assert "mcp__chud__attach_repo" in result.message


async def test_pre_attach_denies_bash(tmp_path):
    sess = _make_session(tmp_path)
    result = await sess._on_tool_request(
        "Bash",
        {"command": "ls"},
        context=None,  # type: ignore[arg-type]
    )
    assert isinstance(result, PermissionResultDeny)
    assert "Bash" in result.message


async def test_pre_attach_allows_read(tmp_path):
    sess = _make_session(tmp_path)
    result = await sess._on_tool_request(
        "Read",
        {"file_path": str(tmp_path / "anything.txt")},
        context=None,  # type: ignore[arg-type]
    )
    assert isinstance(result, PermissionResultAllow)


async def test_pre_attach_allows_glob_and_ls(tmp_path):
    sess = _make_session(tmp_path)
    for tool in ("Glob", "LS", "Grep"):
        result = await sess._on_tool_request(
            tool,
            {},
            context=None,  # type: ignore[arg-type]
        )
        assert isinstance(result, PermissionResultAllow), tool


# ---------------------------------------------------------------- Layer 2: path gate


async def test_attached_allows_edit_inside_worktree(tmp_path):
    wt = _worktree(tmp_path, "alpha")
    sess = _make_session(tmp_path, repos={"alpha": wt})
    inside = wt.worktree_path / "src" / "main.py"
    result = await sess._on_tool_request(
        "Edit",
        {"file_path": str(inside)},
        context=None,  # type: ignore[arg-type]
    )
    assert isinstance(result, PermissionResultAllow)


async def test_attached_denies_edit_outside_worktree(tmp_path):
    wt = _worktree(tmp_path, "alpha")
    sess = _make_session(tmp_path, repos={"alpha": wt})
    outside = tmp_path / "elsewhere" / "README.md"
    result = await sess._on_tool_request(
        "Edit",
        {"file_path": str(outside)},
        context=None,  # type: ignore[arg-type]
    )
    assert isinstance(result, PermissionResultDeny)
    assert str(wt.worktree_path) in result.message
    assert "outside" in result.message


async def test_write_and_notebookedit_follow_edit_semantics(tmp_path):
    wt = _worktree(tmp_path, "alpha")
    sess = _make_session(tmp_path, repos={"alpha": wt})
    outside = tmp_path / "elsewhere" / "thing.ipynb"

    write_result = await sess._on_tool_request(
        "Write",
        {"file_path": str(outside)},
        context=None,  # type: ignore[arg-type]
    )
    assert isinstance(write_result, PermissionResultDeny)

    nb_result = await sess._on_tool_request(
        "NotebookEdit",
        {"notebook_path": str(outside)},
        context=None,  # type: ignore[arg-type]
    )
    assert isinstance(nb_result, PermissionResultDeny)


async def test_attached_allows_bash_unconditionally(tmp_path):
    """Layer 3b: with a repo attached, Bash is not statically vetted —
    cwd reframing at session start does the passive containment work."""
    wt = _worktree(tmp_path, "alpha")
    sess = _make_session(tmp_path, repos={"alpha": wt})
    result = await sess._on_tool_request(
        "Bash",
        {"command": "rm -rf /tmp/anywhere"},
        context=None,  # type: ignore[arg-type]
    )
    assert isinstance(result, PermissionResultAllow)


async def test_multiple_worktrees_each_allowed(tmp_path):
    a = _worktree(tmp_path, "a")
    b = _worktree(tmp_path, "b")
    sess = _make_session(tmp_path, repos={"a": a, "b": b})
    for wt in (a, b):
        result = await sess._on_tool_request(
            "Edit",
            {"file_path": str(wt.worktree_path / "f.txt")},
            context=None,  # type: ignore[arg-type]
        )
        assert isinstance(result, PermissionResultAllow), wt.worktree_path


async def test_path_traversal_resolved_before_check(tmp_path):
    """`/wt/../elsewhere/x` should be denied; `relative_to` must compare
    realpaths, not raw inputs."""
    wt = _worktree(tmp_path, "alpha")
    (tmp_path / "elsewhere").mkdir()
    sess = _make_session(tmp_path, repos={"alpha": wt})
    sneaky = wt.worktree_path / ".." / "elsewhere" / "x.txt"
    result = await sess._on_tool_request(
        "Edit",
        {"file_path": str(sneaky)},
        context=None,  # type: ignore[arg-type]
    )
    assert isinstance(result, PermissionResultDeny)


# ---------------------------------------------------------------- Plan-mode bypass


async def test_plan_mode_bypasses_gate(tmp_path):
    """During PLANNING and AWAITING_PLAN_APPROVAL the SDK's plan permission
    mode already prevents edits; the chud gate must not preemptively deny."""
    for status in (SessionStatus.PLANNING, SessionStatus.AWAITING_PLAN_APPROVAL):
        sess = _make_session(tmp_path, status=status)
        result = await sess._on_tool_request(
            "Edit",
            {"file_path": str(tmp_path / "x.txt")},
            context=None,  # type: ignore[arg-type]
        )
        assert isinstance(result, PermissionResultAllow), status


# ---------------------------------------------------------------- PreToolUse hook
# can_use_tool is bypassed once permission_mode flips to acceptEdits, so the
# real enforcement runs through the PreToolUse hook. Verify it returns the
# expected hookSpecificOutput shape with permissionDecision="deny".


async def test_pre_tool_use_hook_denies_pre_attach(tmp_path):
    sess = _make_session(tmp_path)
    out = await sess._on_pre_tool_use_hook(
        {"tool_name": "Edit", "tool_input": {"file_path": str(tmp_path / "x.txt")}},  # type: ignore[arg-type]
        tool_use_id=None,
        context=None,  # type: ignore[arg-type]
    )
    spec = cast(dict[str, Any], out)["hookSpecificOutput"]
    assert spec["hookEventName"] == "PreToolUse"
    assert spec["permissionDecision"] == "deny"
    assert "before any repo is attached" in spec["permissionDecisionReason"]


async def test_pre_tool_use_hook_denies_outside_worktree(tmp_path):
    wt = _worktree(tmp_path, "alpha")
    sess = _make_session(tmp_path, repos={"alpha": wt})
    out = await sess._on_pre_tool_use_hook(
        {
            "tool_name": "Edit",
            "tool_input": {"file_path": str(tmp_path / "elsewhere" / "x.txt")},
        },  # type: ignore[arg-type]
        tool_use_id=None,
        context=None,  # type: ignore[arg-type]
    )
    spec = cast(dict[str, Any], out)["hookSpecificOutput"]
    assert spec["permissionDecision"] == "deny"
    assert "outside" in spec["permissionDecisionReason"]


async def test_pre_tool_use_hook_allows_inside_worktree(tmp_path):
    wt = _worktree(tmp_path, "alpha")
    sess = _make_session(tmp_path, repos={"alpha": wt})
    out = await sess._on_pre_tool_use_hook(
        {
            "tool_name": "Edit",
            "tool_input": {"file_path": str(wt.worktree_path / "src" / "main.py")},
        },  # type: ignore[arg-type]
        tool_use_id=None,
        context=None,  # type: ignore[arg-type]
    )
    # Allow path = empty hook output (no hookSpecificOutput).
    assert "hookSpecificOutput" not in cast(dict[str, Any], out)
