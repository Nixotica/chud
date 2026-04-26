from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Static

from chud.markup import TextDanger, TextHeading, TextMuted, TextPath
from chud.types import SessionState


class CleanupConfirmationModal(ModalScreen[bool]):
    """Confirm destructive cleanup of a finished session.

    Returns True on confirm, False on cancel/escape. The caller is responsible
    for actually invoking ``manager.kill_session(..., cleanup_workspace=True)``
    and removing the row from the sidebar.
    """

    DEFAULT_CSS = """
    CleanupConfirmationModal {
        align: center middle;
    }
    CleanupConfirmationModal > Vertical {
        width: 90%;
        max-width: 100;
        height: auto;
        max-height: 80%;
        background: $surface;
        border: round $accent;
        padding: 1 2;
    }
    CleanupConfirmationModal #title {
        height: 1;
        color: $accent;
    }
    CleanupConfirmationModal VerticalScroll {
        height: auto;
        max-height: 14;
        margin-top: 1;
        margin-bottom: 1;
    }
    CleanupConfirmationModal Horizontal {
        height: 3;
        align: right middle;
    }
    CleanupConfirmationModal Button {
        margin-left: 2;
    }
    """

    BINDINGS = [
        ("escape", "cancel", "Cancel"),
        ("y", "confirm", "Confirm"),
        ("n", "cancel", "Cancel"),
    ]

    def __init__(self, state: SessionState) -> None:
        super().__init__()
        self.state = state

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(
                TextHeading(f"Clean up session {self.state.id[:8]}?"),
                id="title",
            )
            with VerticalScroll():
                yield Static(
                    f"This will {TextDanger('permanently delete')} the workspace "
                    "directory and remove all attached worktrees:"
                )
                yield Static(f"  • workspace: {TextPath(str(self.state.workspace_dir))}")
                if self.state.attached_repos:
                    for wt in self.state.attached_repos.values():
                        yield Static(
                            f"  • worktree:  {TextPath(str(wt.worktree_path))}  "
                            f"{TextMuted(f'(branch {wt.branch})')}"
                        )
                else:
                    yield Static(f"  {TextMuted('(no attached repos)')}")
                yield Static(
                    "\nUnpushed commits on the chud branch will be lost unless "
                    "you also enabled the draft-PR option."
                )
            with Horizontal():
                yield Button("Cancel (n)", id="cancel")
                yield Button("Confirm (y)", id="confirm", variant="error")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "confirm":
            self.dismiss(True)
        elif event.button.id == "cancel":
            self.dismiss(False)

    def action_confirm(self) -> None:
        self.dismiss(True)

    def action_cancel(self) -> None:
        self.dismiss(False)
