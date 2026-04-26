"""Semantic helpers that wrap strings in Rich/Textual markup tags.

Naming reflects the UI role of the text (``TextError``, ``TextHeading``, …)
rather than the underlying tag, so re-skinning the TUI later is a search-
and-replace on this module instead of every call site.

Caller is responsible for escaping untrusted text — ``rich.markup.escape``
is re-exported here for convenience so call sites need only one import.
"""

from __future__ import annotations

from rich.markup import escape

__all__ = [
    "escape",
    "TextError",
    "TextSuccess",
    "TextPrompt",
    "TextDanger",
    "TextHeading",
    "TextMuted",
    "TextStatusChange",
    "TextStatus",
    "TextPath",
    "TextRepoLabel",
    "TextToolName",
    "TextPlanBanner",
    "TextAttached",
]


def _wrap(tag: str, text: str) -> str:
    return f"[{tag}]{text}[/{tag}]"


def TextError(msg: str) -> str:
    """Errors and blocking failures — bold red."""
    return _wrap("bold red", msg)


def TextSuccess(msg: str) -> str:
    """Success notifications — bold green."""
    return _wrap("bold green", msg)


def TextPrompt(msg: str) -> str:
    """Agent attention prompts — bold yellow."""
    return _wrap("bold yellow", msg)


def TextDanger(msg: str) -> str:
    """Destructive-action copy (non-bold) — red."""
    return _wrap("red", msg)


def TextHeading(msg: str) -> str:
    """Headings: modal titles, role labels, session ids — bold."""
    return _wrap("bold", msg)


def TextMuted(msg: str) -> str:
    """Secondary metadata and descriptions — dim."""
    return _wrap("dim", msg)


def TextStatusChange(msg: str) -> str:
    """Status-transition transcript line — dim italic."""
    return _wrap("dim italic", msg)


def TextStatus(msg: str) -> str:
    """Status enum value in the session header — cyan."""
    return _wrap("cyan", msg)


def TextPath(msg: str) -> str:
    """Filesystem paths — yellow."""
    return _wrap("yellow", msg)


def TextRepoLabel(msg: str) -> str:
    """Repository name labels — yellow."""
    return _wrap("yellow", msg)


def TextToolName(msg: str) -> str:
    """Tool names in the transcript — yellow."""
    return _wrap("yellow", msg)


def TextPlanBanner(msg: str) -> str:
    """Plan-proposed banner — bold magenta."""
    return _wrap("bold magenta", msg)


def TextAttached(msg: str) -> str:
    """Repo-attached notice — blue."""
    return _wrap("blue", msg)
