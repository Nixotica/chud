"""Shared base for chud's modal screens.

Adds vim-style ``j``/``k`` scroll bindings that route to a single
``VerticalScroll`` exposed by the subclass via :py:meth:`scroll_container`.
The bindings no-op while a text input owns focus, so the user can type
literal ``j``/``k`` into prompts without scrolling the modal underneath.

Also provides shared ``j``/``k`` focus navigation between option Checkboxes
(those whose id starts with ``opt-``, the convention used for
``SESSION_OPTIONS``-backed checkboxes in both ``NewSessionModal`` and
``SettingsModal``).
"""

from __future__ import annotations

from typing import TypeVar

from textual import events
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Checkbox, Input, Select, TextArea
from textual.widgets._select import SelectOverlay

ScreenResultType = TypeVar("ScreenResultType")


class ScrollableModalScreen(ModalScreen[ScreenResultType]):
    """``ModalScreen`` base with vim-style j/k scroll over a single container.

    Subclasses override :py:meth:`scroll_container` to return the
    ``VerticalScroll`` (or any scrollable ``Widget`` exposing ``scroll_up`` /
    ``scroll_down``) that should respond to ``j``/``k``.
    """

    BINDINGS = [
        Binding("j", "vim_scroll_down", show=False),
        Binding("k", "vim_scroll_up", show=False),
    ]

    def scroll_container(self) -> VerticalScroll | None:
        """The scroll target for ``j``/``k``. Return ``None`` to disable."""
        return None

    def _vim_scroll(self, *, down: bool) -> None:
        # Choice widgets own j/k while focused — let them navigate options
        # rather than scrolling the modal body out from under the user.
        if isinstance(self.focused, Input | TextArea | Select | SelectOverlay):
            return
        container = self.scroll_container()
        if container is None:
            return
        (container.scroll_down if down else container.scroll_up)()

    def action_vim_scroll_down(self) -> None:
        self._vim_scroll(down=True)

    def action_vim_scroll_up(self) -> None:
        self._vim_scroll(down=False)

    def on_key(self, event: events.Key) -> None:
        """j/k focus between option Checkboxes (``opt-*`` id convention).

        Without this, the screen-level j/k bindings would scroll the modal
        body underneath a focused option Checkbox. We navigate in DOM order
        among Checkboxes whose id starts with ``opt-`` and, at the
        boundaries, fall through to ``focus_next``/``focus_previous`` so the
        user can exit the option group cleanly into adjacent fields.
        """
        if event.key not in ("j", "k"):
            return
        focused = self.focused
        if not isinstance(focused, Checkbox):
            return
        if not (focused.id or "").startswith("opt-"):
            return
        opts = [cb for cb in self.query(Checkbox) if (cb.id or "").startswith("opt-")]
        try:
            idx = opts.index(focused)
        except ValueError:
            return
        event.stop()
        event.prevent_default()
        target = idx + 1 if event.key == "j" else idx - 1
        if 0 <= target < len(opts):
            opts[target].focus()
        elif event.key == "j":
            self.focus_next()
        else:
            self.focus_previous()
