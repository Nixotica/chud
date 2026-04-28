from __future__ import annotations

from pathlib import Path

import pytest

from chud import state as state_mod


@pytest.fixture(autouse=True)
def _isolate_user_config(tmp_path_factory, monkeypatch):
    """Redirect HOME and ``state.config_file()`` so tests never read the user's
    real config.

    Without this, tests that rely on default ``branch_prefix`` / ``include_slug`` /
    ``pr_body_footer`` / effort defaults fail on developer machines where those
    keys have been customized via the Settings modal or where
    ``~/.claude/settings.json`` exists. CI runners (with no such files) would
    then take a different code path — exactly the local/CI divergence we want to
    eliminate.
    """
    home = tmp_path_factory.mktemp("chud-home")
    monkeypatch.setenv("HOME", str(home))
    cfg = tmp_path_factory.mktemp("chud-cfg") / "config.json"
    monkeypatch.setattr(state_mod, "config_file", lambda: Path(cfg))
