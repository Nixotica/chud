"""Tests for SessionManager DONE-side-effect dispatch and kill_session cleanup.

We don't spin up a real ClaudeSDKClient — instead we manually construct an
``AgentSession`` so we can observe how ``_handle_event`` reacts to a synthetic
STATUS_CHANGED(DONE) event.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from chud import pr as pr_mod
from chud.manager import SessionManager
from chud.options import OPT_MAKE_DRAFT_PR, OPT_SELF_CLEANUP
from chud.pr import PRResult
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
async def test_done_with_make_draft_pr_requests_review_not_publish(
    tmp_path, monkeypatch
):
    """DONE with the draft-PR option must broadcast PR_REVIEW_REQUESTED and
    NOT publish until ``submit_pr_review(accepted=True)`` is invoked."""
    mgr = SessionManager()
    sess = _make_session({OPT_MAKE_DRAFT_PR: True, OPT_SELF_CLEANUP: False}, tmp_path)
    mgr.sessions[sess.state.id] = sess
    monkeypatch.setattr(mgr, "_persist", lambda: None)

    calls: list[str] = []

    async def fake_publish(state, title=None, body=None):
        calls.append(state.id)
        return []

    monkeypatch.setattr(pr_mod, "publish_draft_prs", fake_publish)

    queue = mgr.subscribe()
    await mgr._handle_event(_done_event(sess.state.id), sess)

    # No pending PR task yet — the manager waits for submit_pr_review.
    for task in list(mgr._pr_tasks.values()):
        await task
    assert calls == []

    kinds: list[EventKind] = []
    while not queue.empty():
        kinds.append(queue.get_nowait().kind)
    assert EventKind.PR_REVIEW_REQUESTED in kinds
    assert EventKind.PR_PUBLISHED not in kinds


@pytest.mark.asyncio
async def test_submit_pr_review_accept_publishes(tmp_path, monkeypatch):
    mgr = SessionManager()
    sess = _make_session({OPT_MAKE_DRAFT_PR: True, OPT_SELF_CLEANUP: False}, tmp_path)
    mgr.sessions[sess.state.id] = sess
    monkeypatch.setattr(mgr, "_persist", lambda: None)

    seen: list[tuple[str, str | None, str | None]] = []

    async def fake_publish(state, title=None, body=None):
        seen.append((state.id, title, body))
        return [PRResult(repo_label="r", branch="b", url="https://e/pr/1")]

    monkeypatch.setattr(pr_mod, "publish_draft_prs", fake_publish)

    queue = mgr.subscribe()
    await mgr.submit_pr_review(
        sess.state.id, accepted=True, title="edited title", body="edited body"
    )
    for task in list(mgr._pr_tasks.values()):
        await task

    assert seen == [(sess.state.id, "edited title", "edited body")]
    kinds: list[EventKind] = []
    while not queue.empty():
        kinds.append(queue.get_nowait().kind)
    assert EventKind.PR_PUBLISHED in kinds
    assert EventKind.CLEANUP_REQUESTED not in kinds


@pytest.mark.asyncio
async def test_submit_pr_review_reject_skips_publish(tmp_path, monkeypatch):
    mgr = SessionManager()
    sess = _make_session({OPT_MAKE_DRAFT_PR: True, OPT_SELF_CLEANUP: False}, tmp_path)
    mgr.sessions[sess.state.id] = sess
    monkeypatch.setattr(mgr, "_persist", lambda: None)

    calls: list[str] = []

    async def fake_publish(state, title=None, body=None):
        calls.append(state.id)
        return []

    monkeypatch.setattr(pr_mod, "publish_draft_prs", fake_publish)

    queue = mgr.subscribe()
    await mgr.submit_pr_review(sess.state.id, accepted=False)
    for task in list(mgr._pr_tasks.values()):
        await task

    assert calls == []
    kinds: list[EventKind] = []
    while not queue.empty():
        kinds.append(queue.get_nowait().kind)
    assert EventKind.PR_PUBLISHED not in kinds
    assert EventKind.CLEANUP_REQUESTED not in kinds


@pytest.mark.asyncio
async def test_submit_pr_review_reject_with_cleanup_emits_cleanup(
    tmp_path, monkeypatch
):
    """Rejecting the PR must still trigger the cleanup prompt when the
    self-cleanup option is enabled — the user may have rejected the PR
    *because* they want to wipe the workspace."""
    mgr = SessionManager()
    sess = _make_session({OPT_MAKE_DRAFT_PR: True, OPT_SELF_CLEANUP: True}, tmp_path)
    mgr.sessions[sess.state.id] = sess
    monkeypatch.setattr(mgr, "_persist", lambda: None)

    async def fake_publish(state, title=None, body=None):
        raise AssertionError("publish must not run on reject")

    monkeypatch.setattr(pr_mod, "publish_draft_prs", fake_publish)

    queue = mgr.subscribe()
    await mgr.submit_pr_review(sess.state.id, accepted=False)
    for task in list(mgr._pr_tasks.values()):
        await task

    kinds: list[EventKind] = []
    while not queue.empty():
        kinds.append(queue.get_nowait().kind)
    assert kinds.count(EventKind.CLEANUP_REQUESTED) == 1


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
async def test_kill_session_with_cleanup_workspace_calls_worktree_cleanup(tmp_path, monkeypatch):
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
    monkeypatch.setattr(wt_mgr, "cleanup_workspace", lambda: cleanup_calls.append("nope"))

    async def noop_stop():
        pass

    monkeypatch.setattr(sess, "stop", noop_stop)

    await mgr.kill_session(sess.state.id)

    assert cleanup_calls == []


@pytest.mark.asyncio
async def test_accepted_pr_and_cleanup_orders_events(tmp_path, monkeypatch):
    """CLEANUP_REQUESTED must be broadcast only after publish_draft_prs returns.

    Locks the bug where cleanup raced PR publish, wiping the worktree before
    git push could finish. Now the user explicitly accepts via
    ``submit_pr_review`` before publish runs.
    """
    mgr = SessionManager()
    sess = _make_session({OPT_MAKE_DRAFT_PR: True, OPT_SELF_CLEANUP: True}, tmp_path)
    mgr.sessions[sess.state.id] = sess
    monkeypatch.setattr(mgr, "_persist", lambda: None)

    async def fake_publish(state, title=None, body=None):
        for _ in range(3):
            await asyncio.sleep(0)
        return [PRResult(repo_label="r", branch="b", url="https://example/pr/1")]

    monkeypatch.setattr(pr_mod, "publish_draft_prs", fake_publish)

    queue = mgr.subscribe()

    # DONE first surfaces the review request.
    await mgr._handle_event(_done_event(sess.state.id), sess)
    # Drain the review-request event so we only inspect the publish→cleanup
    # ordering produced by submit_pr_review.
    while not queue.empty():
        queue.get_nowait()

    await mgr.submit_pr_review(sess.state.id, accepted=True)
    for task in list(mgr._pr_tasks.values()):
        await task

    kinds: list[EventKind] = []
    while not queue.empty():
        kinds.append(queue.get_nowait().kind)

    assert EventKind.PR_PUBLISHED in kinds
    assert EventKind.CLEANUP_REQUESTED in kinds
    assert kinds.index(EventKind.PR_PUBLISHED) < kinds.index(EventKind.CLEANUP_REQUESTED)


@pytest.mark.asyncio
async def test_cleanup_payload_carries_published_prs(tmp_path, monkeypatch):
    """CLEANUP_REQUESTED's payload must include the list of successful PRs so
    the cleanup modal can render them without a separate event-log lookup.

    Mixed success/failure: only successful entries (``error is None`` and a
    truthy URL, not discarded) make it into ``published_prs``.
    """
    mgr = SessionManager()
    sess = _make_session({OPT_MAKE_DRAFT_PR: True, OPT_SELF_CLEANUP: True}, tmp_path)
    mgr.sessions[sess.state.id] = sess
    monkeypatch.setattr(mgr, "_persist", lambda: None)

    async def fake_publish(state, title=None, body=None):
        return [
            PRResult(repo_label="ok", branch="b1", url="https://example/pr/1"),
            PRResult(repo_label="fail", branch="b2", error="boom"),
            PRResult(repo_label="empty", branch="b3", discarded=True),
        ]

    monkeypatch.setattr(pr_mod, "publish_draft_prs", fake_publish)

    queue = mgr.subscribe()
    await mgr._handle_event(_done_event(sess.state.id), sess)
    while not queue.empty():
        queue.get_nowait()

    await mgr.submit_pr_review(sess.state.id, accepted=True)
    for task in list(mgr._pr_tasks.values()):
        await task

    cleanup_events = []
    while not queue.empty():
        ev = queue.get_nowait()
        if ev.kind == EventKind.CLEANUP_REQUESTED:
            cleanup_events.append(ev)

    assert len(cleanup_events) == 1
    assert cleanup_events[0].payload == {
        "published_prs": [{"repo": "ok", "url": "https://example/pr/1"}],
    }


@pytest.mark.asyncio
async def test_cleanup_payload_empty_on_publish_failure(tmp_path, monkeypatch):
    """When publish raises, CLEANUP_REQUESTED still fires with empty
    ``published_prs`` so the modal falls back to the destructive-warning copy.
    """
    mgr = SessionManager()
    sess = _make_session({OPT_MAKE_DRAFT_PR: True, OPT_SELF_CLEANUP: True}, tmp_path)
    mgr.sessions[sess.state.id] = sess
    monkeypatch.setattr(mgr, "_persist", lambda: None)

    async def boom(state, title=None, body=None):
        raise RuntimeError("publish exploded")

    monkeypatch.setattr(pr_mod, "publish_draft_prs", boom)

    queue = mgr.subscribe()
    await mgr._handle_event(_done_event(sess.state.id), sess)
    while not queue.empty():
        queue.get_nowait()

    await mgr.submit_pr_review(sess.state.id, accepted=True)
    for task in list(mgr._pr_tasks.values()):
        await task

    cleanup_events = []
    while not queue.empty():
        ev = queue.get_nowait()
        if ev.kind == EventKind.CLEANUP_REQUESTED:
            cleanup_events.append(ev)

    assert len(cleanup_events) == 1
    assert cleanup_events[0].payload == {"published_prs": []}


@pytest.mark.asyncio
async def test_accepted_pr_failure_still_emits_cleanup(tmp_path, monkeypatch):
    """If PR publish raises after the user accepts, cleanup is still offered
    (after PR_FAILED)."""
    mgr = SessionManager()
    sess = _make_session({OPT_MAKE_DRAFT_PR: True, OPT_SELF_CLEANUP: True}, tmp_path)
    mgr.sessions[sess.state.id] = sess
    monkeypatch.setattr(mgr, "_persist", lambda: None)

    async def boom(state, title=None, body=None):
        raise RuntimeError("publish exploded")

    monkeypatch.setattr(pr_mod, "publish_draft_prs", boom)

    queue = mgr.subscribe()
    await mgr._handle_event(_done_event(sess.state.id), sess)
    while not queue.empty():
        queue.get_nowait()

    await mgr.submit_pr_review(sess.state.id, accepted=True)
    for task in list(mgr._pr_tasks.values()):
        await task

    kinds: list[EventKind] = []
    while not queue.empty():
        kinds.append(queue.get_nowait().kind)

    assert kinds.count(EventKind.CLEANUP_REQUESTED) == 1
    assert EventKind.PR_FAILED in kinds
    assert kinds.index(EventKind.PR_FAILED) < kinds.index(EventKind.CLEANUP_REQUESTED)
