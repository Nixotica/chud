from __future__ import annotations

import contextlib
import json
from typing import Any

from textual import events, on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.content import Content
from textual.geometry import Size
from textual.style import Style
from textual.widget import Widget
from textual.widgets import Button, Checkbox, Input, RadioButton, RadioSet, Static

from chud.markup import TextHeading, TextMuted
from chud.widgets._scrollable_modal import ScrollableModalScreen


def _toggle_button_with_off_glyph(self: Checkbox | RadioButton, inner_off: str) -> Content:
    """Render the toggle button cell, swapping glyph by ``self.value``.

    Textual 8.x's default ``ToggleButton._button`` renders ``BUTTON_INNER``
    in both states and only flips its colour. We want a different character
    per state — ✓ when on, ``inner_off`` when off — so we recreate the
    assembly with a value-dependent inner.
    """
    button_style = self.get_visual_style("toggle--button")
    side_style = Style(
        foreground=button_style.background,
        background=self.background_colors[1],
    )
    inner = self.BUTTON_INNER if self.value else inner_off
    return Content.assemble(
        (self.BUTTON_LEFT, side_style),
        (inner, button_style),
        (self.BUTTON_RIGHT, side_style),
    )


# Heuristic: an option is "long enough that the user might want to expand it"
# when its rendered single-line form exceeds this many columns. Picked to
# roughly match the modal's content width minus the toggle glyph and padding.
_LONG_OPTION_THRESHOLD = 60


class _ExpandableOption:
    """Mixin for ``Checkbox``/``RadioButton`` that allows multi-line expansion.

    The base ``ToggleButton`` strips labels to a single line in
    ``_make_label`` and hard-codes ``get_content_height`` to ``1``. We
    override both so that, when ``set_expanded(True)`` is called, the
    widget's label can carry an embedded description across multiple lines.
    """

    _option_label: str
    _option_description: str
    _expanded: bool

    def _init_option(self, label_text: str, description: str) -> None:
        self._option_label = label_text
        self._option_description = (description or "").strip()
        self._expanded = False

    @property
    def is_long(self) -> bool:
        base = self._option_label
        if self._option_description:
            base = f"{base} — {self._option_description}"
        return len(base) > _LONG_OPTION_THRESHOLD

    def _compute_label(self) -> Content:
        dim = Style(dim=True)
        if self._expanded:
            text = Content(self._option_label)
            if self._option_description:
                text = text.append("\n").append_text(self._option_description, dim)
            text = text.append("  ").append_text("(← collapse)", dim)
            return text
        # Collapsed: build a single-line content. If the option is long
        # enough that its base text won't fit at the current width, truncate
        # ourselves and append ``… (→)`` so the expand affordance is visible.
        base = self._option_label
        if self._option_description:
            base = f"{base} — {self._option_description}"
        if not self.is_long:
            return Content(base)
        # ToggleButton.render prepends a 3-cell button glyph + 1-cell spacer.
        button_width = 4
        # Fallback width for the moment between __init__ and the first
        # ``on_resize``; gets corrected once layout settles. ``self.size`` is
        # only available after the Widget base init runs, so guard the read.
        try:
            current_width: int = self.size.width  # type: ignore[attr-defined]
        except (RuntimeError, AttributeError):
            current_width = 0
        width = current_width if current_width > 0 else 80
        available = max(10, width - button_width)
        hint_truncated = "… (→)"
        hint_full = "  (→)"
        if len(base) + len(hint_full) <= available:
            return Content(base).append("  ").append_text("(→)", dim)
        cut = max(1, available - len(hint_truncated))
        return Content(base[:cut].rstrip()).append_text(hint_truncated, dim)

    # --- ToggleButton overrides --------------------------------------------

    def _make_label(self, label: Any) -> Content:  # type: ignore[override]
        # Parent strips to ``first_line.rstrip()``; we need to keep newlines
        # so the expanded view can render across multiple rows.
        if isinstance(label, Content):
            return label
        return Content.from_text(label)

    def get_content_height(  # type: ignore[override]
        self, container: Size, viewport: Size, width: int
    ) -> int:
        if not self._expanded:
            return 1
        # Account for the 3-cell button glyph + 1-cell spacer that
        # ``ToggleButton.render`` prepends to the label.
        button_width = 3 + 1
        available = max(1, width - button_width)
        plain: str = self.label.plain  # type: ignore[attr-defined]
        total = 0
        for line in plain.split("\n"):
            total += max(1, -(-max(1, len(line)) // available))
        return max(1, total)

    def set_expanded(self, value: bool) -> None:
        if self._expanded == value:
            return
        self._expanded = value
        # ToggleButton's label setter calls ``self._make_label`` (overridden
        # above) and triggers a layout-changing refresh.
        self.label = self._compute_label()  # type: ignore[attr-defined]

    def _refresh_collapsed_label(self) -> None:
        """Recompute the collapsed label against the current widget width.

        The collapsed form truncates to fit the visible row; we don't know
        the row's width until after layout, so we recompute on mount and on
        each resize. Expanded labels don't need this — they wrap freely.
        """
        if self._expanded:
            return
        self.label = self._compute_label()  # type: ignore[attr-defined]

    def on_mount(self) -> None:
        self._refresh_collapsed_label()

    def on_resize(self, event: events.Resize) -> None:
        del event
        self._refresh_collapsed_label()


class _CheckMarkBox(_ExpandableOption, Checkbox):
    BUTTON_INNER = "✓"
    BUTTON_INNER_OFF = "X"

    def __init__(self, label_text: str, description: str = "", **kwargs: Any) -> None:
        self._init_option(label_text, description)
        super().__init__(self._compute_label(), **kwargs)

    @property
    def _button(self) -> Content:
        return _toggle_button_with_off_glyph(self, self.BUTTON_INNER_OFF)


class _CheckMarkRadio(_ExpandableOption, RadioButton):
    BUTTON_INNER = "✓"
    BUTTON_INNER_OFF = "●"

    def __init__(self, label_text: str, description: str = "", **kwargs: Any) -> None:
        self._init_option(label_text, description)
        super().__init__(self._compute_label(), **kwargs)

    @property
    def _button(self) -> Content:
        return _toggle_button_with_off_glyph(self, self.BUTTON_INNER_OFF)


class QuestionModal(ScrollableModalScreen[str | None]):
    """Render an AskUserQuestion tool call and collect the user's answer."""

    DEFAULT_CSS = """
    QuestionModal {
        align: center middle;
    }
    QuestionModal > Vertical {
        width: 90%;
        max-width: 120;
        height: 80%;
        background: $surface;
        border: round $accent;
        padding: 1 2;
    }
    QuestionModal #question-title {
        height: 1;
        color: $accent;
    }
    QuestionModal VerticalScroll {
        height: 1fr;
        margin-top: 1;
        margin-bottom: 1;
    }
    QuestionModal .question-header {
        color: $accent;
        margin-top: 1;
    }
    QuestionModal .question-text {
        margin-bottom: 1;
        height: auto;
    }
    QuestionModal #other-input {
        height: 3;
        margin-top: 1;
    }
    QuestionModal Input.free-input {
        height: 3;
        margin-bottom: 1;
    }
    QuestionModal Checkbox {
        height: auto;
        min-height: 1;
        margin: 0;
        padding: 0;
        border: none;
        background: transparent;
        text-wrap: wrap;
        text-overflow: clip;
    }
    QuestionModal Checkbox:focus {
        border: none;
        background: $boost;
    }
    QuestionModal RadioSet {
        border: none;
        padding: 0;
        margin: 0;
        background: transparent;
        height: auto;
        width: 1fr;
    }
    QuestionModal RadioSet:focus {
        background: $boost;
    }
    QuestionModal RadioButton {
        height: auto;
        min-height: 1;
        margin: 0;
        padding: 0;
        border: none;
        background: transparent;
        text-wrap: wrap;
        text-overflow: clip;
    }
    QuestionModal RadioButton:focus {
        background: $boost;
    }
    QuestionModal Horizontal {
        height: 3;
        align: right middle;
    }
    QuestionModal Button {
        margin-left: 2;
    }
    """

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("f2", "submit", "Submit", priority=True),
    ]

    def __init__(self, session_id: str, question_input: dict[str, Any]) -> None:
        super().__init__()
        self.session_id = session_id
        self.question_input = question_input
        # Normalize to a list of dicts; tolerate malformed input by falling
        # back to a single synthetic question whose body is the raw JSON.
        raw_questions = question_input.get("questions")
        if isinstance(raw_questions, list) and raw_questions:
            self.questions: list[dict[str, Any]] = [
                q if isinstance(q, dict) else {"question": str(q)} for q in raw_questions
            ]
        else:
            self.questions = [
                {
                    "question": "(unrecognized AskUserQuestion shape — raw input below)",
                    "options": [],
                    "multiSelect": False,
                    "_raw": json.dumps(question_input, default=str, indent=2),
                }
            ]

    def scroll_container(self) -> VerticalScroll | None:
        try:
            return self.query_one(VerticalScroll)
        except Exception:
            return None

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(
                TextHeading(f"Question from session {self.session_id[:8]}"),
                id="question-title",
            )
            scroll = VerticalScroll()
            scroll.can_focus = False
            with scroll:
                for idx, q in enumerate(self.questions):
                    header = str(q.get("header") or "").strip()
                    if header:
                        yield Static(TextHeading(header), classes="question-header")
                    # Question body wraps naturally; no expand/collapse.
                    yield Static(
                        str(q.get("question") or "(no question text)"),
                        classes="question-text",
                        markup=False,
                    )
                    raw = q.get("_raw")
                    if raw:
                        yield Static(TextMuted(str(raw)))
                    options = q.get("options") or []
                    multi = bool(q.get("multiSelect"))
                    if not options:
                        yield Input(
                            placeholder="Type your answer…",
                            id=f"q{idx}-free",
                            classes="free-input",
                        )
                        continue
                    if multi:
                        for opt_idx, opt in enumerate(options):
                            label, desc = _split_option(opt)
                            yield _CheckMarkBox(label, desc, id=f"q{idx}-opt{opt_idx}")
                    else:
                        with RadioSet(id=f"q{idx}-radio"):
                            for opt_idx, opt in enumerate(options):
                                label, desc = _split_option(opt)
                                yield _CheckMarkRadio(label, desc, id=f"q{idx}-opt{opt_idx}")
            yield Input(
                placeholder="Other / free-text answer (optional)…",
                id="other-input",
            )
            with Horizontal():
                cancel_btn = Button("Cancel (Esc)", id="cancel", variant="error")
                cancel_btn.can_focus = False
                yield cancel_btn
                submit_btn = Button("Submit (F2)", id="submit", variant="success")
                submit_btn.can_focus = False
                yield submit_btn

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "submit":
            self._submit()
        elif event.button.id == "cancel":
            self.dismiss(None)

    @on(Input.Submitted, "#other-input")
    def _on_other_submitted(self, event: Input.Submitted) -> None:
        self._submit()

    @on(RadioSet.Changed)
    def _on_radio_changed(self, event: RadioSet.Changed) -> None:
        """Selecting a single-choice option advances to the next question."""
        idx = self._question_index_of(event.radio_set)
        if idx is None:
            return
        self._advance_from(idx)

    @on(Input.Submitted, "Input.free-input")
    def _on_free_submitted(self, event: Input.Submitted) -> None:
        idx = self._question_index_of(event.input)
        if idx is None:
            self._submit()
            return
        self._advance_from(idx)

    def action_cancel(self) -> None:
        self.dismiss(None)

    def action_submit(self) -> None:
        self._submit()

    def on_mount(self) -> None:
        """Place initial focus on the first interactive widget."""
        for idx in range(len(self.questions)):
            target = self._question_target_id(idx)
            if target is None:
                continue
            try:
                self.query_one(target).focus()
                return
            except Exception:
                continue
        with contextlib.suppress(Exception):
            self.query_one("#other-input", Input).focus()

    def _question_target_id(self, idx: int) -> str | None:
        """CSS selector for the first focusable response widget of question ``idx``."""
        if idx < 0 or idx >= len(self.questions):
            return None
        q = self.questions[idx]
        options = q.get("options") or []
        if not options:
            return f"#q{idx}-free"
        multi = bool(q.get("multiSelect"))
        return f"#q{idx}-opt0" if multi else f"#q{idx}-radio"

    def _question_last_target_id(self, idx: int) -> str | None:
        """CSS selector for the *last* focusable response widget of question ``idx``."""
        if idx < 0 or idx >= len(self.questions):
            return None
        q = self.questions[idx]
        options = q.get("options") or []
        if not options:
            return f"#q{idx}-free"
        if bool(q.get("multiSelect")):
            return f"#q{idx}-opt{len(options) - 1}"
        return f"#q{idx}-radio"

    def _question_index_of(self, widget: Widget | None) -> int | None:
        """Walk up from ``widget`` to find the question index encoded in its id."""
        node: Widget | None = widget
        while node is not None:
            wid = node.id or ""
            if wid.startswith("q") and "-" in wid:
                with contextlib.suppress(ValueError):
                    return int(wid[1 : wid.index("-")])
            node = getattr(node, "parent", None)
        return None

    def _advance_from(self, idx: int) -> None:
        """Focus the next question's response widget, or fall back to ``#other-input``."""
        for next_idx in range(idx + 1, len(self.questions)):
            target = self._question_target_id(next_idx)
            if target is None:
                continue
            try:
                self.query_one(target).focus()
                return
            except Exception:
                continue
        with contextlib.suppress(Exception):
            self.query_one("#other-input", Input).focus()

    def _retreat_to(self, idx: int) -> None:
        """Focus the previous question's last response widget, if any."""
        for prev_idx in range(idx - 1, -1, -1):
            target = self._question_last_target_id(prev_idx)
            if target is None:
                continue
            try:
                self.query_one(target).focus()
                return
            except Exception:
                continue

    def _focused_option(self) -> _CheckMarkBox | _CheckMarkRadio | None:
        """The expandable option currently under focus, if any.

        For multi-select Checkboxes the focused widget *is* the option. For
        single-select RadioSets the RadioSet itself is focused, so we resolve
        the highlighted child via :pyattr:`RadioSet._selected`.
        """
        focused = self.focused
        if isinstance(focused, _CheckMarkBox):
            return focused
        if isinstance(focused, RadioSet):
            idx = getattr(focused, "_selected", None)
            if not isinstance(idx, int) or idx < 0:
                return None
            radios = [c for c in focused.children if isinstance(c, _CheckMarkRadio)]
            if 0 <= idx < len(radios):
                return radios[idx]
        return None

    def on_key(self, event: events.Key) -> None:
        """Keyboard nav.

        Right/left expand/collapse the *focused* option (or the highlighted
        radio inside a focused RadioSet). Up/down moves between Checkboxes
        in the same multi-select question, falling through to adjacent
        questions at the boundaries. j/k mirror up/down for vim-style nav,
        including inside a focused RadioSet (Textual's RadioSet only binds
        up/down/left/right natively). Inputs keep their own cursor handling
        because we early-return on ``Input``-focused widgets.
        """
        focused = self.focused
        if focused is None or isinstance(focused, Input):
            return

        if event.key in ("right", "left", "l", "h"):
            target = self._focused_option()
            if target is None or not target.is_long:
                return
            expand = event.key in ("right", "l")
            if target._expanded != expand:
                target.set_expanded(expand)
                event.stop()
                event.prevent_default()
            return

        if event.key in ("up", "down", "j", "k") and isinstance(focused, Checkbox):
            wid = focused.id or ""
            if "-opt" not in wid:
                return
            try:
                q_idx = int(wid[1 : wid.index("-")])
                opt_idx = int(wid.split("-opt", 1)[1])
            except ValueError:
                return
            q = self.questions[q_idx]
            count = len(q.get("options") or [])
            event.stop()
            event.prevent_default()
            if event.key in ("down", "j"):
                if opt_idx + 1 < count:
                    with contextlib.suppress(Exception):
                        self.query_one(f"#q{q_idx}-opt{opt_idx + 1}", Checkbox).focus()
                else:
                    self._advance_from(q_idx)
            else:
                if opt_idx - 1 >= 0:
                    with contextlib.suppress(Exception):
                        self.query_one(f"#q{q_idx}-opt{opt_idx - 1}", Checkbox).focus()
                else:
                    self._retreat_to(q_idx)
            return

        if event.key in ("up", "down", "j", "k") and isinstance(focused, RadioSet):
            # RadioSet binds up/down/left/right natively but not j/k, and at
            # the first/last radio neither set crosses into the adjacent
            # question. We fill both gaps here so single-select questions
            # navigate identically to multi-select ones.
            radios = [c for c in focused.children if isinstance(c, _CheckMarkRadio)]
            if not radios:
                return
            selected = focused._selected if isinstance(focused._selected, int) else 0
            going_down = event.key in ("down", "j")
            at_boundary = (going_down and selected >= len(radios) - 1) or (
                not going_down and selected <= 0
            )
            q_idx = self._question_index_of(focused)
            if at_boundary and q_idx is not None:
                event.stop()
                event.prevent_default()
                if going_down:
                    self._advance_from(q_idx)
                else:
                    self._retreat_to(q_idx)
                return
            if event.key in ("j", "k"):
                event.stop()
                event.prevent_default()
                with contextlib.suppress(Exception):
                    if going_down:
                        focused.action_next_button()
                    else:
                        focused.action_previous_button()
                return

    def _submit(self) -> None:
        parts: list[str] = []
        for idx, q in enumerate(self.questions):
            options = q.get("options") or []
            label = q.get("header") or q.get("question") or f"Q{idx + 1}"
            short = str(label).strip().splitlines()[0][:60]
            if not options:
                try:
                    free = self.query_one(f"#q{idx}-free", Input).value.strip()
                except Exception:
                    free = ""
                if free:
                    parts.append(f"{short}: {free}")
                continue
            multi = bool(q.get("multiSelect"))
            chosen_labels: list[str] = []
            if multi:
                for opt_idx, opt in enumerate(options):
                    try:
                        cb = self.query_one(f"#q{idx}-opt{opt_idx}", Checkbox)
                    except Exception:
                        continue
                    if cb.value:
                        chosen_labels.append(_option_label(opt))
            else:
                try:
                    rs = self.query_one(f"#q{idx}-radio", RadioSet)
                except Exception:
                    rs = None
                if rs is not None and rs.pressed_index >= 0:
                    chosen = options[rs.pressed_index]
                    chosen_labels.append(_option_label(chosen))
            answer = ", ".join(chosen_labels) if chosen_labels else "(no selection)"
            parts.append(f"{short}: {answer}")

        try:
            other = self.query_one("#other-input", Input).value.strip()
        except Exception:
            other = ""
        summary = " | ".join(parts) if parts else ""
        if other:
            summary = f"{summary}; other: {other}" if summary else other
        if not summary:
            summary = "(no selection)"
        self.dismiss(summary)


def _option_label(opt: Any) -> str:
    if isinstance(opt, dict):
        return str(opt.get("label") or opt.get("value") or opt)
    return str(opt)


def _split_option(opt: Any) -> tuple[str, str]:
    """Return ``(label, description)`` for an option dict or scalar."""
    if isinstance(opt, dict):
        label = str(opt.get("label") or opt.get("value") or opt)
        desc = str(opt.get("description") or "").strip()
        return label, desc
    return str(opt), ""
