"""Plan-flow tests for AgentSession.

Covers:

* Empty plans submitted via ``ExitPlanMode`` are denied at the permission
  layer without transitioning the session into ``AWAITING_PLAN_APPROVAL`` —
  the agent never gets to surface a blank plan modal to the user.
* Non-empty plans still go through the normal modal-gating flow.
* A ``ResultMessage`` arriving before any plan was approved (e.g. the agent
  gives up after a plan rejection) leaves the session in ``AWAITING_USER``
  rather than transitioning to terminal ``DONE``, so the user can re-engage.
* Post-approval ``ResultMessage`` still terminates the session in ``DONE``.

See GitHub issue #64.
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
    """An empty or whitespace-only plan must not reach the approval modal."""
    sess = _make_session()
    result = await sess._on_tool_request(
        "ExitPlanMode",
        {"plan": plan},
        context=None,  # type: ignore[arg-type]
    )
    assert isinstance(result, PermissionResultDeny)
    assert "non-empty plan" in result.message


async def test_exit_plan_mode_empty_plan_does_not_transition_state():
    """Empty-plan deny must leave status at PLANNING and not stash a future."""
    sess = _make_session()
    await sess._on_tool_request(
        "ExitPlanMode",
        {"plan": ""},
        context=None,  # type: ignore[arg-type]
    )
    assert sess.state.status == SessionStatus.PLANNING
    assert sess._plan_decision is None
    assert sess._pending_plan_text is None


async def test_exit_plan_mode_empty_plan_emits_no_plan_proposed_event():
    """No PLAN_PROPOSED event — the UI should never see an empty plan."""
    sess = _make_session()
    await sess._on_tool_request(
        "ExitPlanMode",
        {"plan": ""},
        context=None,  # type: ignore[arg-type]
    )
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
    AWAITING_USER so the user can prompt again — not terminal DONE.

    Regression test for GitHub issue #64: rejecting a plan and getting a
    ResultMessage shortly after used to move the session to DONE, hiding the
    fact that the user could still re-engage.
    """
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
