from __future__ import annotations

from collections.abc import Iterable

from textual.widgets import Static


class ChudFooter(Static):
    """Bottom key-hint bar.

    A bespoke footer rather than Textual's ``Footer`` so it renders with a
    transparent background. Under ANSI color mode (which lets the host terminal
    background show through), ``Footer`` bakes explicit terminal-default
    backgrounds into its rendered content; those composite to the ANSI theme's
    foreground color and become an opaque, unreadable bar. A plain ``Static``
    with foreground-only markup keeps each cell's background unset, so the
    terminal shows through while the key glyphs stay legible.
    """

    DEFAULT_CSS = """
    ChudFooter {
        dock: bottom;
        height: 1;
        padding: 0 1;
        background: transparent;
        color: $foreground;
    }
    """

    def __init__(self, hints: Iterable[tuple[str, str]]) -> None:
        markup = "   ".join(f"[$accent]{key}[/] {label}" for key, label in hints)
        super().__init__(markup)
