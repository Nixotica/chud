"""FIFO queueing of blocking user-input prompts in ChudApp.

These tests verify that when multiple agent sessions emit blocking prompts
(PLAN_PROPOSED, CLEANUP_REQUESTED, NEEDS_USER_INPUT), the first prompt stays
visible until the user answers it, then the next one appears — instead of the
latest modal covering whichever the user hasn't answered yet.

We don't spin up real ClaudeSDKClient instances; we manually populate
``app.manager.sessions`` with synthetic ``AgentSession`` objects so the queue
dispatcher's ``manager.sessions.get(sid)`` checks pass, then drive
``app._on_event`` directly.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from chud.app import ChudApp
from chud.session import AgentSession
from chud.types import Event, EventKind, SessionState, SessionStatus
from chud.widgets.cleanup_confirmation_modal import CleanupConfirmationModal
from chud.widgets.plan_modal import PlanApprovalModal


def _register_session(app: ChudApp, sid: str, tmp_path: Path) -> AgentSession:
    """Drop a stub AgentSession into the manager so queue checks pass.

    Queue logic only inspects ``app.manager.sessions`` — we don't need the
    SessionListView row or a real SDK client for these tests.
    """
    state = SessionState(
        id=sid,
        workspace_dir=tmp_path / sid,
        status=SessionStatus.EXECUTING,
        initial_prompt=f"prompt {sid}",
    )
    state.workspace_dir.mkdir(parents=True, exist_ok=True)
    sess = AgentSession(state, model=None)
    app.manager.sessions[sid] = sess
    return sess


def _plan_event(sid: str, plan: str = "do the thing") -> Event:
    return Event(
        session_id=sid,
        kind=EventKind.PLAN_PROPOSED,
        payload={"plan": plan},
    )


def _input_event(sid: str, message: str = "what now?") -> Event:
    return Event(
        session_id=sid,
        kind=EventKind.NEEDS_USER_INPUT,
        payload={"reason": "notification", "message": message},
    )


def _cleanup_event(sid: str) -> Event:
    return Event(session_id=sid, kind=EventKind.CLEANUP_REQUESTED)


def _status_event(sid: str, status: SessionStatus) -> Event:
    return Event(
        session_id=sid,
        kind=EventKind.STATUS_CHANGED,
        payload={"status": status.value},
    )


def _count_modals(app: ChudApp, modal_cls: type) -> int:
    return sum(1 for s in app.screen_stack if isinstance(s, modal_cls))


@pytest.mark.asyncio
async def test_two_plan_proposed_events_queue_modals_one_at_a_time(tmp_path):
    app = ChudApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        _register_session(app, "sess-a", tmp_path)
        _register_session(app, "sess-b", tmp_path)

        app._on_event(_plan_event("sess-a", "plan A"))
        app._on_event(_plan_event("sess-b", "plan B"))
        await pilot.pause()

        # Only one plan modal should be on the stack; the other waits in queue.
        assert _count_modals(app, PlanApprovalModal) == 1
        assert app._prompt_active is not None
        assert app._prompt_active.session_id == "sess-a"
        assert [p.session_id for p in app._prompt_queue] == ["sess-b"]

        # The visible modal is for sess-a (the first to ask).
        modal = app.screen
        assert isinstance(modal, PlanApprovalModal)
        assert modal.session_id == "sess-a"

        # Approve sess-a's plan via the "a" key. After it resolves, the queued
        # sess-b modal should appear automatically.
        await pilot.press("a")
        await pilot.pause()
        await pilot.pause()  # give the worker an extra tick to advance the queue

        assert _count_modals(app, PlanApprovalModal) == 1
        modal2 = app.screen
        assert isinstance(modal2, PlanApprovalModal)
        assert modal2.session_id == "sess-b"
        assert app._prompt_queue == []


@pytest.mark.asyncio
async def test_duplicate_plan_proposed_for_same_session_enqueues_once(tmp_path):
    app = ChudApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        _register_session(app, "sess-a", tmp_path)

        app._on_event(_plan_event("sess-a"))
        app._on_event(_plan_event("sess-a"))  # duplicate
        app._on_event(_plan_event("sess-a"))  # duplicate
        await pilot.pause()

        assert _count_modals(app, PlanApprovalModal) == 1
        # Active is sess-a, queue should be empty (dedup against active).
        assert app._prompt_active is not None
        assert app._prompt_active.session_id == "sess-a"
        assert app._prompt_queue == []


@pytest.mark.asyncio
async def test_input_request_auto_selects_session_and_advances_on_reply(tmp_path):
    app = ChudApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        _register_session(app, "sess-a", tmp_path)
        _register_session(app, "sess-b", tmp_path)
        # Pre-select b so we can confirm the input event flips selection to a.
        app._select_session("sess-b")
        await pilot.pause()
        assert app._selected_session_id == "sess-b"

        app._on_event(_input_event("sess-a", "need clarification"))
        await pilot.pause()

        # NEEDS_USER_INPUT auto-selects the asking session.
        assert app._selected_session_id == "sess-a"
        assert app._prompt_active is not None
        assert app._prompt_active.kind == "input"
        assert app._prompt_active.session_id == "sess-a"

        # Now sess-b also asks. b should queue behind a.
        app._on_event(_input_event("sess-b", "and me too"))
        await pilot.pause()

        assert app._selected_session_id == "sess-a"  # still a
        assert [p.session_id for p in app._prompt_queue] == ["sess-b"]


@pytest.mark.asyncio
async def test_plan_then_input_for_different_session_queues_correctly(tmp_path):
    """Cross-kind FIFO: a plan modal in front, an input request behind it."""
    app = ChudApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        sess_a = _register_session(app, "sess-a", tmp_path)
        _register_session(app, "sess-b", tmp_path)

        # Stub the SDK calls that the plan modal would trigger on approve.
        async def noop_approve():
            return None

        async def noop_reject(reason: str | None = None):
            return None

        sess_a.approve_plan = noop_approve  # type: ignore[method-assign]
        sess_a.reject_plan = noop_reject  # type: ignore[method-assign]

        app._on_event(_plan_event("sess-a"))
        app._on_event(_input_event("sess-b"))
        await pilot.pause()

        assert _count_modals(app, PlanApprovalModal) == 1
        assert app._prompt_active.kind == "plan"
        assert app._prompt_active.session_id == "sess-a"
        assert [(p.kind, p.session_id) for p in app._prompt_queue] == [("input", "sess-b")]

        # Reject sess-a's plan (escape key). Then the input request should
        # become active and selection should auto-flip to sess-b.
        await pilot.press("escape")
        await pilot.pause()
        await pilot.pause()

        assert _count_modals(app, PlanApprovalModal) == 0
        assert app._prompt_active is not None
        assert app._prompt_active.kind == "input"
        assert app._prompt_active.session_id == "sess-b"
        assert app._selected_session_id == "sess-b"


@pytest.mark.asyncio
async def test_killed_session_with_queued_prompt_is_skipped(tmp_path):
    """If a session is killed while its prompt is queued (not active), the
    dispatcher must skip it when its turn comes — not show a stale modal."""
    app = ChudApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        _register_session(app, "sess-a", tmp_path)
        _register_session(app, "sess-b", tmp_path)
        _register_session(app, "sess-c", tmp_path)

        app._on_event(_plan_event("sess-a"))
        app._on_event(_plan_event("sess-b"))
        app._on_event(_plan_event("sess-c"))
        await pilot.pause()

        # a is active; b and c are queued.
        assert app._prompt_active.session_id == "sess-a"
        assert [p.session_id for p in app._prompt_queue] == ["sess-b", "sess-c"]

        # Drop sess-b directly from manager (simulates kill while queued) and
        # also via _drop_session_prompts so the queue is pruned.
        app.manager.sessions.pop("sess-b")
        app._drop_session_prompts("sess-b")

        # Resolve sess-a — the queue should jump to sess-c, skipping b.
        await pilot.press("escape")
        await pilot.pause()
        await pilot.pause()

        modal = app.screen
        assert isinstance(modal, PlanApprovalModal)
        assert modal.session_id == "sess-c"
        assert app._prompt_queue == []


@pytest.mark.asyncio
async def test_active_prompt_session_killed_advances_queue(tmp_path):
    """Killing the session whose prompt is currently active drops the active
    slot and immediately surfaces the next queued prompt."""
    app = ChudApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        _register_session(app, "sess-a", tmp_path)
        _register_session(app, "sess-b", tmp_path)

        app._on_event(_plan_event("sess-a"))
        app._on_event(_plan_event("sess-b"))
        await pilot.pause()

        assert app._prompt_active.session_id == "sess-a"

        # Simulate the active session being killed out from under the modal.
        app.manager.sessions.pop("sess-a")
        app._drop_session_prompts("sess-a")
        await pilot.pause()

        # Queue should have advanced to sess-b. (The sess-a modal screen may
        # still be on the stack until its worker unwinds, but the active
        # PromptRequest tracking should reflect sess-b.)
        assert app._prompt_active is not None
        assert app._prompt_active.session_id == "sess-b"
        assert app._prompt_queue == []


@pytest.mark.asyncio
async def test_status_change_to_done_drops_stale_input_prompt(tmp_path):
    """Regression: stop-hook NEEDS_USER_INPUT followed by ResultMessage DONE
    used to leave an unresolvable 'input' prompt active that blocked the
    post-DONE cleanup modal from ever appearing.
    """
    app = ChudApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        _register_session(app, "sess-a", tmp_path)

        # Stop-hook fires NEEDS_USER_INPUT — input prompt becomes active.
        app._on_event(_input_event("sess-a", "stop_hook"))
        await pilot.pause()
        assert app._prompt_active is not None
        assert app._prompt_active.kind == "input"
        assert app._prompt_active.session_id == "sess-a"

        # ResultMessage flips status to DONE. The stale input prompt has no
        # one to resolve it, so the queue should drop it now.
        app._on_event(_status_event("sess-a", SessionStatus.DONE))
        await pilot.pause()
        assert app._prompt_active is None
        assert app._prompt_queue == []

        # Cleanup broadcast that follows DONE should now surface a modal.
        app._on_event(_cleanup_event("sess-a"))
        await pilot.pause()
        await pilot.pause()
        assert _count_modals(app, CleanupConfirmationModal) == 1
        assert app._prompt_active is not None
        assert app._prompt_active.kind == "cleanup"


@pytest.mark.asyncio
async def test_status_change_to_executing_drops_stale_input_prompt(tmp_path):
    """A stop-hook input prompt should also clear if the agent recovers and
    transitions back to EXECUTING on its own.
    """
    app = ChudApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        _register_session(app, "sess-a", tmp_path)

        app._on_event(_input_event("sess-a"))
        await pilot.pause()
        assert app._prompt_active is not None
        assert app._prompt_active.kind == "input"

        app._on_event(_status_event("sess-a", SessionStatus.EXECUTING))
        await pilot.pause()
        assert app._prompt_active is None


@pytest.mark.asyncio
async def test_status_change_to_awaiting_user_keeps_input_prompt(tmp_path):
    """The drop only fires when the new status is *not* AWAITING_USER —
    a STATUS_CHANGED(awaiting_user) event must not eat its own input prompt.
    """
    app = ChudApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        _register_session(app, "sess-a", tmp_path)

        app._on_event(_status_event("sess-a", SessionStatus.AWAITING_USER))
        app._on_event(_input_event("sess-a"))
        await pilot.pause()
        assert app._prompt_active is not None
        assert app._prompt_active.kind == "input"


@pytest.mark.asyncio
async def test_cleanup_request_queues_behind_active_plan(tmp_path):
    app = ChudApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        _register_session(app, "sess-a", tmp_path)
        _register_session(app, "sess-b", tmp_path)

        app._on_event(_plan_event("sess-a"))
        app._on_event(_cleanup_event("sess-b"))
        await pilot.pause()

        assert app._prompt_active.kind == "plan"
        assert app._prompt_active.session_id == "sess-a"
        assert [(p.kind, p.session_id) for p in app._prompt_queue] == [("cleanup", "sess-b")]
        assert _count_modals(app, PlanApprovalModal) == 1
        assert _count_modals(app, CleanupConfirmationModal) == 0
