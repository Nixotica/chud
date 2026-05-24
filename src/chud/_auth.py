"""Resolve a GitHub auth token for the githubkit client.

Resolution order (first match wins):

1. ``GH_TOKEN`` env var (non-empty).
2. ``GITHUB_TOKEN`` env var (non-empty).
3. ``~/.config/gh/hosts.yml`` — extract ``["github.com"]["oauth_token"]``,
   the same field ``gh auth login`` writes. Picking it up here preserves
   the "just works after gh auth login" UX without shelling out to ``gh``.

Returns ``None`` if nothing resolves; callers treat that as "no GitHub
features" and degrade silently (empty issue picker, ``PRResult.error``
for publish attempts).

The token can't change mid-process (env vars are read once, ``hosts.yml``
is owned by the user's shell), so the resolver is memoized via
``functools.cache``.
"""

from __future__ import annotations

import logging
import os
from functools import cache
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger(__name__)

_HOSTS_YML = Path.home() / ".config" / "gh" / "hosts.yml"


def _read_env_token() -> str | None:
    for var in ("GH_TOKEN", "GITHUB_TOKEN"):
        val = os.environ.get(var)
        if val:
            return val
    return None


def _read_hosts_yml_token() -> str | None:
    """Best-effort read of ``~/.config/gh/hosts.yml``.

    Any failure (file missing, malformed YAML, missing key, wrong shape)
    collapses to ``None`` so callers fall through to the next layer
    instead of crashing the TUI on an unexpected gh config.
    """
    try:
        with _HOSTS_YML.open("r", encoding="utf-8") as fp:
            data: Any = yaml.safe_load(fp)
        token = data["github.com"]["oauth_token"]
        if isinstance(token, str) and token:
            return token
    except Exception:
        log.debug("hosts.yml token read failed", exc_info=True)
    return None


@cache
def resolve_github_token() -> str | None:
    """Return the resolved GitHub token, or ``None`` if nothing is configured."""
    return _read_env_token() or _read_hosts_yml_token()
