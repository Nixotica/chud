"""Modal asking the user to approve or deny a single tool call.

Surfaced by ``AgentSession`` when a permission decision is needed:

- ``run_mode == "default"`` and the SDK calls ``can_use_tool`` for a tool that
  the user's ``~/.claude/settings.json`` allow/deny rules didn't resolve.
- ``run_mode == "low_perms"`` and the agent invokes any of
  ``options.LOW_PERMS_GATED_TOOLS`` (Edit / Write / NotebookEdit / Bash).

Both paths use ``AgentSession._await_permission`` to block on a future; the
modal's dismiss value resolves it. Mirrors the keyboard vocabulary of
``PlanApprovalModal`` (a / r / x / Enter / Esc) so users move between modals
without context switching.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Literal

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Button, Input, Static

from chud.markup import TextHeading
from chud.widgets._scrollable_modal import ScrollableModalScreen


@dataclass(frozen=True)
class PermissionDecision:
    """User's choice on the per-tool permission modal.

    ``action`` is one of:
      - ``approve`` — let the agent execute this tool call.
      - ``deny`` — block this call. ``reason`` carries any typed feedback
        (empty for a plain deny).
      - ``kill`` — terminate the session outright. Used when the user has
        seen enough and wants to stop the agent rather than answer this
        prompt-after-prompt.
    """

    action: Literal["approve", "deny", "kill"]
    reason: str = ""


class PermissionModal(ScrollableModalScreen[PermissionDecision | None]):
    """Ask the user to approve/deny a single tool call.

    Dismiss values:
      - ``PermissionDecision("approve")`` — approve.
      - ``PermissionDecision("deny")`` — plain deny (sent to the agent as
        the default deny message).
      - ``PermissionDecision("deny", reason)`` — deny with free-text
        feedback. ``reason`` is forwarded to the agent as the deny message,
        same channel ``reject_plan`` uses.
      - ``PermissionDecision("kill")`` — user wants the session terminated.
      - ``None`` — defensive; resolved by the caller as a plain deny.
    """

    DEFAULT_CSS = """
    PermissionModal {
        align: center middle;
    }
    PermissionModal > Vertical {
        width: 90%;
        max-width: 120;
        height: 80%;
        background: $surface;
        border: round $accent;
        padding: 1 2;
    }
    PermissionModal #permission-title {
        height: 1;
        color: $accent;
    }
    PermissionModal #tool-name {
        height: 1;
        margin-top: 1;
        color: $accent;
    }
    PermissionModal VerticalScroll {
        height: 1fr;
        margin-top: 1;
        margin-bottom: 1;
    }
    PermissionModal Horizontal {
        height: 3;
        align: right middle;
    }
    PermissionModal Button {
        margin-left: 2;
    }
    PermissionModal #respond-input {
        display: none;
        height: 3;
        margin-top: 1;
    }
    PermissionModal #respond-input.visible {
        display: block;
    }
    """

    BINDINGS = [
        Binding("a", "approve", "Approve"),
        Binding("r", "deny", "Deny"),
        Binding("x", "kill", "Kill session"),
        Binding("enter", "respond", "Deny with feedback"),
        Binding("escape", "deny", "Deny"),
    ]

    def __init__(
        self,
        session_id: str,
        tool_name: str,
        tool_input: dict[str, Any],
    ) -> None:
        super().__init__()
        self.session_id = session_id
        self.tool_name = tool_name or "(unknown)"
        self.tool_input = tool_input or {}

    def scroll_container(self) -> VerticalScroll | None:
        try:
            return self.query_one(VerticalScroll)
        except Exception:
            return None

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(
                TextHeading(f"Tool permission — session {self.session_id[:8]}"),
                id="permission-title",
            )
            yield Static(TextHeading(self.tool_name), id="tool-name")
            with VerticalScroll():
                # Pretty-print so paths and command strings are scannable. The
                # default=str fallback handles non-JSON types (Path, etc.) that
                # the agent occasionally passes through.
                try:
                    body = json.dumps(self.tool_input, indent=2, default=str)
                except Exception:
                    body = repr(self.tool_input)
                yield Static(body, markup=False)
            with Horizontal():
                # Buttons stay mouse-clickable but never grab keyboard focus,
                # so a/r/k/enter/escape bindings always fire while the Input
                # is hidden. Once the Input is revealed and focused, it owns
                # Enter (to submit) and printable characters.
                kill_btn = Button("Kill session (x)", id="kill", variant="error")
                kill_btn.can_focus = False
                yield kill_btn
                deny_btn = Button("Deny (r)", id="deny", variant="warning")
                deny_btn.can_focus = False
                yield deny_btn
                respond_btn = Button("Deny w/ feedback (Enter)", id="respond")
                respond_btn.can_focus = False
                yield respond_btn
                approve_btn = Button("Approve (a)", id="approve", variant="success")
                approve_btn.can_focus = False
                yield approve_btn
            yield Input(placeholder="Reason… (Enter to send)", id="respond-input")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "approve":
            self.dismiss(PermissionDecision("approve"))
        elif event.button.id == "deny":
            self.dismiss(PermissionDecision("deny"))
        elif event.button.id == "kill":
            self.dismiss(PermissionDecision("kill"))
        elif event.button.id == "respond":
            self.action_respond()

    def action_approve(self) -> None:
        self.dismiss(PermissionDecision("approve"))

    def action_deny(self) -> None:
        self.dismiss(PermissionDecision("deny"))

    def action_kill(self) -> None:
        self.dismiss(PermissionDecision("kill"))

    def action_respond(self) -> None:
        """First press reveals the input + focuses it; second Enter (handled
        by ``_on_respond_submitted`` below) dispatches the deny with the
        typed reason."""
        try:
            inp = self.query_one("#respond-input", Input)
        except Exception:
            return
        if "visible" not in inp.classes:
            inp.add_class("visible")
            inp.focus()

    @on(Input.Submitted, "#respond-input")
    def _on_respond_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        # Empty submit collapses to a plain deny so the user can't accidentally
        # forward a no-op message; any non-empty string becomes the deny
        # reason.
        self.dismiss(PermissionDecision("deny", text))
