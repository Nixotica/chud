"""Shared base for chud's modal screens.

Adds vim-style ``j``/``k`` scroll bindings that route to a single
``VerticalScroll`` exposed by the subclass via :py:meth:`scroll_container`.
The bindings no-op while a text input owns focus, so the user can type
literal ``j``/``k`` into prompts without scrolling the modal underneath.
"""

from __future__ import annotations

from typing import TypeVar

from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Input, Select, TextArea
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
