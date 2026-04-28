from __future__ import annotations

from dataclasses import dataclass, field

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Checkbox, Label, Select, Static, TextArea

from chud.options import EFFORT_VALUES, SESSION_OPTIONS
from chud.state import (
    claude_settings_effort,
    get_recent_repo_paths,
    load_user_config,
    save_user_config,
    user_default_effort,
    user_default_options,
)
from chud.types import KEY_EFFORT

_EFFORT_CHOICES: tuple[tuple[str, str], ...] = tuple((v.capitalize(), v) for v in EFFORT_VALUES)


@dataclass
class NewSessionResult:
    prompt: str
    options: dict[str, bool] = field(default_factory=user_default_options)


def _detect_cwd_repo() -> str:
    """If CWD is inside a git repo, return its toplevel path; else empty string."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            cwd=Path.cwd(),
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return ""


def _safe_recent_repos() -> list[str]:
    """Return recent repo paths for autocomplete, swallowing any load errors.

    The modal must open even if `sessions.json` is missing or malformed, so we
    fall back to an empty list and let the suggester silently no-op.
    """
    try:
        return get_recent_repo_paths()
    except Exception:
        return []


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
        height: 36;
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
    NewSessionModal #effort {
        margin-bottom: 1;
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
            yield Label("Effort:")
            default_effort = user_default_effort() or claude_settings_effort()
            choices = tuple(
                (f"{label} (default)" if value == default_effort else label, value)
                for label, value in _EFFORT_CHOICES
            )
            tooltip = (
                "Reasoning effort hint for the agent. "
                "Default leaves it to the SDK; higher values trade speed for thoroughness."
            )
            if default_effort is None:
                yield Select(choices, id=KEY_EFFORT, allow_blank=True, tooltip=tooltip)
            else:
                yield Select(
                    choices,
                    id=KEY_EFFORT,
                    allow_blank=False,
                    value=default_effort,
                    tooltip=tooltip,
                )
            with Horizontal(id="buttons"):
                yield Button("Cancel (Esc)", id="cancel")
                yield Button("Start (F2)", id="start", variant="success")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel":
            self.dismiss(None)
        elif event.button.id == "start":
            self._submit()

    def _save_defaults(self) -> None:
        options = {
            opt.id: self.query_one(f"#opt-{opt.id}", Checkbox).value for opt in SESSION_OPTIONS
        }
        # Merge into the existing config dict so we don't clobber unrelated
        # keys (e.g. future settings) that other code paths may have written.
        config = load_user_config()
        config.update(options)
        config[KEY_EFFORT] = self._read_effort()
        save_user_config(config)
        self.app.notify("Saved as defaults.")

    def _read_effort(self) -> str | None:
        """Read the effort Select; blank (no selection) maps to ``None``."""
        raw = self.query_one("#effort", Select).value
        if not isinstance(raw, str):
            return None
        return raw

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
            opt.id: self.query_one(f"#opt-{opt.id}", Checkbox).value for opt in SESSION_OPTIONS
        }
        self.dismiss(
            NewSessionResult(
                repo_path=repo_path,
                prompt=prompt,
                options=options,
                effort=self._read_effort(),
            )
        )
