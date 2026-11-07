"""The walk is part of the suite, so a gate that stops refusing fails CI, not a reader."""

from __future__ import annotations

import importlib.util
import pathlib

_PATH = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "refusal_walk.py"


def _walk():
    spec = importlib.util.spec_from_file_location("refusal_walk", _PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_walk_meets_every_expectation(capsys):
    assert _walk().main() == 0
    out = capsys.readouterr().out
    assert "OK: 5 expected rows present, 2 controls absent, 0 unclassified" in out


def test_the_walk_fails_when_a_gate_loses_its_mapping(monkeypatch, capsys):
    from app import refusals

    monkeypatch.delitem(refusals.REASON_GATES, "recipient_not_allowlisted")
    assert _walk().main() == 1
    assert "missing expected row" in capsys.readouterr().out
