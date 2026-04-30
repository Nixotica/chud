from __future__ import annotations

from dataclasses import dataclass, field

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Checkbox, Label, Select, Static, TextArea

from chud.gh import Issue, build_issue_prompt
from chud.options import EFFORT_VALUES, SESSION_OPTIONS
from chud.state import (
    claude_settings_effort,
    load_user_config,
    save_user_config,
    user_default_effort,
    user_default_options,
)
from chud.types import KEY_EFFORT

_EFFORT_CHOICES: tuple[tuple[str, str], ...] = tuple((v.capitalize(), v) for v in EFFORT_VALUES)

# Glyph prefix used in the issue picker to mark issues that already have an
# active (non-terminal) chud session linked to them. Avoid bracket-wrapped
# text — Textual's Select renders option labels through Rich markup, so
# ``[…]`` would be eaten as a tag.
ACTIVE_CHUD_ICON = "⚙"


@dataclass
class NewSessionResult:
    prompt: str
    options: dict[str, bool] = field(default_factory=user_default_options)
    effort: str | None = None
    issue: Issue | None = None


class NewSessionModal(ModalScreen[NewSessionResult | None]):
    """Prompt the user for an initial prompt + options for a new session.

    The repo to attach is derived automatically from chud's launch directory
    (see ``chud.worktree.detect_cwd_repo``); when chud isn't inside a git
    repo, the session starts unattached and the agent uses the
    ``mcp__chud__attach_repo`` tool to attach repos itself.

    When chud is launched inside a repo and ``gh`` is available, the caller
    can pass a list of recent open issues into ``issues``; the modal then
    renders an optional GitHub-issue picker above the prompt. Selecting an
    issue pre-populates the prompt textarea with the issue's title, URL,
    body, and a ``---`` separator so the user can append additional context
    before submitting; the resulting ``NewSessionResult.prompt`` already
    contains the merged text and the caller passes it to the agent as-is.
    """

    DEFAULT_CSS = """
    NewSessionModal {
        align: center middle;
    }
    NewSessionModal > Vertical {
        width: 90%;
        max-width: 100;
        height: auto;
        max-height: 90%;
        background: $surface;
        border: round $accent;
        padding: 1 2;
    }
    NewSessionModal #body {
        height: 1fr;
        margin-top: 1;
        margin-bottom: 1;
    }
    NewSessionModal Label {
        height: 1;
        color: $text-muted;
    }
    NewSessionModal TextArea {
        height: 8;
        margin-bottom: 1;
    }
    NewSessionModal #issue {
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

    # Tell Textual which widget to focus on screen mount, BEFORE the first
    # paint. Without this, the screen auto-focuses the first focusable
    # widget in compose order — now the issue Select since it sits above
    # the prompt — and our ``on_mount`` reassignment to the prompt fires
    # one frame later, producing a visible flash on the issue picker.
    AUTO_FOCUS = "#prompt"

    def __init__(
        self,
        issues: list[Issue] | None = None,
        active_sessions_by_issue: dict[int, list[str]] | None = None,
    ) -> None:
        super().__init__()
        # Normalize ``None`` and ``[]`` to "no picker" via ``bool(self._issues)``;
        # the caller already collapses both gh-missing and empty-list cases to
        # ``None``, but we re-check here so the modal stays robust if invoked
        # directly (e.g. from tests) with an empty list.
        self._issues: list[Issue] = list(issues) if issues else []
        # Last text we auto-populated into the prompt textarea on issue
        # selection. Used to detect "user hasn't edited it" so re-selecting a
        # different issue replaces cleanly without stomping manual edits.
        self._last_autopopulated: str = ""
        # {issue_number: [session_id, ...]} for active (non-terminal) chud
        # sessions already linked to each issue. Used to decorate the picker
        # labels so the user can see an existing chud is on an issue before
        # spawning a duplicate. Empty by default — callers that don't pass
        # this in just get plain labels.
        self._active_by_issue: dict[int, list[str]] = dict(active_sessions_by_issue or {})

    @property
    def _has_issue_picker(self) -> bool:
        return bool(self._issues)

    def _format_issue_label(self, issue: Issue) -> str:
        """Render the dropdown label for an issue, prefixing a gear icon when
        an active chud session is already linked to it. The count is appended
        only when more than one chud is on the same issue.

        Examples:
          ``#42 — Tighten retries``           (no active session)
          ``⚙ #42 — Tighten retries``         (one active session)
          ``⚙×2 #42 — Tighten retries``       (multiple)
        """
        base = f"#{issue.number} — {issue.title}"
        active = self._active_by_issue.get(issue.number, [])
        if not active:
            return base
        if len(active) == 1:
            return f"{ACTIVE_CHUD_ICON} {base}"
        return f"{ACTIVE_CHUD_ICON}×{len(active)} {base}"

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static("[bold]New session[/bold]")
            body = VerticalScroll(id="body")
            # Let inner widgets own focus — matches QuestionModal's pattern so
            # tab-cycling lands on the prompt/options/effort controls rather
            # than the scroll container itself.
            body.can_focus = False
            with body:
                yield Label("Initial prompt:")
                yield TextArea("", id="prompt")
                if self._has_issue_picker:
                    yield Label("Link GitHub issue (optional):")
                    issue_choices = tuple(
                        (self._format_issue_label(i), str(i.number)) for i in self._issues
                    )
                    yield Select(
                        issue_choices,
                        id="issue",
                        allow_blank=True,
                        prompt="(none — start without an issue)",
                        tooltip=(
                            "Optionally pre-load a GitHub issue's title, URL, and body "
                            "into the agent's initial prompt. The text you typed above "
                            "is appended after a `---` separator."
                        ),
                    )
                defaults = user_default_options()
                # ``VerticalScroll`` is focusable by default; that adds a
                # spurious tab stop between the prompt and the first checkbox.
                # Disable focus on the container itself (mirrors the outer
                # ``body`` scroll above); a focused checkbox inside still
                # scrolls the parent into view.
                options_scroll = VerticalScroll(id="options-group")
                options_scroll.can_focus = False
                with options_scroll:
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

    def on_select_changed(self, event: Select.Changed) -> None:
        """Pre-populate the prompt textarea when the user picks an issue.

        Only overwrites when the textarea is empty or still holds the text we
        last auto-populated (so re-picking a different issue swaps cleanly,
        but manual edits aren't stomped). When the picker is cleared back to
        blank, restore the textarea to whatever it held before any
        auto-population.
        """
        if event.select.id != "issue":
            return
        prompt_area = self.query_one("#prompt", TextArea)
        current = prompt_area.text
        if current and current != self._last_autopopulated:
            self.app.notify(
                "Prompt has been edited; not overwriting. Clear it manually to "
                "load a different issue.",
                severity="warning",
            )
            return

        raw = event.value
        if not isinstance(raw, str):
            prompt_area.text = ""
            self._last_autopopulated = ""
            return
        issue = next((i for i in self._issues if str(i.number) == raw), None)
        if issue is None:
            return
        # Pass an empty user_prompt so we get just the issue head + body + "---"
        # separator; the user types their additions below the separator.
        merged = build_issue_prompt(issue, "")
        prompt_area.text = merged
        self._last_autopopulated = merged

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

    def _read_issue(self) -> Issue | None:
        """Resolve the ``Select#issue`` value to an ``Issue``, or ``None``.

        ``Select.value`` returns the ``BLANK`` sentinel (not a ``str``) when
        the user hasn't picked anything, which collapses to ``None`` here.
        Otherwise we look up the matching issue by number — comparing as
        strings since ``Select`` round-trips option values verbatim.
        """
        if not self._has_issue_picker:
            return None
        raw = self.query_one("#issue", Select).value
        if not isinstance(raw, str):
            return None
        return next((i for i in self._issues if str(i.number) == raw), None)

    def _submit(self) -> None:
        prompt = self.query_one("#prompt", TextArea).text.strip()
        if not prompt:
            return
        options = {
            opt.id: self.query_one(f"#opt-{opt.id}", Checkbox).value for opt in SESSION_OPTIONS
        }
        self.dismiss(
            NewSessionResult(
                prompt=prompt,
                options=options,
                effort=self._read_effort(),
                issue=self._read_issue(),
            )
        )
