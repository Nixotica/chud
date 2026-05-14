"""``Select`` subclass with vim-style ``j``/``k`` navigation.

Drops Textual's stock type-to-search behavior in favor of explicit
``j``/``k`` bindings: while the overlay is open they map to
``cursor_down``/``cursor_up``; with the (closed) Select focused they open
the overlay, matching how ``down``/``up`` already behave.

Type-to-search may come back later as an explicit opt-in feature.
"""

from __future__ import annotations

from typing import Any

from textual.app import ComposeResult
from textual.binding import Binding
from textual.widgets import Select
from textual.widgets._select import SelectCurrent, SelectOverlay


class VimSelectOverlay(SelectOverlay):
    BINDINGS = [
        *SelectOverlay.BINDINGS,
        Binding("j", "cursor_down", show=False),
        Binding("k", "cursor_up", show=False),
    ]


class VimSelect(Select[Any]):
    BINDINGS = [
        *Select.BINDINGS,
        Binding("j,k", "show_overlay", show=False),
    ]

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        # type_to_search is dropped wholesale for chud selects; force it
        # off even if a caller passes True so we never regress.
        kwargs["type_to_search"] = False
        super().__init__(*args, **kwargs)

    def compose(self) -> ComposeResult:
        yield SelectCurrent(self.prompt)
        yield VimSelectOverlay(type_to_search=False).data_bind(compact=Select.compact)
