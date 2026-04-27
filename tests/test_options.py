from __future__ import annotations

from chud.options import (
    EFFORT_VALUES,
    OPT_MAKE_DRAFT_PR,
    OPT_SELF_CLEANUP,
    SESSION_OPTIONS,
    default_options,
    normalize_effort,
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


def test_effort_values_contains_default_and_known_levels():
    assert None in EFFORT_VALUES
    for level in ("low", "medium", "high", "max"):
        assert level in EFFORT_VALUES


def test_normalize_effort_accepts_known_values():
    assert normalize_effort("low") == "low"
    assert normalize_effort("medium") == "medium"
    assert normalize_effort("high") == "high"
    assert normalize_effort("max") == "max"


def test_normalize_effort_handles_none():
    assert normalize_effort(None) is None


def test_normalize_effort_rejects_unknown():
    assert normalize_effort("turbo") is None
    assert normalize_effort("") is None
    assert normalize_effort(42) is None
    assert normalize_effort({"effort": "high"}) is None
