"""Shutdown-ordering tests for AgentSession.stop().

The Claude Code subprocess speaks to the SDK over a control stream. If chud
tears that stream down while a hook callback round-trip is in flight, Claude
logs `Error in hook callback hook_0: Stream closed`. The fix in stop() is to:

  1. Resolve any pending plan-approval future with a deny so the SDK's
     can_use_tool round-trip completes.
  2. interrupt() the session so Claude stops issuing new control requests.
  3. Then cancel the reader and disconnect.

These tests use a fake ClaudeSDKClient to verify ordering and that stop() is
safe in the no-pending-decision case.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from chud.session import AgentSession
from chud.types import SessionState, SessionStatus


class FakeClient:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.connected = False

    async def connect(self) -> None:
        self.connected = True
        self.calls.append("connect")

    async def disconnect(self) -> None:
        self.calls.append("disconnect")
        self.connected = False

    async def interrupt(self) -> None:
        self.calls.append("interrupt")

    async def query(self, *_a, **_kw) -> None:
        self.calls.append("query")

    async def set_permission_mode(self, mode: str) -> None:
        self.calls.append(f"set_permission_mode:{mode}")

    async def receive_messages(self):
        # Idle reader — yields nothing, blocks until cancelled.
        await asyncio.Event().wait()
        if False:
            yield  # pragma: no cover


def _make_session() -> AgentSession:
    return AgentSession(
        SessionState(id="t1", workspace_dir=Path("/tmp/chud-test/t1")),
    )


async def test_stop_resolves_pending_plan_decision_with_deny():
    """If we tear down while awaiting a plan decision, the future must be resolved
    so the SDK's can_use_tool callback returns instead of waiting forever."""
    sess = _make_session()
    sess._client = FakeClient()  # type: ignore[assignment]
    sess._plan_decision = asyncio.get_event_loop().create_future()

    pending = sess._plan_decision

    await sess.stop()

    assert pending.done()
    result = pending.result()
    # PermissionResultDeny has a `message` field.
    assert getattr(result, "message", None) == "Session stopped."
    assert sess._plan_decision is None
    assert sess._pending_plan_text is None


async def test_stop_calls_interrupt_before_disconnect():
    sess = _make_session()
    fake = FakeClient()
    sess._client = fake  # type: ignore[assignment]

    await sess.stop()

    assert "interrupt" in fake.calls
    assert "disconnect" in fake.calls
    assert fake.calls.index("interrupt") < fake.calls.index("disconnect")


async def test_stop_is_safe_with_no_pending_decision():
    sess = _make_session()
    fake = FakeClient()
    sess._client = fake  # type: ignore[assignment]

    await sess.stop()

    assert sess._client is None
    assert sess._reader_task is None
    assert fake.calls == ["interrupt", "disconnect"]


async def test_stop_swallows_interrupt_errors():
    """If the subprocess is already gone, interrupt() may raise — stop() must
    still proceed to disconnect and clear state."""

    class BrokenInterruptClient(FakeClient):
        async def interrupt(self) -> None:
            self.calls.append("interrupt")
            raise RuntimeError("subprocess gone")

    sess = _make_session()
    fake = BrokenInterruptClient()
    sess._client = fake  # type: ignore[assignment]

    await sess.stop()

    assert fake.calls == ["interrupt", "disconnect"]
    assert sess._client is None


async def test_stop_cancels_reader_task():
    sess = _make_session()
    sess._client = FakeClient()  # type: ignore[assignment]

    async def reader() -> None:
        await asyncio.Event().wait()

    sess._reader_task = asyncio.create_task(reader())
    await asyncio.sleep(0)  # let it start

    await sess.stop()

    assert sess._reader_task is None


async def test_stop_is_idempotent():
    sess = _make_session()
    sess._client = FakeClient()  # type: ignore[assignment]

    await sess.stop()
    # Second call: no client, no reader, no pending decision — must not raise.
    await sess.stop()


async def test_stop_with_done_plan_decision_does_not_overwrite():
    """If the user already approved/rejected, don't clobber the result."""
    sess = _make_session()
    sess._client = FakeClient()  # type: ignore[assignment]
    fut: asyncio.Future = asyncio.get_event_loop().create_future()
    fut.set_result("already-decided")
    sess._plan_decision = fut

    await sess.stop()

    assert fut.result() == "already-decided"


@pytest.mark.parametrize(
    "status",
    [SessionStatus.PLANNING, SessionStatus.AWAITING_PLAN_APPROVAL, SessionStatus.EXECUTING],
)
async def test_stop_works_in_any_active_status(status: SessionStatus):
    sess = _make_session()
    sess.state.status = status
    sess._client = FakeClient()  # type: ignore[assignment]
    if status == SessionStatus.AWAITING_PLAN_APPROVAL:
        sess._plan_decision = asyncio.get_event_loop().create_future()

    await sess.stop()
    assert sess._client is None
