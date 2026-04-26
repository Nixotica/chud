from __future__ import annotations

from pathlib import Path

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, Static

from chud.markup import TextHeading


class AttachRepoModal(ModalScreen[Path | None]):
    DEFAULT_CSS = """
    AttachRepoModal {
        align: center middle;
    }
    AttachRepoModal > Vertical {
        width: 80%;
        max-width: 80;
        height: 12;
        background: $surface;
        border: round $accent;
        padding: 1 2;
    }
    AttachRepoModal Horizontal {
        height: 3;
        align: right middle;
    }
    AttachRepoModal Button {
        margin-left: 2;
    }
    """

    BINDINGS = [("escape", "cancel", "Cancel")]

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(TextHeading("Attach a repo to this session"))
            yield Label("Repo path:")
            yield Input(placeholder="/path/to/repo", id="repo")
            with Horizontal():
                yield Button("Cancel", id="cancel")
                yield Button("Attach", id="attach", variant="success")

    def on_mount(self) -> None:
        self.query_one("#repo", Input).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel":
            self.dismiss(None)
        elif event.button.id == "attach":
            value = self.query_one("#repo", Input).value.strip()
            if value:
                self.dismiss(Path(value).expanduser())

    def action_cancel(self) -> None:
        self.dismiss(None)
