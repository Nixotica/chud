from __future__ import annotations

from chud.options import (
    OPT_MAKE_DRAFT_PR,
    OPT_SELF_CLEANUP,
    SESSION_OPTIONS,
    default_options,
    normalize_options,
)


def test_default_options_contains_every_registered_option():
    defaults = default_options()
    assert set(defaults.keys()) == {opt.id for opt in SESSION_OPTIONS}
    for opt in SESSION_OPTIONS:
        assert defaults[opt.id] == opt.default


def test_default_options_returns_fresh_dict_each_call():
    a = default_options()
    a[OPT_MAKE_DRAFT_PR] = True
    b = default_options()
    assert b[OPT_MAKE_DRAFT_PR] is False


def test_normalize_options_handles_none():
    assert normalize_options(None) == default_options()


def test_normalize_options_fills_missing_keys_with_defaults():
    out = normalize_options({OPT_MAKE_DRAFT_PR: True})
    assert out[OPT_MAKE_DRAFT_PR] is True
    assert out[OPT_SELF_CLEANUP] is False


def test_normalize_options_drops_unknown_keys():
    out = normalize_options({"not_a_real_option": True, OPT_SELF_CLEANUP: True})
    assert "not_a_real_option" not in out
    assert out[OPT_SELF_CLEANUP] is True


def test_normalize_options_coerces_truthy_values_to_bool():
    out = normalize_options({OPT_MAKE_DRAFT_PR: 1, OPT_SELF_CLEANUP: 0})  # type: ignore[dict-item]
    assert out[OPT_MAKE_DRAFT_PR] is True
    assert out[OPT_SELF_CLEANUP] is False
