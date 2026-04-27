"""Unit tests for the pre-release dev scenario loader."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from chud.dev import _load_seed


def test_load_seed_reads_explicit_path(tmp_path: Path) -> None:
    seed = tmp_path / "seed.json"
    seed.write_text(json.dumps({"questions": [{"question": "hi"}]}))
    out = _load_seed(seed, default=tmp_path / "absent.json")
    assert out == {"questions": [{"question": "hi"}]}


def test_load_seed_falls_back_to_default(tmp_path: Path) -> None:
    default = tmp_path / "default.json"
    default.write_text(json.dumps({"questions": [{"question": "fallback"}]}))
    out = _load_seed(None, default=default)
    assert out == {"questions": [{"question": "fallback"}]}


def test_load_seed_rejects_non_object(tmp_path: Path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps([1, 2, 3]))
    with pytest.raises(ValueError):
        _load_seed(bad, default=tmp_path / "absent.json")


def test_load_seed_raises_on_missing(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        _load_seed(None, default=tmp_path / "nope.json")


def test_load_seed_raises_on_malformed_json(tmp_path: Path) -> None:
    bad = tmp_path / "broken.json"
    bad.write_text("{not json")
    with pytest.raises(json.JSONDecodeError):
        _load_seed(bad, default=tmp_path / "absent.json")
