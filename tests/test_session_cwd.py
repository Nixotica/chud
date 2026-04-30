"""Tests for AgentSession.start() cwd derivation.

Regression: agents used to launch with cwd pointing at the parent of the
worktree instead of the worktree path itself, which made them edit the source
repo instead of the worktree. start() now picks the worktree path when
exactly one repo is attached, and falls back to ``worktrees_root()`` for the
0-repo and ≥2-repo cases (when no launch_cwd is provided).
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


@pytest.fixture
def isolated_root(tmp_path: Path, monkeypatch):
    root = tmp_path / "wt-root"
    root.mkdir()
    monkeypatch.setattr("chud.session.worktrees_root", lambda: root)
    return root


def _state(repos: list[Worktree] | None = None) -> SessionState:
    st = SessionState(id="t1")
    for wt in repos or []:
        st.attached_repos[str(wt.repo_path)] = wt
    return st


async def test_start_with_no_repos_falls_back_to_worktrees_root(
    patched_client, isolated_root: Path
):
    sess = AgentSession(_state())
    try:
        await sess.start("hello")
        assert patched_client.last_options.cwd == isolated_root
    finally:
        await sess.stop()


async def test_start_with_single_repo_uses_worktree_path(tmp_path, patched_client):
    repo = tmp_path / "src"
    wt_path = tmp_path / "wt"
    wt_path.mkdir()
    wt = Worktree(repo_path=repo, worktree_path=wt_path, branch="chud/x-t1")

    sess = AgentSession(_state([wt]))
    try:
        await sess.start("hello")
        assert patched_client.last_options.cwd == wt_path
    finally:
        await sess.stop()


async def test_start_forwards_effort_to_sdk_options(patched_client, isolated_root: Path):
    del isolated_root
    sess = AgentSession(_state(), effort="high")
    try:
        await sess.start("hello")
        assert patched_client.last_options.effort == "high"
    finally:
        await sess.stop()


async def test_start_with_no_effort_passes_none(patched_client, isolated_root: Path):
    del isolated_root
    sess = AgentSession(_state())
    try:
        await sess.start("hello")
        assert patched_client.last_options.effort is None
    finally:
        await sess.stop()


async def test_start_with_two_repos_falls_back_to_worktrees_root(
    tmp_path, patched_client, isolated_root: Path
):
    wt1 = Worktree(
        repo_path=tmp_path / "a",
        worktree_path=isolated_root / "t1-a",
        branch="chud/x-t1",
    )
    wt2 = Worktree(
        repo_path=tmp_path / "b",
        worktree_path=isolated_root / "t1-b",
        branch="chud/x-t1",
    )
    sess = AgentSession(_state([wt1, wt2]))
    try:
        await sess.start("hello")
        assert patched_client.last_options.cwd == isolated_root
    finally:
        await sess.stop()
