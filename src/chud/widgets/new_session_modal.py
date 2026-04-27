from __future__ import annotations

from dataclasses import dataclass, field

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Checkbox, Label, Static, TextArea

from chud.options import SESSION_OPTIONS
from chud.state import save_user_config, user_default_options


@dataclass
class NewSessionResult:
    prompt: str
    options: dict[str, bool] = field(default_factory=user_default_options)


class NewSessionModal(ModalScreen[NewSessionResult | None]):
    """Prompt the user for an initial prompt + options for a new session.

    The repo to attach is derived automatically from chud's launch directory
    (see ``chud.worktree.detect_cwd_repo``); when chud isn't inside a git
    repo, the session starts unattached and the agent uses the
    ``mcp__chud__attach_repo`` tool to attach repos itself.
    """

    DEFAULT_CSS = """
    NewSessionModal {
        align: center middle;
    }
    NewSessionModal > Vertical {
        width: 90%;
        max-width: 100;
        height: 26;
        background: $surface;
        border: round $accent;
        padding: 1 2;
    }
    NewSessionModal Label {
        height: 1;
        color: $text-muted;
    }
    NewSessionModal TextArea {
        height: 8;
        margin-bottom: 1;
    }
    NewSessionModal #options-group {
        height: auto;
        max-height: 8;
        margin-bottom: 1;
        border: round $primary-background;
        padding: 0 1;
    }
    NewSessionModal #options-group Label {
        color: $accent;
    }
    NewSessionModal #options-group Checkbox {
        height: 1;
        margin: 0;
        padding: 0;
        border: none;
        background: transparent;
    }
    NewSessionModal #options-group Checkbox:focus {
        border: none;
        background: $boost;
    }
    NewSessionModal Horizontal#buttons {
        height: 3;
        align: right middle;
    }
    NewSessionModal Button {
        margin-left: 2;
    }
    """

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("f2", "start", "Start", priority=True),
    ]

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static("[bold]New session[/bold]")
            yield Label("Initial prompt:")
            yield TextArea("", id="prompt")
            defaults = user_default_options()
            with VerticalScroll(id="options-group"):
                yield Label("Options")
                for opt in SESSION_OPTIONS:
                    yield Checkbox(
                        opt.label,
                        value=defaults[opt.id],
                        id=f"opt-{opt.id}",
                        tooltip=opt.description,
                    )
            with Horizontal(id="buttons"):
                yield Button("Cancel (Esc)", id="cancel")
                yield Button("Save as defaults", id="save-defaults")
                yield Button("Start (F2)", id="start", variant="success")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel":
            self.dismiss(None)
        elif event.button.id == "save-defaults":
            self._save_defaults()
        elif event.button.id == "start":
            self._submit()

    def _save_defaults(self) -> None:
        options = {
            opt.id: self.query_one(f"#opt-{opt.id}", Checkbox).value
            for opt in SESSION_OPTIONS
        }
        save_user_config(options)
        self.app.notify("Saved as defaults.")

    def action_cancel(self) -> None:
        self.dismiss(None)

    def action_start(self) -> None:
        self._submit()

    def on_mount(self) -> None:
        self.query_one("#prompt", TextArea).focus()

    def _submit(self) -> None:
        prompt = self.query_one("#prompt", TextArea).text.strip()
        if not prompt:
            return
        options = {
            opt.id: self.query_one(f"#opt-{opt.id}", Checkbox).value
            for opt in SESSION_OPTIONS
        }
        self.dismiss(NewSessionResult(prompt=prompt, options=options))
