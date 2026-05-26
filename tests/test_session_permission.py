"""Tests for AgentSession's per-tool permission flow.

Two modes route through this flow:

- ``default`` mode uses the SDK's ``can_use_tool`` callback (chud's
  ``_on_tool_request``).
- ``low_perms`` mode uses the PreToolUse hook (chud's
  ``_on_pre_tool_use_hook``).

Both share ``_await_permission`` to surface a ``PERMISSION_REQUESTED``
event, block on a future, and resolve via ``approve_tool`` / ``deny_tool``.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, cast

from claude_agent_sdk.types import PermissionResultAllow, PermissionResultDeny

from chud.session import AgentSession
from chud.types import EventKind, SessionState, SessionStatus, Worktree


class FakeClient:
    """Captures ``set_permission_mode`` calls for the approve-plan tests."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def connect(self) -> None:
        pass

    async def disconnect(self) -> None:
        pass

    async def interrupt(self) -> None:
        pass

    async def query(self, *_a, **_kw) -> None:
        pass

    async def set_permission_mode(self, mode: str) -> None:
        self.calls.append(f"set_permission_mode:{mode}")

    async def receive_messages(self):
        await asyncio.Event().wait()
        if False:
            yield  # pragma: no cover


def _make_session(
    run_mode: str,
    *,
    status: SessionStatus = SessionStatus.EXECUTING,
    worktree_path: Path | None = None,
) -> AgentSession:
    """Build a session with an attached worktree so the filesystem gate
    (which runs before the permission branch) doesn't pre-empt the test.

    ``worktree_path`` lets callers pass a tmp path; if unset we use ``/`` so
    any absolute file_path falls within it.
    """
    state = SessionState(id="t-perm", run_mode=run_mode)
    state.status = status
    wt_root = worktree_path if worktree_path is not None else Path("/")
    state.attached_repos["fake"] = Worktree(
        repo_path=Path("/tmp/repo"),
        worktree_path=wt_root,
        branch="chud/t-perm",
    )
    return AgentSession(state, run_mode=run_mode)


async def _drain(events: asyncio.Queue, kind: EventKind, timeout: float = 1.0) -> dict:
    """Pull events until we hit ``kind``, or fail on timeout."""
    deadline = asyncio.get_event_loop().time() + timeout
    while True:
        remaining = deadline - asyncio.get_event_loop().time()
        if remaining <= 0:
            raise AssertionError(f"timed out waiting for {kind}")
        ev = await asyncio.wait_for(events.get(), timeout=remaining)
        if ev.kind == kind:
            return ev.payload


# ---------------------------------------------------------------- default mode


async def test_default_mode_intercepts_can_use_tool_and_approves():
    sess = _make_session("default")
    tool_input = {"file_path": "/tmp/foo.py", "old_string": "a", "new_string": "b"}

    request_task = asyncio.create_task(
        sess._on_tool_request("Edit", tool_input, context=None)  # type: ignore[arg-type]
    )
    payload = await _drain(sess.events, EventKind.PERMISSION_REQUESTED)
    assert payload["tool_name"] == "Edit"
    assert payload["tool_input"] == tool_input
    assert sess._permission_decision is not None and not sess._permission_decision.done()

    await sess.approve_tool()
    result = await asyncio.wait_for(request_task, timeout=1.0)
    assert isinstance(result, PermissionResultAllow)
    assert sess._permission_decision is None
    assert sess._pending_permission is None


async def test_default_mode_intercepts_can_use_tool_and_denies_with_reason():
    sess = _make_session("default")
    request_task = asyncio.create_task(
        sess._on_tool_request("Edit", {"file_path": "/x"}, context=None)  # type: ignore[arg-type]
    )
    await _drain(sess.events, EventKind.PERMISSION_REQUESTED)

    await sess.deny_tool(reason="too risky")
    result = await asyncio.wait_for(request_task, timeout=1.0)
    assert isinstance(result, PermissionResultDeny)
    assert result.message == "too risky"


# ---------------------------------------------------------------- low_perms mode


async def test_low_perms_mode_intercepts_pre_tool_use_hook_and_approves():
    sess = _make_session("low_perms")

    request_task = asyncio.create_task(
        sess._on_pre_tool_use_hook(
            {"tool_name": "Edit", "tool_input": {"file_path": "/y"}},  # type: ignore[arg-type]
            tool_use_id=None,
            context=None,  # type: ignore[arg-type]
        )
    )
    payload = await _drain(sess.events, EventKind.PERMISSION_REQUESTED)
    assert payload["tool_name"] == "Edit"

    await sess.approve_tool()
    out = await asyncio.wait_for(request_task, timeout=1.0)
    spec = cast(dict[str, Any], out)["hookSpecificOutput"]
    assert spec["permissionDecision"] == "allow"


async def test_low_perms_mode_intercepts_pre_tool_use_hook_and_denies_with_reason():
    sess = _make_session("low_perms")

    request_task = asyncio.create_task(
        sess._on_pre_tool_use_hook(
            {"tool_name": "Bash", "tool_input": {"command": "rm -rf /"}},  # type: ignore[arg-type]
            tool_use_id=None,
            context=None,  # type: ignore[arg-type]
        )
    )
    await _drain(sess.events, EventKind.PERMISSION_REQUESTED)

    await sess.deny_tool(reason="nope, dangerous")
    out = await asyncio.wait_for(request_task, timeout=1.0)
    spec = cast(dict[str, Any], out)["hookSpecificOutput"]
    assert spec["permissionDecision"] == "deny"
    assert spec["permissionDecisionReason"] == "nope, dangerous"


async def test_low_perms_only_gates_dangerous_tools():
    """Read-only tools must pass through ``{}`` in low_perms without an event."""
    sess = _make_session("low_perms")

    out = await sess._on_pre_tool_use_hook(
        {"tool_name": "Read", "tool_input": {"file_path": "/x"}},  # type: ignore[arg-type]
        tool_use_id=None,
        context=None,  # type: ignore[arg-type]
    )
    assert out == {}
    assert sess.events.empty()
    assert sess._permission_decision is None


# ---------------------------------------------------------------- full_auto


async def test_full_auto_skips_intercepts():
    sess = _make_session("full_auto")

    # can_use_tool allows immediately, no event, no future.
    result = await sess._on_tool_request("Edit", {"file_path": "/x"}, context=None)  # type: ignore[arg-type]
    assert isinstance(result, PermissionResultAllow)
    assert sess._permission_decision is None
    assert sess.events.empty()

    # PreToolUse passes through with {} (no fs gate violation; no low_perms gate).
    out = await sess._on_pre_tool_use_hook(
        {"tool_name": "Edit", "tool_input": {"file_path": "/x"}},  # type: ignore[arg-type]
        tool_use_id=None,
        context=None,  # type: ignore[arg-type]
    )
    assert out == {}
    assert sess.events.empty()


# ---------------------------------------------------------------- status gating


async def test_permission_skipped_during_planning_in_default_mode():
    sess = _make_session("default", status=SessionStatus.PLANNING)
    result = await sess._on_tool_request("Edit", {"file_path": "/x"}, context=None)  # type: ignore[arg-type]
    assert isinstance(result, PermissionResultAllow)
    assert sess._permission_decision is None
    assert sess.events.empty()


async def test_permission_skipped_during_planning_in_low_perms_mode():
    sess = _make_session("low_perms", status=SessionStatus.PLANNING)
    out = await sess._on_pre_tool_use_hook(
        {"tool_name": "Edit", "tool_input": {"file_path": "/x"}},  # type: ignore[arg-type]
        tool_use_id=None,
        context=None,  # type: ignore[arg-type]
    )
    assert out == {}
    assert sess._permission_decision is None
    assert sess.events.empty()


# ---------------------------------------------------------------- approve_plan SDK mode


async def test_approve_plan_sets_acceptedits_in_full_auto_mode():
    sess = _make_session("full_auto")
    fake = FakeClient()
    sess._client = fake  # type: ignore[assignment]
    sess._plan_decision = asyncio.get_event_loop().create_future()

    await sess.approve_plan()

    assert "set_permission_mode:acceptEdits" in fake.calls


async def test_approve_plan_sets_default_in_default_mode():
    sess = _make_session("default")
    fake = FakeClient()
    sess._client = fake  # type: ignore[assignment]
    sess._plan_decision = asyncio.get_event_loop().create_future()

    await sess.approve_plan()

    assert "set_permission_mode:default" in fake.calls


async def test_approve_plan_sets_acceptedits_in_low_perms_mode():
    """low_perms gates via PreToolUse, so the SDK still runs in acceptEdits
    after plan approval — otherwise the SDK would also try to prompt for
    dangerous tools and we'd double-prompt the user."""
    sess = _make_session("low_perms")
    fake = FakeClient()
    sess._client = fake  # type: ignore[assignment]
    sess._plan_decision = asyncio.get_event_loop().create_future()

    await sess.approve_plan()

    assert "set_permission_mode:acceptEdits" in fake.calls


# ---------------------------------------------------------------- stop / supersede


async def test_stop_resolves_in_flight_permission_decision():
    sess = _make_session("default")
    sess._client = FakeClient()  # type: ignore[assignment]
    sess._permission_decision = asyncio.get_event_loop().create_future()
    sess._pending_permission = {"tool_name": "Edit", "tool_input": {}}

    pending = sess._permission_decision
    await sess.stop()

    assert pending.done()
    result = pending.result()
    assert isinstance(result, PermissionResultDeny)
    assert result.message == "Session stopped."
    assert sess._permission_decision is None
    assert sess._pending_permission is None


async def test_concurrent_permission_request_supersedes_prior():
    """If two requests race, the first is resolved with Deny('superseded')."""
    sess = _make_session("default")

    first_task = asyncio.create_task(
        sess._on_tool_request("Edit", {"file_path": "/a"}, context=None)  # type: ignore[arg-type]
    )
    await _drain(sess.events, EventKind.PERMISSION_REQUESTED)
    first_future = sess._permission_decision
    assert first_future is not None

    second_task = asyncio.create_task(
        sess._on_tool_request("Edit", {"file_path": "/b"}, context=None)  # type: ignore[arg-type]
    )
    await _drain(sess.events, EventKind.PERMISSION_REQUESTED)

    # First future now resolved with the supersede deny; first awaiter sees it.
    first_result = await asyncio.wait_for(first_task, timeout=1.0)
    assert isinstance(first_result, PermissionResultDeny)
    assert first_result.message == "superseded"

    # Resolve the second one normally.
    await sess.approve_tool()
    second_result = await asyncio.wait_for(second_task, timeout=1.0)
    assert isinstance(second_result, PermissionResultAllow)


# ---------------------------------------------------------------- noop guards


async def test_approve_tool_with_no_pending_decision_is_noop():
    sess = _make_session("default")
    await sess.approve_tool()
    assert sess._permission_decision is None


async def test_deny_tool_with_no_pending_decision_is_noop():
    sess = _make_session("default")
    await sess.deny_tool("nope")
    assert sess._permission_decision is None


# ---------------------------------------------------------------- deny → AWAITING_USER routing


def _result_message() -> Any:
    """Minimal ResultMessage stand-in matching the SDK's shape (all fields
    that the dataclass requires). Reading isn't needed — only ``isinstance``
    in ``_dispatch_message`` cares."""
    from claude_agent_sdk import ResultMessage

    return ResultMessage(
        subtype="",
        duration_ms=0,
        duration_api_ms=0,
        is_error=False,
        num_turns=0,
        session_id="t-perm",
    )


async def test_result_message_after_deny_routes_to_awaiting_user_not_done():
    """A denied edit must not collapse the session into DONE — otherwise the
    PR / cleanup pipeline fires on an aborted run. Park in AWAITING_USER so
    the user can either reply or kill the session explicitly."""
    sess = _make_session("default")
    sess._permission_decision = asyncio.get_event_loop().create_future()
    await sess.deny_tool("blocking this one")
    assert sess._deny_pending is True

    await sess._dispatch_message(_result_message())

    assert sess.state.status == SessionStatus.AWAITING_USER
    assert sess._deny_pending is False

    # Sanity: a NEEDS_USER_INPUT event was emitted so the UI surfaces a
    # prompt for the user.
    saw_input = False
    while not sess.events.empty():
        ev = sess.events.get_nowait()
        if ev.kind == EventKind.NEEDS_USER_INPUT:
            saw_input = True
            break
    assert saw_input


async def test_send_message_clears_deny_flag():
    """A user-supplied follow-up resets the turn — the next ResultMessage
    represents a fresh agent loop and should route to DONE normally."""

    class _SilentClient:
        async def query(self, *_a: Any, **_kw: Any) -> None:
            pass

    sess = _make_session("default")
    sess._client = _SilentClient()  # type: ignore[assignment]
    sess._deny_pending = True

    await sess.send_message("try this instead")
    assert sess._deny_pending is False

    await sess._dispatch_message(_result_message())
    assert sess.state.status == SessionStatus.DONE
