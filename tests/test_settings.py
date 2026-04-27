from __future__ import annotations

from pathlib import Path

from chud import settings as settings_mod
from chud import state as state_mod


def _redirect_config(tmp_path: Path, monkeypatch) -> Path:
    """Point ``state.config_file`` at a tmp dir so tests don't touch real config."""
    cfg = tmp_path / "config.json"
    monkeypatch.setattr(state_mod, "config_file", lambda: cfg)
    return cfg


# ---------------------------------------------------------------- defaults


def test_get_branch_prefix_default_when_missing(tmp_path, monkeypatch):
    _redirect_config(tmp_path, monkeypatch)
    assert settings_mod.get_branch_prefix() == settings_mod.DEFAULT_BRANCH_PREFIX


def test_get_include_slug_default_when_missing(tmp_path, monkeypatch):
    _redirect_config(tmp_path, monkeypatch)
    assert settings_mod.get_include_slug() is settings_mod.DEFAULT_INCLUDE_SLUG


def test_get_pr_body_footer_default_when_missing(tmp_path, monkeypatch):
    _redirect_config(tmp_path, monkeypatch)
    assert settings_mod.get_pr_body_footer() == settings_mod.DEFAULT_PR_BODY_FOOTER


# ---------------------------------------------------------------- round-trip


def test_save_settings_round_trip(tmp_path, monkeypatch):
    _redirect_config(tmp_path, monkeypatch)
    settings_mod.save_settings(
        {
            settings_mod.KEY_BRANCH_PREFIX: "agent/",
            settings_mod.KEY_INCLUDE_SLUG: False,
            settings_mod.KEY_PR_BODY_FOOTER: "made by chud :: {session_id}",
        }
    )
    assert settings_mod.get_branch_prefix() == "agent/"
    assert settings_mod.get_include_slug() is False
    assert settings_mod.get_pr_body_footer() == "made by chud :: {session_id}"


def test_save_settings_preserves_unrelated_keys(tmp_path, monkeypatch):
    """A save mustn't drop the per-session option booleans living in the same file."""
    _redirect_config(tmp_path, monkeypatch)
    state_mod.save_user_config({"make_draft_pr": True, "self_cleanup": True})
    settings_mod.save_settings({settings_mod.KEY_BRANCH_PREFIX: "x/"})

    cfg = state_mod.load_user_config()
    assert cfg.get("make_draft_pr") is True
    assert cfg.get("self_cleanup") is True
    assert cfg.get(settings_mod.KEY_BRANCH_PREFIX) == "x/"


# ---------------------------------------------------------------- validation


def test_get_branch_prefix_falls_back_on_wrong_type(tmp_path, monkeypatch):
    _redirect_config(tmp_path, monkeypatch)
    state_mod.save_user_config({settings_mod.KEY_BRANCH_PREFIX: 42})
    assert settings_mod.get_branch_prefix() == settings_mod.DEFAULT_BRANCH_PREFIX


def test_get_include_slug_falls_back_on_wrong_type(tmp_path, monkeypatch):
    _redirect_config(tmp_path, monkeypatch)
    # Non-bool truthy value: we don't coerce here; callers expect a real bool.
    state_mod.save_user_config({settings_mod.KEY_INCLUDE_SLUG: "yes"})
    assert settings_mod.get_include_slug() is settings_mod.DEFAULT_INCLUDE_SLUG


def test_sanitize_branch_prefix_strips_illegal_chars():
    # Spaces, ``~`` and ``?`` are invalid in a git ref.
    assert settings_mod.sanitize_branch_prefix("ag ent/?~") == "agent/"


def test_sanitize_branch_prefix_collapses_double_dots():
    # ``..`` is rejected by git ref parsing; we collapse it.
    assert settings_mod.sanitize_branch_prefix("foo..bar/") == "foo.bar/"


def test_sanitize_branch_prefix_allows_empty():
    assert settings_mod.sanitize_branch_prefix("") == ""


# ---------------------------------------------------------------- footer rendering


def test_render_pr_body_footer_substitutes_session_id(tmp_path, monkeypatch):
    _redirect_config(tmp_path, monkeypatch)
    settings_mod.save_settings(
        {settings_mod.KEY_PR_BODY_FOOTER: "session={session_id}"}
    )
    assert settings_mod.render_pr_body_footer("abc123") == "session=abc123"


def test_render_pr_body_footer_default_matches_legacy(tmp_path, monkeypatch):
    """Default footer string matches the original hardcoded blurb."""
    _redirect_config(tmp_path, monkeypatch)
    out = settings_mod.render_pr_body_footer("sess1")
    assert out == "*Draft PR opened by chud session `sess1`.*"


def test_render_pr_body_footer_unknown_placeholder_is_safe(tmp_path, monkeypatch):
    """An unknown ``{placeholder}`` must not crash PR creation."""
    _redirect_config(tmp_path, monkeypatch)
    settings_mod.save_settings(
        {settings_mod.KEY_PR_BODY_FOOTER: "session={session_id} extra={oops}"}
    )
    # Falls back to the literal template string instead of raising.
    out = settings_mod.render_pr_body_footer("abc")
    assert "{oops}" in out


# ---------------------------------------------------------------- snapshot


def test_load_settings_returns_full_snapshot(tmp_path, monkeypatch):
    _redirect_config(tmp_path, monkeypatch)
    snap = settings_mod.load_settings()
    assert set(snap.keys()) == {
        settings_mod.KEY_BRANCH_PREFIX,
        settings_mod.KEY_INCLUDE_SLUG,
        settings_mod.KEY_PR_BODY_FOOTER,
    }
    assert snap[settings_mod.KEY_BRANCH_PREFIX] == settings_mod.DEFAULT_BRANCH_PREFIX
    assert snap[settings_mod.KEY_INCLUDE_SLUG] is settings_mod.DEFAULT_INCLUDE_SLUG
    assert snap[settings_mod.KEY_PR_BODY_FOOTER] == settings_mod.DEFAULT_PR_BODY_FOOTER
