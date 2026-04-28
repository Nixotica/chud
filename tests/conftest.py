from __future__ import annotations

from pathlib import Path

import pytest

from chud import state as state_mod


@pytest.fixture(autouse=True)
def _isolate_user_config(tmp_path_factory, monkeypatch):
    """Redirect ``state.config_file()`` so tests never read the user's real config.

    Without this, tests that rely on default ``branch_prefix`` / ``include_slug`` /
    ``pr_body_footer`` values fail on developer machines where those keys have
    been customized via the Settings modal.
    """
    cfg = tmp_path_factory.mktemp("chud-cfg") / "config.json"
    monkeypatch.setattr(state_mod, "config_file", lambda: Path(cfg))
