from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ToolUseBlock,
    UserMessage,
)
from claude_agent_sdk.types import (
    HookMatcher,
    PermissionResultAllow,
    PermissionResultDeny,
    ToolPermissionContext,
)

from chud.types import Event, EventKind, SessionState, SessionStatus

log = logging.getLogger(__name__)


class AgentSession:
    """Wraps one ClaudeSDKClient and runs the chud state machine.

    Lifecycle:
      NEW -> PLANNING -> AWAITING_PLAN_APPROVAL -> EXECUTING <-> AWAITING_USER -> DONE | ERRORED

    Events are pushed to `self.events` (async queue); the SessionManager drains and fans
    them out to UI subscribers. User input is delivered back via `send_message()` and
    `approve_plan()` / `reject_plan()`.
    """

    def __init__(
        self,
        state: SessionState,
        *,
        model: str | None = None,
    ) -> None:
        self.state = state
        self.model = model
        self.events: asyncio.Queue[Event] = asyncio.Queue()
        self._client: ClaudeSDKClient | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._plan_decision: asyncio.Future[PermissionResultAllow | PermissionResultDeny] | None = (
            None
        )
        self._pending_plan_text: str | None = None

    # ------------------------------------------------------------------ lifecycle

    async def start(self, prompt: str) -> None:
        """Open the client in plan mode and dispatch the initial prompt."""
        if self._client is not None:
            raise RuntimeError("session already started")

        self.state.initial_prompt = prompt

        options = ClaudeAgentOptions(
            cwd=self.state.workspace_dir,
            permission_mode="plan",
            can_use_tool=self._on_tool_request,
            hooks={
                "Stop": [HookMatcher(hooks=[self._on_stop_hook])],
                "Notification": [HookMatcher(hooks=[self._on_notification_hook])],
            },
            model=self.model,
        )

        self._client = ClaudeSDKClient(options=options)
        await self._client.connect()
        await self._set_status(SessionStatus.PLANNING)
        await self._client.query(prompt)
        self._reader_task = asyncio.create_task(
            self._read_loop(), name=f"chud-read-{self.state.id}"
        )

    async def stop(self) -> None:
        # Resolve any in-flight plan decision so the SDK's can_use_tool round-trip
        # completes before we tear down the stream; otherwise the Claude subprocess
        # logs "Error in hook callback hook_0: Stream closed" when its control
        # request can't reach us.
        if self._plan_decision is not None and not self._plan_decision.done():
            self._plan_decision.set_result(
                PermissionResultDeny(message="Session stopped.")
            )
        self._plan_decision = None
        self._pending_plan_text = None

        if self._client is not None:
            with contextlib.suppress(Exception):
                await self._client.interrupt()

        if self._reader_task is not None and not self._reader_task.done():
            self._reader_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._reader_task
        if self._client is not None:
            try:
                await self._client.disconnect()
            except Exception:
                log.exception("disconnect failed for session %s", self.state.id)
        self._client = None
        self._reader_task = None

    # ------------------------------------------------------------------ user input

    async def send_message(self, text: str) -> None:
        if self._client is None:
            raise RuntimeError("session not started")
        if self.state.status == SessionStatus.AWAITING_USER:
            await self._set_status(SessionStatus.EXECUTING)
        self.state.pending_question = None
        await self._client.query(text)

    async def approve_plan(self) -> None:
        """Approve the pending plan: switch to acceptEdits and let ExitPlanMode through."""
        if self._plan_decision is None or self._plan_decision.done():
            log.warning("approve_plan called with no pending decision (session %s)", self.state.id)
            return
        assert self._client is not None
        await self._client.set_permission_mode("acceptEdits")
        self._plan_decision.set_result(PermissionResultAllow())
        self._plan_decision = None
        self._pending_plan_text = None
        await self._set_status(SessionStatus.EXECUTING)

    async def reject_plan(self, reason: str = "User rejected the plan.") -> None:
        if self._plan_decision is None or self._plan_decision.done():
            log.warning("reject_plan called with no pending decision (session %s)", self.state.id)
            return
        self._plan_decision.set_result(PermissionResultDeny(message=reason))
        self._plan_decision = None
        self._pending_plan_text = None
        await self._set_status(SessionStatus.PLANNING)

    # ------------------------------------------------------------------ SDK callbacks

    async def _on_tool_request(
        self,
        tool_name: str,
        tool_input: dict[str, Any],
        context: ToolPermissionContext,
    ) -> PermissionResultAllow | PermissionResultDeny:
        """Permission interception. Gates on ExitPlanMode; allows everything else."""
        if tool_name == "ExitPlanMode":
            plan_text = str(tool_input.get("plan", "")).strip()
            self._pending_plan_text = plan_text
            self._plan_decision = asyncio.get_event_loop().create_future()
            await self._set_status(SessionStatus.AWAITING_PLAN_APPROVAL)
            await self._emit(EventKind.PLAN_PROPOSED, {"plan": plan_text})
            return await self._plan_decision

        # Default: allow. Future iterations may add a deny-list or interactive gating
        # for non-plan tools (e.g., destructive Bash). v1 trusts acceptEdits.
        return PermissionResultAllow()

    async def _on_stop_hook(
        self,
        input_data: dict[str, Any],
        tool_use_id: str | None,
        context: Any,
    ) -> dict[str, Any]:
        """Detect 'agent went idle without finishing' (probable question to user)."""
        if self.state.status not in (SessionStatus.AWAITING_PLAN_APPROVAL, SessionStatus.DONE):
            await self._set_status(SessionStatus.AWAITING_USER)
            await self._emit(
                EventKind.NEEDS_USER_INPUT,
                {"reason": "stop_hook", "raw": input_data},
            )
        return {}

    async def _on_notification_hook(
        self,
        input_data: dict[str, Any],
        tool_use_id: str | None,
        context: Any,
    ) -> dict[str, Any]:
        message = str(input_data.get("message") or input_data.get("notification") or input_data)
        self.state.pending_question = message
        await self._set_status(SessionStatus.AWAITING_USER)
        await self._emit(
            EventKind.NEEDS_USER_INPUT,
            {"reason": "notification", "message": message, "raw": input_data},
        )
        return {}

    # ------------------------------------------------------------------ message loop

    async def _read_loop(self) -> None:
        assert self._client is not None
        try:
            async for msg in self._client.receive_messages():
                await self._dispatch_message(msg)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.exception("read loop crashed for session %s", self.state.id)
            self.state.error = repr(e)
            await self._set_status(SessionStatus.ERRORED)
            await self._emit(EventKind.ERROR, {"error": repr(e)})

    async def _dispatch_message(self, msg: Any) -> None:
        if isinstance(msg, AssistantMessage):
            text_chunks: list[str] = []
            tool_calls: list[dict[str, Any]] = []
            for block in msg.content:
                if isinstance(block, TextBlock):
                    text_chunks.append(block.text)
                elif isinstance(block, ToolUseBlock):
                    tool_calls.append({"name": block.name, "input": block.input, "id": block.id})
            if text_chunks:
                await self._emit(
                    EventKind.TRANSCRIPT_APPENDED,
                    {"role": "assistant", "text": "".join(text_chunks)},
                )
            for tc in tool_calls:
                await self._emit(
                    EventKind.TRANSCRIPT_APPENDED,
                    {"role": "assistant", "tool_use": tc},
                )
        elif isinstance(msg, UserMessage):
            # Mostly tool_result echoes from the SDK. The default renderer drops
            # the raw payload; a future verbose mode can extract structured
            # tool_use_id / content from msg.content.
            await self._emit(
                EventKind.TRANSCRIPT_APPENDED, {"role": "user", "raw": _stringify(msg)}
            )
        elif isinstance(msg, SystemMessage):
            await self._emit(
                EventKind.TRANSCRIPT_APPENDED, {"role": "system", "raw": _stringify(msg)}
            )
        elif isinstance(msg, ResultMessage):
            # Full ResultMessage stats stay in chud.log; the UI just sees the
            # status transition emitted by _set_status.
            log.debug("ResultMessage for session %s: %s", self.state.id, _stringify(msg))
            await self._set_status(SessionStatus.DONE)
        else:
            log.warning("unknown SDK message type %s", type(msg).__name__)
            await self._emit(
                EventKind.UNKNOWN_MESSAGE,
                {"type": type(msg).__name__, "raw": _stringify(msg)},
            )

    # ------------------------------------------------------------------ helpers

    async def _set_status(self, status: SessionStatus) -> None:
        if self.state.status == status:
            return
        self.state.status = status
        self.state.last_activity_at = datetime.now(timezone.utc)
        await self._emit(EventKind.STATUS_CHANGED, {"status": status.value})

    async def _emit(self, kind: EventKind, payload: dict[str, Any]) -> None:
        await self.events.put(Event(session_id=self.state.id, kind=kind, payload=payload))


def _stringify(obj: Any) -> str:
    """Best-effort string repr for unknown SDK message shapes — keeps things visible in v1."""
    try:
        if hasattr(obj, "to_dict"):
            return str(obj.to_dict())
        if hasattr(obj, "__dict__"):
            return str(obj.__dict__)
        return repr(obj)
    except Exception:
        return repr(obj)


def workspace_for(state_root: Path, session_id: str) -> Path:
    return state_root / session_id
