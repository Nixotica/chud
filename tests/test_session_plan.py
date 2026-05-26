"""Plan-flow tests for AgentSession.

Covers:

* Empty/malformed plans submitted via ``ExitPlanMode`` are denied at the
  permission layer without surfacing a blank plan modal.
* Non-empty plans go through the normal modal-gating flow.
* A ``ResultMessage`` arriving before any plan was approved leaves the session
  in ``AWAITING_USER`` rather than terminal ``DONE``; a post-approval one still
  terminates in ``DONE``.
* Plan rejection interrupts the planning turn and replays any typed feedback as
  a real user ``query()`` message — the model ignores feedback delivered through
  the ``ExitPlanMode`` deny channel (it reads as untrusted tool output), so
  re-engaging via a genuine user turn is what actually revises the plan.
"""

from __future__ import annotations

import asyncio

import pytest
from claude_agent_sdk import ResultMessage
from claude_agent_sdk.types import PermissionResultAllow, PermissionResultDeny

from chud.session import AgentSession
from chud.types import EventKind, SessionState, SessionStatus


def _make_session(status: SessionStatus = SessionStatus.PLANNING) -> AgentSession:
    state = SessionState(id="t-plan")
    state.status = status
    return AgentSession(state)


def _make_result_message() -> ResultMessage:
    return ResultMessage(
        subtype="success",
        duration_ms=0,
        duration_api_ms=0,
        is_error=False,
        num_turns=1,
        session_id="t-plan",
    )


async def _drain(sess: AgentSession) -> list:
    out = []
    while not sess.events.empty():
        out.append(await sess.events.get())
    return out


# ---------------------------------------------------------------- empty-plan deny


@pytest.mark.parametrize("plan", ["", "   ", "\n\t  \n"])
async def test_exit_plan_mode_empty_plan_is_denied(plan: str):
    """An empty/whitespace plan is denied without transitioning state or
    surfacing a plan modal to the UI."""
    sess = _make_session()
    result = await sess._on_tool_request(
        "ExitPlanMode",
        {"plan": plan},
        context=None,  # type: ignore[arg-type]
    )
    assert isinstance(result, PermissionResultDeny)
    assert "non-empty plan" in result.message
    assert sess.state.status == SessionStatus.PLANNING
    assert sess._plan_decision is None
    assert sess._pending_plan_text is None
    events = await _drain(sess)
    assert not any(e.kind == EventKind.PLAN_PROPOSED for e in events)


async def test_exit_plan_mode_missing_plan_key_is_denied():
    """Defensive: a malformed call with no `plan` argument is also denied."""
    sess = _make_session()
    result = await sess._on_tool_request(
        "ExitPlanMode",
        {},
        context=None,  # type: ignore[arg-type]
    )
    assert isinstance(result, PermissionResultDeny)
    assert "non-empty plan" in result.message


# ---------------------------------------------------------------- happy path


async def test_exit_plan_mode_non_empty_plan_awaits_decision():
    """A real plan still gates on the approval future and emits PLAN_PROPOSED."""
    sess = _make_session()

    task = asyncio.create_task(
        sess._on_tool_request(
            "ExitPlanMode",
            {"plan": "1. do the thing\n2. do the other thing"},
            context=None,  # type: ignore[arg-type]
        )
    )

    for _ in range(10):
        await asyncio.sleep(0)
        if sess._plan_decision is not None:
            break

    assert sess._plan_decision is not None and not sess._plan_decision.done()
    assert sess.state.status == SessionStatus.AWAITING_PLAN_APPROVAL
    assert sess._pending_plan_text == "1. do the thing\n2. do the other thing"

    events = await _drain(sess)
    proposed = [e for e in events if e.kind == EventKind.PLAN_PROPOSED]
    assert len(proposed) == 1
    assert proposed[0].payload["plan"] == "1. do the thing\n2. do the other thing"

    sess._plan_decision.set_result(PermissionResultAllow())
    result = await task
    assert isinstance(result, PermissionResultAllow)


# ---------------------------------------------------------------- DONE gating


@pytest.mark.parametrize(
    "status",
    [
        SessionStatus.NEW,
        SessionStatus.PLANNING,
        SessionStatus.AWAITING_PLAN_APPROVAL,
    ],
)
async def test_result_message_before_plan_approval_becomes_awaiting_user(
    status: SessionStatus,
):
    """If the agent ends its turn before a plan was ever approved, surface
    AWAITING_USER so the user can prompt again — not terminal DONE (which would
    fire the auto-PR / cleanup pipeline on a session that never executed)."""
    sess = _make_session(status=status)
    await sess._dispatch_message(_make_result_message())
    assert sess.state.status == SessionStatus.AWAITING_USER


@pytest.mark.parametrize("status", [SessionStatus.EXECUTING, SessionStatus.AWAITING_USER])
async def test_result_message_after_plan_approval_becomes_done(
    status: SessionStatus,
):
    """Regression guard: once a plan was approved, ResultMessage still
    terminates the session in DONE."""
    sess = _make_session(status=status)
    await sess._dispatch_message(_make_result_message())
    assert sess.state.status == SessionStatus.DONE


# ---------------------------------------------------------------- rejection re-engagement


class _RecordingClient:
    """Captures query() calls so a rejection's feedback replay is observable."""

    def __init__(self) -> None:
        self.queries: list[str] = []

    async def query(self, text: str) -> None:
        self.queries.append(text)


async def _await_plan_decision(sess: AgentSession, plan: str) -> asyncio.Task:
    """Drive ExitPlanMode interception until the approval future is pending."""
    task = asyncio.create_task(
        sess._on_tool_request("ExitPlanMode", {"plan": plan}, context=None)  # type: ignore[arg-type]
    )
    for _ in range(10):
        await asyncio.sleep(0)
        if sess._plan_decision is not None:
            break
    assert sess._plan_decision is not None
    return task


async def test_reject_plan_with_feedback_interrupts_then_replays_as_user_message():
    """The deny carries interrupt=True (so the model can't re-propose the same
    plan within the turn), and once that turn ends the feedback is replayed as a
    genuine query() user message — never via the model-distrusted deny channel —
    leaving the session planning."""
    sess = _make_session()
    client = _RecordingClient()
    sess._client = client  # type: ignore[assignment]
    task = await _await_plan_decision(sess, "1. do X")

    await sess.reject_plan(reason="use a different library")
    result = await task
    assert isinstance(result, PermissionResultDeny)
    assert result.interrupt is True
    assert sess._pending_plan_feedback == "use a different library"
    assert sess._deny_pending is False

    await sess._dispatch_message(_make_result_message())

    assert client.queries == ["use a different library"]
    assert sess._pending_plan_feedback is None
    assert sess.state.status == SessionStatus.PLANNING


async def test_reject_plan_without_feedback_awaits_user_followup():
    """A bare reject (no modal text) has nothing to replay, so it parks the
    session in AWAITING_USER and surfaces a NEEDS_USER_INPUT prompt."""
    sess = _make_session()
    task = await _await_plan_decision(sess, "1. do X")

    await sess.reject_plan()
    await task
    assert sess._pending_plan_feedback is None
    assert sess._deny_pending is True

    await sess._dispatch_message(_make_result_message())

    assert sess.state.status == SessionStatus.AWAITING_USER
    assert sess._deny_pending is False
    events = await _drain(sess)
    assert any(e.kind == EventKind.NEEDS_USER_INPUT for e in events)


async def test_input_box_followup_during_planning_resumes_planning_not_executing():
    """A follow-up message sent while no plan is approved (e.g. after a bare
    reject) must resume PLANNING, not EXECUTING — otherwise the post-approval
    filesystem gate activates and blocks the CLI's own plan-file edits, so the
    agent can't revise the plan."""
    sess = _make_session(status=SessionStatus.AWAITING_USER)
    client = _RecordingClient()
    sess._client = client  # type: ignore[assignment]
    assert sess.state.approved_plan is None

    await sess.send_message("make the greeting French")

    assert sess.state.status == SessionStatus.PLANNING
    assert client.queries == ["make the greeting French"]


async def test_input_box_followup_after_approval_resumes_executing():
    """Once a plan is approved, a follow-up resumes EXECUTING as before."""
    sess = _make_session(status=SessionStatus.AWAITING_USER)
    sess._client = _RecordingClient()  # type: ignore[assignment]
    sess.state.approved_plan = "approved plan"

    await sess.send_message("keep going")

    assert sess.state.status == SessionStatus.EXECUTING
