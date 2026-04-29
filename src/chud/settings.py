"""User-tunable global settings beyond the per-session boolean toggles.

Per-session toggles (``options.py``) flow from the New Session modal into one
session's ``SessionState``. The keys here are *global*: they affect every new
session at the moment it's created (branch naming) or finished (PR footer).

Persistence reuses ``state.config_file()`` — the same JSON document that backs
``options.py`` defaults — so the on-disk format is one shared dict-of-mixed-
types: bools for option toggles, strings/bools for the keys defined here. Each
getter validates the value's type and falls back to the registered default,
so a corrupt or hand-edited config can't crash startup.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from chud.state import load_user_config, save_user_config

log = logging.getLogger(__name__)

# Public keys (persisted in config.json — renaming is a breaking change).
KEY_BRANCH_PREFIX = "branch_prefix"
KEY_INCLUDE_SLUG = "include_slug"
KEY_PR_BODY_FOOTER = "pr_body_footer"

DEFAULT_BRANCH_PREFIX = "chud/"
DEFAULT_INCLUDE_SLUG = True
# Kept byte-for-byte compatible with the legacy hardcoded footer in pr.py so
# users with no config still see the original PR body.
DEFAULT_PR_BODY_FOOTER = "*Draft PR opened by chud session `{session_id}`.*"

# Git ref names tolerate slashes, dots, hyphens, underscores, alphanumerics —
# but reject spaces, ``..``, ``~``, ``^``, ``:``, ``?``, ``*``, ``[``, and a
# few others (man gitcheck-ref-format). Rather than reproduce the full grammar,
# strip any character outside a conservative whitelist; runs are collapsed and
# the result re-validated for emptiness by the caller's fallback.
_BRANCH_PREFIX_ALLOWED = re.compile(r"[^A-Za-z0-9._/\-]")


def sanitize_branch_prefix(raw: str) -> str:
    """Drop characters illegal in a git ref; collapse repeated separators.

    Empty input is allowed (means "no prefix"). The trailing slash is the
    user's choice — we don't append one — so ``agent/`` and ``agent-`` are
    both fine, and ``""`` produces unprefixed branch names.
    """
    cleaned = _BRANCH_PREFIX_ALLOWED.sub("", raw or "")
    # Git rejects consecutive dots in a ref segment; cheap collapse handles
    # the obvious case without a full ref-format walk.
    cleaned = re.sub(r"\.\.+", ".", cleaned)
    return cleaned


def get_branch_prefix() -> str:
    """Return the configured branch prefix, sanitized; default on missing/wrong type."""
    cfg = load_user_config()
    raw = cfg.get(KEY_BRANCH_PREFIX)
    if not isinstance(raw, str):
        return DEFAULT_BRANCH_PREFIX
    return sanitize_branch_prefix(raw)


def get_include_slug() -> bool:
    """Return whether to include the prompt slug in branch names; default on bad type."""
    cfg = load_user_config()
    raw = cfg.get(KEY_INCLUDE_SLUG)
    if not isinstance(raw, bool):
        return DEFAULT_INCLUDE_SLUG
    return raw


def get_pr_body_footer() -> str:
    """Return the PR body footer template; default on missing/wrong type."""
    cfg = load_user_config()
    raw = cfg.get(KEY_PR_BODY_FOOTER)
    if not isinstance(raw, str):
        return DEFAULT_PR_BODY_FOOTER
    return raw


def render_pr_body_footer(session_id: str) -> str:
    """Substitute ``{session_id}`` into the configured footer template.

    Unknown placeholders in a user-supplied template would raise ``KeyError``
    from ``str.format`` and crash PR creation; swallow that and return the
    template literal so the PR still opens (the footer just won't render
    correctly — better than failing the whole publish).
    """
    template = get_pr_body_footer()
    try:
        return template.format(session_id=session_id)
    except (KeyError, IndexError, ValueError) as e:
        log.warning("pr_body_footer template error (%s); using literal template", e)
        return template


def load_settings() -> dict[str, Any]:
    """Snapshot of the settings keys this module owns, with defaults filled in.

    Used by the Settings modal to populate its initial widget values. Per-
    session option toggles (``make_draft_pr``, ``self_cleanup``) are *not*
    included — those come from ``options.user_default_options()``.
    """
    return {
        KEY_BRANCH_PREFIX: get_branch_prefix(),
        KEY_INCLUDE_SLUG: get_include_slug(),
        KEY_PR_BODY_FOOTER: get_pr_body_footer(),
    }


def save_settings(values: dict[str, Any]) -> None:
    """Merge ``values`` into the existing user config and persist atomically.

    The merge preserves keys this module doesn't own (i.e. per-session option
    booleans saved by other code paths), so settings and options share one
    config file without stomping on each other.
    """
    cfg = load_user_config()
    cfg.update(values)
    save_user_config(cfg)
