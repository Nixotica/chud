from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from platformdirs import user_data_dir

from chud.types import KEY_EFFORT, SessionState


def data_root() -> Path:
    root = Path(user_data_dir("chud", appauthor=False))
    root.mkdir(parents=True, exist_ok=True)
    return root


def workspaces_root() -> Path:
    root = data_root() / "workspaces"
    root.mkdir(parents=True, exist_ok=True)
    return root


def sessions_file() -> Path:
    return data_root() / "sessions.json"


def config_file() -> Path:
    return data_root() / "config.json"


def load_all() -> dict[str, SessionState]:
    path = sessions_file()
    if not path.exists():
        return {}
    raw = json.loads(path.read_text())
    return {sid: SessionState.from_dict(d) for sid, d in raw.items()}


def save_all(sessions: dict[str, SessionState]) -> None:
    path = sessions_file()
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({sid: s.to_dict() for sid, s in sessions.items()}, indent=2))
    tmp.replace(path)


def load_user_config() -> dict[str, Any]:
    """Load persisted user defaults; return {} if missing or unreadable.

    The config dict is heterogeneous: it holds the boolean option toggles
    (keys in the ``SESSION_OPTIONS`` registry) alongside the per-user default
    ``effort`` string, so the value type is ``Any``.
    """
    path = config_file()
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text())
    except Exception:
        return {}
    return raw if isinstance(raw, dict) else {}


def save_user_config(config: dict[str, Any]) -> None:
    path = config_file()
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(config, indent=2))
    tmp.replace(path)


def user_default_options() -> dict[str, bool]:
    """Registry defaults overlaid with the user's saved preferences."""
    # Lazy import keeps the options → types → state graph acyclic.
    from chud.options import normalize_options

    return normalize_options(load_user_config())


def user_default_effort() -> str | None:
    """Persisted default effort, or ``None`` if unset / invalid."""
    from chud.options import normalize_effort

    return normalize_effort(load_user_config().get(KEY_EFFORT))


def claude_settings_effort() -> str | None:
    """Best-effort read of ``effortLevel`` from ``~/.claude/settings.json``.

    Lets chud surface the user's Claude Code-level effort preference as the
    new-session default when no chud-specific default has been saved.
    """
    from chud.options import normalize_effort

    path = Path.home() / ".claude" / "settings.json"
    try:
        raw = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict):
        return None
    return normalize_effort(raw.get("effortLevel"))


def get_recent_repo_paths() -> list[str]:
    """Distinct repo paths from past sessions, most-recently-active first.

    Reads `attached_repos` across every persisted `SessionState` and returns each
    unique repo toplevel path (the dict keys, already `str(toplevel)` per
    `WorktreeManager.attach_repo`). The list is sorted by the latest
    `last_activity_at` of any session that referenced the repo, descending, so the
    most-recently-touched repo comes first.
    """
    sessions = load_all()
    last_seen: dict[str, datetime] = {}
    for s in sessions.values():
        for repo_key in s.attached_repos:
            ts = s.last_activity_at
            if repo_key not in last_seen or ts > last_seen[repo_key]:
                last_seen[repo_key] = ts
    return [p for p, _ in sorted(last_seen.items(), key=lambda kv: kv[1], reverse=True)]
