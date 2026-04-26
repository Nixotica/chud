from __future__ import annotations

from textual import on
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Button, Input, Markdown, Static

from chud.markup import TextHeading
from chud.widgets._scrollable_modal import ScrollableModalScreen


class PlanApprovalModal(ScrollableModalScreen[bool | str]):
    """Show the agent's proposed plan and gate execution on user approval.

    Returns:
        - True on approve
        - False on plain reject (default rejection reason)
        - str on respond: a non-empty user-typed reason. The caller should treat
          this as a rejection whose deny message is the typed string, so the
          agent can revise.
    """

    DEFAULT_CSS = """
    PlanApprovalModal {
        align: center middle;
    }
    PlanApprovalModal > Vertical {
        width: 90%;
        max-width: 120;
        height: 80%;
        background: $surface;
        border: round $accent;
        padding: 1 2;
    }
    PlanApprovalModal #plan-title {
        height: 1;
        color: $accent;
    }
    PlanApprovalModal VerticalScroll {
        height: 1fr;
        margin-top: 1;
        margin-bottom: 1;
    }
    PlanApprovalModal Horizontal {
        height: 3;
        align: right middle;
    }
    PlanApprovalModal Button {
        margin-left: 2;
    }
    PlanApprovalModal #respond-input {
        display: none;
        height: 3;
        margin-top: 1;
    }
    PlanApprovalModal #respond-input.visible {
        display: block;
    }
    """

    BINDINGS = [
        ("a", "approve", "Approve"),
        ("r", "reject", "Reject"),
        ("enter", "respond", "Respond"),
        ("escape", "reject", "Reject"),
    ]

    def __init__(self, session_id: str, plan_text: str) -> None:
        super().__init__()
        self.session_id = session_id
        self.plan_text = plan_text

    def scroll_container(self) -> VerticalScroll | None:
        try:
            return self.query_one(VerticalScroll)
        except Exception:
            return None

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(
                TextHeading(f"Plan from session {self.session_id[:8]}"),
                id="plan-title",
            )
            with VerticalScroll():
                yield Markdown(self.plan_text or "_(empty plan)_")
            with Horizontal():
                # Buttons remain mouse-clickable but never grab keyboard focus,
                # so the screen's BINDINGS (a / r / enter / escape) always fire
                # while the response Input is hidden. Once the Input is revealed
                # and focused, it naturally takes priority for Enter (submit)
                # and printable characters.
                reject_btn = Button("Reject (r)", id="reject", variant="error")
                reject_btn.can_focus = False
                yield reject_btn
                respond_btn = Button("Respond (Enter)", id="respond")
                respond_btn.can_focus = False
                yield respond_btn
                approve_btn = Button("Approve (a)", id="approve", variant="success")
                approve_btn.can_focus = False
                yield approve_btn
            yield Input(placeholder="Reason… (Enter to send)", id="respond-input")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "approve":
            self.dismiss(True)
        elif event.button.id == "reject":
            self.dismiss(False)
        elif event.button.id == "respond":
            self.action_respond()

    def action_approve(self) -> None:
        self.dismiss(True)

    def action_reject(self) -> None:
        self.dismiss(False)

    def action_respond(self) -> None:
        """First press reveals the input and focuses it; the Input widget
        handles the second Enter via on_input_submitted below."""
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
        # Empty submit collapses to a plain reject so the user can't accidentally
        # forward a no-op message; any non-empty string becomes the deny reason.
        self.dismiss(text or False)
