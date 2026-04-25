from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Markdown, Static


class PlanApprovalModal(ModalScreen[bool]):
    """Show the agent's proposed plan and gate execution on user approval.

    Returns True on approve, False on reject.
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
    """

    BINDINGS = [
        ("a", "approve", "Approve"),
        ("r", "reject", "Reject"),
        ("escape", "reject", "Reject"),
    ]

    def __init__(self, session_id: str, plan_text: str) -> None:
        super().__init__()
        self.session_id = session_id
        self.plan_text = plan_text

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(f"[bold]Plan from session {self.session_id[:8]}[/bold]", id="plan-title")
            with VerticalScroll():
                yield Markdown(self.plan_text or "_(empty plan)_")
            with Horizontal():
                yield Button("Reject (r)", id="reject", variant="error")
                yield Button("Approve (a)", id="approve", variant="success")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "approve":
            self.dismiss(True)
        elif event.button.id == "reject":
            self.dismiss(False)

    def action_approve(self) -> None:
        self.dismiss(True)

    def action_reject(self) -> None:
        self.dismiss(False)
