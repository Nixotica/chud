from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

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
    HookContext,
    HookInput,
    HookJSONOutput,
    HookMatcher,
    PermissionResultAllow,
    PermissionResultDeny,
    ToolPermissionContext,
)

from chud.options import LOW_PERMS_GATED_TOOLS, EffortLevel, normalize_run_mode
from chud.state import worktrees_root
from chud.tools import AttachCallback, build_chud_mcp_server
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
        launch_cwd: Path | None = None,
        attach_callback: AttachCallback | None = None,
        effort: str | None = None,
        run_mode: str | None = None,
    ) -> None:
        self.state = state
        self.model = model
        self._launch_cwd = launch_cwd
        self._attach_callback = attach_callback
        self.effort = effort
        # Resolve to a known choice. ``run_mode`` arg overrides the value
        # carried on ``state.run_mode`` (callers that route everything through
        # the manager will pass them in sync; the kwarg makes it explicit for
        # tests that build an AgentSession directly). Single source of truth
        # lives on ``state.run_mode`` so it round-trips through persistence.
        raw_mode = run_mode if run_mode is not None else state.run_mode
        self.state.run_mode = normalize_run_mode(raw_mode)
        self.events: asyncio.Queue[Event] = asyncio.Queue()
        self._client: ClaudeSDKClient | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._plan_decision: asyncio.Future[PermissionResultAllow | PermissionResultDeny] | None = (
            None
        )
        self._pending_plan_text: str | None = None
        self._question_decision: (
            asyncio.Future[PermissionResultAllow | PermissionResultDeny] | None
        ) = None
        self._pending_question_input: dict[str, Any] | None = None
        # Single-slot future for per-tool permission prompts (``default`` and
        # ``low_perms`` modes). Mirrors the ``_plan_decision`` /
        # ``_question_decision`` pattern: at most one in-flight prompt; if a
        # second arrives we resolve the prior with ``Deny("superseded")``.
        self._permission_decision: (
            asyncio.Future[PermissionResultAllow | PermissionResultDeny] | None
        ) = None
        self._pending_permission: dict[str, Any] | None = None
        # Set when the user denies a plan or tool call. When the agent then
        # ends its turn (ResultMessage), we route to AWAITING_USER instead of
        # DONE so the user can either reply or kill the session — never the
        # auto-PR / cleanup pipeline, which would otherwise treat a deny-driven
        # giveup as a successful finish. Cleared when the user sends a fresh
        # message (start of a new turn).
        self._deny_pending: bool = False
        # Plan-rejection feedback, replayed as a user message once the
        # interrupted planning turn ends (see reject_plan).
        self._pending_plan_feedback: str | None = None

    # ------------------------------------------------------------------ lifecycle

    async def start(self, prompt: str) -> None:
        """Open the client in plan mode and dispatch the initial prompt."""
        if self._client is not None:
            raise RuntimeError("session already started")

        self.state.initial_prompt = prompt

        # If exactly one repo is attached, run the agent inside that worktree so
        # its tools see a real git checkout. With 0 attached repos, prefer the
        # user's launch directory (so the agent can `ls` and discover sibling
        # repos to attach via mcp__chud__attach_repo); fall back to the
        # worktrees root when no launch dir was provided. With 2+ repos, use
        # the worktrees root so the agent can `cd` between worktree subdirs.
        attached = list(self.state.attached_repos.values())
        if len(attached) == 1:
            cwd = attached[0].worktree_path
        elif len(attached) == 0 and self._launch_cwd is not None:
            cwd = self._launch_cwd
        else:
            cwd = worktrees_root()

        effort = cast(EffortLevel | None, self.effort)
        # Per-session in-process MCP server exposing chud-native tools to the
        # agent (e.g. attach_repo). Skipped when no callback was wired in so
        # tests/standalone uses don't pay for an unused server.
        mcp_servers: dict[str, Any] = {}
        allowed_extras: list[str] = []
        if self._attach_callback is not None:
            mcp_servers["chud"] = build_chud_mcp_server(self._attach_callback)
            allowed_extras.append("mcp__chud__attach_repo")

        options_kwargs: dict[str, Any] = dict(
            cwd=cwd,
            permission_mode="plan",
            can_use_tool=self._on_tool_request,
            hooks={
                "Stop": [HookMatcher(hooks=[self._on_stop_hook])],
                "Notification": [HookMatcher(hooks=[self._on_notification_hook])],
                # PreToolUse fires for every tool call regardless of
                # permission_mode, so it's the only reliable enforcement point
                # once we flip to acceptEdits — can_use_tool is bypassed for
                # auto-allowed tools in that mode.
                "PreToolUse": [HookMatcher(hooks=[self._on_pre_tool_use_hook])],
            },
            model=self.model,
            effort=effort,
        )
        if mcp_servers:
            options_kwargs["mcp_servers"] = mcp_servers
            # Setting allowed_tools restricts the model to that list, so we
            # only set it when we actually need to allow our extras through.
            options_kwargs["allowed_tools"] = allowed_extras

        options = ClaudeAgentOptions(**options_kwargs)

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
            self._plan_decision.set_result(PermissionResultDeny(message="Session stopped."))
        self._plan_decision = None
        self._pending_plan_text = None
        self._pending_plan_feedback = None

        if self._question_decision is not None and not self._question_decision.done():
            self._question_decision.set_result(PermissionResultDeny(message="Session stopped."))
        self._question_decision = None
        self._pending_question_input = None

        if self._permission_decision is not None and not self._permission_decision.done():
            self._permission_decision.set_result(PermissionResultDeny(message="Session stopped."))
        self._permission_decision = None
        self._pending_permission = None

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

    def _resume_status(self) -> SessionStatus:
        """Status to resume into when the user supplies input.

        Until a plan is approved the SDK is in plan mode, where a user message
        continues planning. Flipping to EXECUTING there would arm the
        post-approval filesystem gate (blocking the CLI's own plan-file edits,
        so the plan can't be revised) and let a plan-mode turn-end fall through
        to DONE.
        """
        return SessionStatus.EXECUTING if self.state.approved_plan else SessionStatus.PLANNING

    async def send_message(self, text: str) -> None:
        if self._client is None:
            raise RuntimeError("session not started")
        if self.state.status == SessionStatus.AWAITING_USER:
            await self._set_status(self._resume_status())
        self.state.pending_question = None
        self._deny_pending = False
        self._pending_plan_feedback = None
        await self._client.query(text)

    async def approve_plan(self) -> None:
        """Approve the pending plan and flip the SDK to the chosen permission mode.

        ``run_mode`` determines the target SDK mode:
          - ``full_auto`` and ``low_perms`` → ``acceptEdits``. ``low_perms``
            relies on chud's PreToolUse hook to surface modals for each
            dangerous tool; leaving the SDK in ``acceptEdits`` keeps the SDK
            from double-prompting.
          - ``default`` → ``default``. The SDK then respects the user's
            ``~/.claude/settings.json`` allow/deny rules and routes the
            fallthrough to chud's ``can_use_tool`` callback, which surfaces
            a PermissionModal.
        """
        if self._plan_decision is None or self._plan_decision.done():
            log.warning("approve_plan called with no pending decision (session %s)", self.state.id)
            return
        assert self._client is not None
        # Map chud RunMode → SDK permission_mode. Note: both literals happen
        # to be the string ``"default"`` on the ``default`` branch — one is
        # chud's RunMode value, the other is the SDK's permission_mode value.
        sdk_mode = "default" if self.state.run_mode == "default" else "acceptEdits"
        await self._client.set_permission_mode(sdk_mode)
        # Persist the approved plan text so downstream consumers (PR title/body)
        # can read it later, including across TUI restarts.
        if self._pending_plan_text:
            self.state.approved_plan = self._pending_plan_text
        self._plan_decision.set_result(PermissionResultAllow())
        self._plan_decision = None
        self._pending_plan_text = None
        await self._set_status(SessionStatus.EXECUTING)

    async def reject_plan(self, reason: str = "") -> None:
        """Reject the pending plan and re-engage the agent with the feedback.

        The model treats feedback sent through the ``ExitPlanMode`` deny message
        as untrusted tool output and ignores it (re-proposing the same plan), so
        the deny instead interrupts the planning turn; once it ends,
        ``_dispatch_message`` replays the feedback as a real ``query()`` user
        message — which the model acts on. A bare reject (no ``reason``) parks
        the session in AWAITING_USER for the user's follow-up.
        """
        if self._plan_decision is None or self._plan_decision.done():
            log.warning("reject_plan called with no pending decision (session %s)", self.state.id)
            return
        self._plan_decision.set_result(
            PermissionResultDeny(message="User rejected the plan.", interrupt=True)
        )
        self._plan_decision = None
        self._pending_plan_text = None
        feedback = reason.strip()
        self._pending_plan_feedback = feedback or None
        self._deny_pending = not feedback
        await self._set_status(SessionStatus.PLANNING)

    async def answer_question(self, answer_text: str) -> None:
        """Resolve a pending AskUserQuestion tool call with the user's answer.

        The answer is delivered to the model via PermissionResultDeny.message —
        the same SDK channel reject_plan() uses to ferry free-text feedback into
        the agent. From the model's POV this reads as the tool's response.
        """
        if self._question_decision is None or self._question_decision.done():
            log.warning(
                "answer_question called with no pending decision (session %s)", self.state.id
            )
            return
        self._question_decision.set_result(PermissionResultDeny(message=answer_text))
        self._question_decision = None
        self._pending_question_input = None
        await self._set_status(self._resume_status())

    async def approve_tool(self) -> None:
        """Resolve a pending per-tool permission prompt with allow.

        Used by both ``default`` mode (via ``can_use_tool``) and ``low_perms``
        mode (via ``PreToolUse`` hook). Status stays at EXECUTING — unlike
        ``AskUserQuestion``, permission prompts don't represent the agent
        going idle; the SDK is mid-tool-call awaiting our decision.
        """
        if self._permission_decision is None or self._permission_decision.done():
            log.warning("approve_tool called with no pending decision (session %s)", self.state.id)
            return
        self._permission_decision.set_result(PermissionResultAllow())
        self._permission_decision = None
        self._pending_permission = None

    async def deny_tool(self, reason: str = "User denied.") -> None:
        """Resolve a pending per-tool permission prompt with deny.

        ``reason`` is forwarded to the agent as the deny message, mirroring
        ``reject_plan``'s free-text feedback channel.
        """
        if self._permission_decision is None or self._permission_decision.done():
            log.warning("deny_tool called with no pending decision (session %s)", self.state.id)
            return
        self._permission_decision.set_result(PermissionResultDeny(message=reason))
        self._permission_decision = None
        self._pending_permission = None
        self._deny_pending = True

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
            if not plan_text:
                return PermissionResultDeny(
                    message=(
                        "ExitPlanMode requires a non-empty plan. Continue "
                        "planning and call ExitPlanMode again with the "
                        "proposed plan written out in the `plan` argument."
                    )
                )
            self._pending_plan_text = plan_text
            self._plan_decision = asyncio.get_event_loop().create_future()
            await self._set_status(SessionStatus.AWAITING_PLAN_APPROVAL)
            await self._emit(EventKind.PLAN_PROPOSED, {"plan": plan_text})
            return await self._plan_decision

        if tool_name == "AskUserQuestion":
            # AskUserQuestion is a Claude Code harness builtin — the SDK has no
            # implementation, so allowing it through hangs the agent. Intercept
            # it like ExitPlanMode: stash the input, surface a modal via the
            # event queue, and wait for answer_question() to resolve the future
            # with a Deny whose .message carries the user's selection back to
            # the model.
            self._pending_question_input = dict(tool_input)
            self._question_decision = asyncio.get_event_loop().create_future()
            await self._set_status(SessionStatus.AWAITING_USER)
            await self._emit(EventKind.QUESTION_ASKED, {"input": dict(tool_input)})
            return await self._question_decision

        reason = self._check_filesystem_access(tool_name, tool_input)
        if reason is not None:
            return PermissionResultDeny(message=reason)

        # ``default`` mode: after plan approval the SDK is in its ``default``
        # permission mode, which routes anything not auto-allowed by user
        # settings through this callback. Surface a modal for it; ``low_perms``
        # uses the PreToolUse hook instead (so this branch stays a no-op).
        if self.state.run_mode == "default" and self.state.status in (
            SessionStatus.EXECUTING,
            SessionStatus.AWAITING_USER,
        ):
            return await self._await_permission(tool_name, tool_input)

        return PermissionResultAllow()

    async def _await_permission(
        self, tool_name: str, tool_input: dict[str, Any]
    ) -> PermissionResultAllow | PermissionResultDeny:
        """Surface a PermissionModal and block until the user resolves it.

        Single-slot future, mirroring ``_plan_decision`` /
        ``_question_decision``. If a request arrives while another is
        pending we resolve the prior with ``Deny("superseded")`` and log;
        the SDK serialises tool calls per session in practice so this is
        defensive.
        """
        if self._permission_decision is not None and not self._permission_decision.done():
            log.warning("permission decision superseded for session %s", self.state.id)
            self._permission_decision.set_result(PermissionResultDeny(message="superseded"))
        self._pending_permission = {"tool_name": tool_name, "tool_input": dict(tool_input)}
        self._permission_decision = asyncio.get_event_loop().create_future()
        await self._emit(
            EventKind.PERMISSION_REQUESTED,
            {"tool_name": tool_name, "tool_input": dict(tool_input)},
        )
        return await self._permission_decision

    # Edit/Write/NotebookEdit expose the target path in their tool input under
    # these keys; we resolve and check it against attached worktrees.
    _PATH_TOOL_ARG: dict[str, str] = {
        "Edit": "file_path",
        "Write": "file_path",
        "NotebookEdit": "notebook_path",
    }

    def _check_filesystem_access(self, tool_name: str, tool_input: dict[str, Any]) -> str | None:
        """Gate filesystem-mutating tools to attached worktrees.

        Layer 1 (pre-attach): with no repos attached, deny Edit/Write/
        NotebookEdit/Bash so the agent must call ``mcp__chud__attach_repo``
        first. Read-only tools (Read/Grep/Glob/LS) stay open for discovery.

        Layer 2 (path gate): once repos are attached, deny path-typed tools
        whose target falls outside any attached worktree. Bash is not vetted
        here — Layer 3b sets the SDK cwd to the worktree at session start so
        agents passively land inside the worktree without static parsing.

        Plan mode already disables edits, so this gate only applies after the
        plan has been approved (status EXECUTING / AWAITING_USER).
        """
        if self.state.status in (
            SessionStatus.NEW,
            SessionStatus.PLANNING,
            SessionStatus.AWAITING_PLAN_APPROVAL,
        ):
            return None

        is_path_tool = tool_name in self._PATH_TOOL_ARG
        is_bash = tool_name == "Bash"
        if not (is_path_tool or is_bash):
            return None

        attached = self.state.attached_repos
        if not attached:
            return (
                f"chud: {tool_name} is not allowed before any repo is attached "
                f"to this session. Use Read/Glob/LS to discover candidate "
                f"repos, then call mcp__chud__attach_repo with the absolute "
                f"path to a git repo toplevel. After attach, edits and Bash "
                f"are allowed inside the resulting worktree."
            )

        if not is_path_tool:
            return None

        arg_name = self._PATH_TOOL_ARG[tool_name]
        raw = tool_input.get(arg_name)
        if not isinstance(raw, str) or not raw:
            return None  # malformed — let the tool surface its own error.

        target = Path(raw).expanduser()
        try:
            resolved_target = target.resolve()
        except OSError:
            resolved_target = target.absolute()

        worktree_paths = [wt.worktree_path.resolve() for wt in attached.values()]
        for wp in worktree_paths:
            try:
                resolved_target.relative_to(wp)
                return None
            except ValueError:
                continue

        listed = ", ".join(str(wp) for wp in worktree_paths)
        return (
            f"chud: {tool_name} blocked — {resolved_target} is outside this "
            f"session's attached worktrees ({listed}). To edit a different "
            f"repo, call mcp__chud__attach_repo with the repo toplevel; chud "
            f"will create a worktree on branch chud/{self.state.id} under "
            f"{worktrees_root()} and you should edit that copy "
            f"instead of the user's main checkout."
        )

    async def _on_pre_tool_use_hook(
        self,
        input_data: HookInput,
        tool_use_id: str | None,
        context: HookContext,
    ) -> HookJSONOutput:
        """Enforce the filesystem gate for every tool call.

        Unlike ``can_use_tool``, PreToolUse hooks fire even when permission_mode
        is ``acceptEdits`` — so this is the gate that actually runs once the
        user approves a plan.

        ``low_perms`` mode also uses this hook to unconditionally prompt for
        every Edit/Write/NotebookEdit/Bash, even when the user's
        ``~/.claude/settings.json`` would have auto-allowed them.
        """
        tool_name = cast(str, input_data.get("tool_name", ""))
        tool_input = cast(dict[str, Any], input_data.get("tool_input", {}))
        reason = self._check_filesystem_access(tool_name, tool_input)
        if reason is not None:
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": reason,
                }
            }

        if (
            self.state.run_mode == "low_perms"
            and tool_name in LOW_PERMS_GATED_TOOLS
            and self.state.status in (SessionStatus.EXECUTING, SessionStatus.AWAITING_USER)
        ):
            result = await self._await_permission(tool_name, tool_input)
            if isinstance(result, PermissionResultDeny):
                return {
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": "deny",
                        "permissionDecisionReason": result.message or "User denied.",
                    }
                }
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "allow",
                }
            }

        return {}

    async def _on_stop_hook(
        self,
        input_data: HookInput,
        tool_use_id: str | None,
        context: HookContext,
    ) -> HookJSONOutput:
        """Detect 'agent went idle without finishing' (probable question to user)."""
        if self.state.status not in (SessionStatus.AWAITING_PLAN_APPROVAL, SessionStatus.DONE):
            await self._set_status(SessionStatus.AWAITING_USER)
            await self._emit(
                EventKind.NEEDS_USER_INPUT,
                {"reason": "stop_hook", "raw": dict(input_data)},
            )
        return {}

    async def _on_notification_hook(
        self,
        input_data: HookInput,
        tool_use_id: str | None,
        context: HookContext,
    ) -> HookJSONOutput:
        raw = dict(input_data)
        message = str(raw.get("message") or raw.get("notification") or raw)
        self.state.pending_question = message
        await self._set_status(SessionStatus.AWAITING_USER)
        await self._emit(
            EventKind.NEEDS_USER_INPUT,
            {"reason": "notification", "message": message, "raw": raw},
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
            if self._pending_plan_feedback is not None:
                # Plan rejected with feedback: the interrupt ended the turn, so
                # replay the feedback as a real user message and keep planning.
                feedback = self._pending_plan_feedback
                self._pending_plan_feedback = None
                self._deny_pending = False
                await self._set_status(SessionStatus.PLANNING)
                if self._client is not None:
                    await self._client.query(feedback)
            elif self._deny_pending:
                # The agent ended its turn after the user denied an edit or
                # rejected a plan. Don't treat this as a successful completion
                # (which would auto-publish a PR / trigger cleanup); park the
                # session in AWAITING_USER so the user can either reply with a
                # follow-up or kill the session explicitly.
                self._deny_pending = False
                await self._set_status(SessionStatus.AWAITING_USER)
                await self._emit(
                    EventKind.NEEDS_USER_INPUT,
                    {
                        "reason": "deny_followup",
                        "message": (
                            "Agent ended its turn after your denial. Send a new "
                            "message to continue, or kill the session."
                        ),
                    },
                )
            elif self.state.status in (
                SessionStatus.NEW,
                SessionStatus.PLANNING,
                SessionStatus.AWAITING_PLAN_APPROVAL,
            ):
                # DONE is reserved for sessions whose plan was approved and whose
                # execution finished. If the agent ended its turn without ever
                # getting past plan approval, surface AWAITING_USER so the user
                # can re-engage rather than collapsing into terminal DONE.
                await self._set_status(SessionStatus.AWAITING_USER)
            else:
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
        self.state.last_activity_at = datetime.now(UTC)
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
