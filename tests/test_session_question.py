"""Tests for AgentSession's AskUserQuestion interception flow.

When the inner agent calls the AskUserQuestion harness builtin, the SDK has no
implementation to fulfill it — so chud intercepts the permission callback,
emits a QUESTION_ASKED event for the UI, and waits for ``answer_question()``
to deliver the user's response back to the model via PermissionResultDeny.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from claude_agent_sdk.types import PermissionResultDeny

from chud.session import AgentSession
from chud.types import EventKind, SessionState, SessionStatus


def _make_session(tmp_path: Path) -> AgentSession:
    del tmp_path
    state = SessionState(id="t-question")
    state.status = SessionStatus.EXECUTING
    # EXECUTING is only reachable after plan approval, so the state carries an
    # approved plan — answering a question then resumes EXECUTING (not PLANNING).
    state.approved_plan = "approved plan"
    return AgentSession(state)


async def _drain(events: asyncio.Queue, kind: EventKind, timeout: float = 1.0) -> dict:
    """Pull events until we hit the requested kind, or fail on timeout."""
    deadline = asyncio.get_event_loop().time() + timeout
    while True:
        remaining = deadline - asyncio.get_event_loop().time()
        if remaining <= 0:
            raise AssertionError(f"timed out waiting for {kind}")
        ev = await asyncio.wait_for(events.get(), timeout=remaining)
        if ev.kind == kind:
            return ev.payload


async def test_ask_user_question_intercept_and_answer(tmp_path):
    sess = _make_session(tmp_path)
    tool_input = {
        "questions": [
            {
                "question": "Pick one",
                "options": [{"label": "A"}, {"label": "B"}],
                "multiSelect": False,
            }
        ]
    }

    # Kick off the permission callback. It will block on the future until we
    # call answer_question(); race a task that resolves it.
    request_task = asyncio.create_task(
        sess._on_tool_request("AskUserQuestion", tool_input, context=None)  # type: ignore[arg-type]
    )

    payload = await _drain(sess.events, EventKind.QUESTION_ASKED)
    assert payload["input"] == tool_input
    assert sess.state.status == SessionStatus.AWAITING_USER
    assert sess._question_decision is not None
    assert not sess._question_decision.done()

    await sess.answer_question("Pick one: A")

    result = await asyncio.wait_for(request_task, timeout=1.0)
    assert isinstance(result, PermissionResultDeny)
    assert result.message == "Pick one: A"
    assert sess.state.status == SessionStatus.EXECUTING
    assert sess._question_decision is None
    assert sess._pending_question_input is None


async def test_answer_question_with_no_pending_decision_is_noop(tmp_path):
    sess = _make_session(tmp_path)
    # No future scheduled — should warn and return without raising.
    await sess.answer_question("nothing pending")
    assert sess._question_decision is None


async def test_stop_resolves_in_flight_question_decision(tmp_path):
    """Tearing down a session mid-question must not strand the SDK round-trip."""
    sess = _make_session(tmp_path)
    sess._question_decision = asyncio.get_event_loop().create_future()
    sess._pending_question_input = {"questions": []}

    await sess.stop()

    assert sess._question_decision is None
    assert sess._pending_question_input is None


async def test_dismiss_path_still_unblocks_agent(tmp_path):
    """The app falls back to a sentinel string on cancel; verify the future
    still resolves and the model gets a non-empty message back."""
    sess = _make_session(tmp_path)
    request_task = asyncio.create_task(
        sess._on_tool_request("AskUserQuestion", {"questions": []}, context=None)  # type: ignore[arg-type]
    )
    await _drain(sess.events, EventKind.QUESTION_ASKED)

    await sess.answer_question("(user dismissed the question without answering)")

    result = await asyncio.wait_for(request_task, timeout=1.0)
    assert isinstance(result, PermissionResultDeny)
    assert "dismissed" in result.message
