"""Tests for SessionManager DONE-side-effect dispatch and kill_session cleanup.

We don't spin up a real ClaudeSDKClient — instead we manually construct an
``AgentSession`` so we can observe how ``_handle_event`` reacts to a synthetic
STATUS_CHANGED(DONE) event.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from chud import pr as pr_mod
from chud.manager import SessionManager
from chud.options import OPT_MAKE_DRAFT_PR, OPT_SELF_CLEANUP
from chud.session import AgentSession
from chud.types import Event, EventKind, SessionState, SessionStatus
from chud.worktree import WorktreeManager


def _make_session(options: dict[str, bool], tmp_path: Path) -> AgentSession:
    state = SessionState(
        id="sess-test",
        workspace_dir=tmp_path / "ws",
        status=SessionStatus.DONE,
        initial_prompt="do the thing",
        options=options,
    )
    state.workspace_dir.mkdir(parents=True, exist_ok=True)
    return AgentSession(state, model=None)


def _done_event(sid: str) -> Event:
    return Event(
        session_id=sid,
        kind=EventKind.STATUS_CHANGED,
        payload={"status": SessionStatus.DONE.value},
    )


@pytest.mark.asyncio
async def test_done_with_self_cleanup_emits_cleanup_requested(tmp_path, monkeypatch):
    mgr = SessionManager()
    sess = _make_session({OPT_SELF_CLEANUP: True, OPT_MAKE_DRAFT_PR: False}, tmp_path)
    mgr.sessions[sess.state.id] = sess
    # Avoid touching real sessions.json.
    monkeypatch.setattr(mgr, "_persist", lambda: None)

    queue = mgr.subscribe()
    await mgr._handle_event(_done_event(sess.state.id), sess)

    kinds: list[EventKind] = []
    while not queue.empty():
        kinds.append(queue.get_nowait().kind)
    assert EventKind.CLEANUP_REQUESTED in kinds


@pytest.mark.asyncio
async def test_done_without_options_emits_no_extra_events(tmp_path, monkeypatch):
    mgr = SessionManager()
    sess = _make_session({}, tmp_path)
    mgr.sessions[sess.state.id] = sess
    monkeypatch.setattr(mgr, "_persist", lambda: None)

    queue = mgr.subscribe()
    await mgr._handle_event(_done_event(sess.state.id), sess)

    assert queue.empty(), "no CLEANUP_REQUESTED should fire when option is off"


@pytest.mark.asyncio
async def test_done_with_make_draft_pr_invokes_publisher(tmp_path, monkeypatch):
    mgr = SessionManager()
    sess = _make_session({OPT_MAKE_DRAFT_PR: True, OPT_SELF_CLEANUP: False}, tmp_path)
    mgr.sessions[sess.state.id] = sess
    monkeypatch.setattr(mgr, "_persist", lambda: None)

    calls: list[str] = []

    async def fake_publish(state):
        calls.append(state.id)
        return []

    monkeypatch.setattr(pr_mod, "publish_draft_prs", fake_publish)

    await mgr._handle_event(_done_event(sess.state.id), sess)

    # _on_session_done schedules an asyncio task; await pending tasks.
    pending = list(mgr._pr_tasks)
    for task in pending:
        await task

    assert calls == [sess.state.id]


@pytest.mark.asyncio
async def test_done_handled_only_once(tmp_path, monkeypatch):
    """A second DONE STATUS_CHANGED for the same session must not re-fire side effects."""
    mgr = SessionManager()
    sess = _make_session({OPT_SELF_CLEANUP: True}, tmp_path)
    mgr.sessions[sess.state.id] = sess
    monkeypatch.setattr(mgr, "_persist", lambda: None)

    queue = mgr.subscribe()
    await mgr._handle_event(_done_event(sess.state.id), sess)
    await mgr._handle_event(_done_event(sess.state.id), sess)

    cleanup_count = 0
    while not queue.empty():
        ev = queue.get_nowait()
        if ev.kind == EventKind.CLEANUP_REQUESTED:
            cleanup_count += 1
    assert cleanup_count == 1


@pytest.mark.asyncio
async def test_kill_session_with_cleanup_workspace_calls_worktree_cleanup(
    tmp_path, monkeypatch
):
    mgr = SessionManager()
    sess = _make_session({}, tmp_path)
    mgr.sessions[sess.state.id] = sess
    wt_mgr = WorktreeManager(sess.state)
    mgr.worktrees[sess.state.id] = wt_mgr
    monkeypatch.setattr(mgr, "_persist", lambda: None)

    cleanup_calls: list[str] = []
    monkeypatch.setattr(
        wt_mgr,
        "cleanup_workspace",
        lambda: cleanup_calls.append(sess.state.id),
    )

    # Stub stop() so we don't try to talk to a real SDK client.
    async def noop_stop():
        pass

    monkeypatch.setattr(sess, "stop", noop_stop)

    await mgr.kill_session(sess.state.id, cleanup_workspace=True)

    assert cleanup_calls == [sess.state.id]
    assert sess.state.id not in mgr.sessions
    assert sess.state.id not in mgr.worktrees


@pytest.mark.asyncio
async def test_kill_session_default_does_not_cleanup(tmp_path, monkeypatch):
    mgr = SessionManager()
    sess = _make_session({}, tmp_path)
    mgr.sessions[sess.state.id] = sess
    wt_mgr = WorktreeManager(sess.state)
    mgr.worktrees[sess.state.id] = wt_mgr
    monkeypatch.setattr(mgr, "_persist", lambda: None)

    cleanup_calls: list[str] = []
    monkeypatch.setattr(
        wt_mgr, "cleanup_workspace", lambda: cleanup_calls.append("nope")
    )

    async def noop_stop():
        pass

    monkeypatch.setattr(sess, "stop", noop_stop)

    await mgr.kill_session(sess.state.id)

    assert cleanup_calls == []
