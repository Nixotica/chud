"""Tests for AgentSession.start() cwd derivation.

Regression: agents used to launch with cwd=workspace_dir (the parent of the
worktree) instead of the worktree path itself, which made them edit the source
repo instead of the worktree. start() now picks the worktree path when
exactly one repo is attached, and falls back to workspace_dir for the
0-repo and ≥2-repo cases.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from chud.session import AgentSession
from chud.types import SessionState, Worktree


class _CapturingClient:
    """Stand-in for ClaudeSDKClient that records the options it was constructed with."""

    last_options = None

    def __init__(self, options) -> None:
        type(self).last_options = options

    async def connect(self) -> None:
        pass

    async def disconnect(self) -> None:
        pass

    async def interrupt(self) -> None:
        pass

    async def query(self, *_a, **_kw) -> None:
        pass

    async def receive_messages(self):
        await asyncio.Event().wait()
        if False:
            yield  # pragma: no cover


@pytest.fixture
def patched_client(monkeypatch):
    _CapturingClient.last_options = None
    monkeypatch.setattr("chud.session.ClaudeSDKClient", _CapturingClient)
    return _CapturingClient


def _state(workspace: Path, repos: list[Worktree] | None = None) -> SessionState:
    st = SessionState(id="t1", workspace_dir=workspace)
    for wt in repos or []:
        st.attached_repos[str(wt.repo_path)] = wt
    return st


async def test_start_with_no_repos_uses_workspace_dir(tmp_path, patched_client):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    sess = AgentSession(_state(workspace))
    try:
        await sess.start("hello")
        assert patched_client.last_options.cwd == workspace
    finally:
        await sess.stop()


async def test_start_with_single_repo_uses_worktree_path(tmp_path, patched_client):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    repo = tmp_path / "src"
    wt_path = workspace / "src"
    wt_path.mkdir()
    wt = Worktree(repo_path=repo, worktree_path=wt_path, branch="chud/x-t1")

    sess = AgentSession(_state(workspace, [wt]))
    try:
        await sess.start("hello")
        assert patched_client.last_options.cwd == wt_path
    finally:
        await sess.stop()


async def test_start_with_two_repos_uses_workspace_dir(tmp_path, patched_client):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    wt1 = Worktree(
        repo_path=tmp_path / "a",
        worktree_path=workspace / "a",
        branch="chud/x-t1",
    )
    wt2 = Worktree(
        repo_path=tmp_path / "b",
        worktree_path=workspace / "b",
        branch="chud/x-t1",
    )
    sess = AgentSession(_state(workspace, [wt1, wt2]))
    try:
        await sess.start("hello")
        assert patched_client.last_options.cwd == workspace
    finally:
        await sess.stop()
