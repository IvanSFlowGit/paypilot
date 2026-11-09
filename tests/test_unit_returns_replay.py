"""The Unit replay CLI: three exit states, signature check, golden output, no sends. Synthetic data only."""

from __future__ import annotations

import base64
import hashlib
import hmac
import importlib.util
import io
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("unit_returns_replay", ROOT / "scripts" / "unit_returns_replay.py")
cli = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cli)

DEMO = ROOT / "examples" / "unit-demo" / "delivery.json"
SECRET = "test-secret-not-a-real-one"


def run(argv):
    buf = io.StringIO()
    return cli.main(argv, out=buf), buf.getvalue()


def sign(raw):
    return base64.b64encode(hmac.new(SECRET.encode(), raw, hashlib.sha1).digest()).decode()


def write(tmp_path, events):
    f = tmp_path / "d.json"
    f.write_bytes(json.dumps({"data": events}).encode())
    return f


def ev(eid, reason, etype="payment.returned"):
    attrs = {"amount": 1}
    if reason:
        attrs["reason"] = reason
    return {"id": eid, "type": etype, "attributes": attrs, "relationships": {"payment": {"data": {"id": "p" + eid}}}}


def test_the_bundled_demo_matches_its_committed_output():
    code, text = run(["--events", str(DEMO)])
    fresh = "\n".join(x for x in text.splitlines() if not x.startswith("subject:")) + "\n"
    assert code == 1
    assert (ROOT / "examples" / "unit-demo" / "expected.txt").read_text() == fresh
    assert "events: 8 read, 5 decided, 2 held, 1 not a return" in text


def test_all_decided_is_exit_zero(tmp_path):
    code, text = run(["--events", str(write(tmp_path, [ev("1", "InsufficientFunds"), ev("2", "AccountClosed")]))])
    assert code == 0 and "2 decided, 0 held" in text


def test_a_held_event_is_exit_one_and_listed(tmp_path):
    code, text = run(["--events", str(write(tmp_path, [ev("1", None)]))])
    assert code == 1 and "held 1: reason_not_in_event" in text


@pytest.mark.parametrize("content", [b"not json", b"{}", b'{"data": []}'])
def test_a_malformed_file_is_exit_two_and_prints_no_report(tmp_path, content):
    f = tmp_path / "d.json"
    f.write_bytes(content)
    code, text = run(["--events", str(f)])
    assert code == 2 and "Nothing was decided." in text and "events:" not in text


def test_a_relative_or_missing_path_is_exit_two(tmp_path):
    assert run(["--events", "d.json"])[0] == 2
    assert run(["--events", str(tmp_path / "nope.json")])[0] == 2


def test_a_correct_signature_is_verified(tmp_path, monkeypatch):
    monkeypatch.setenv(cli.SECRET_ENV, SECRET)
    f = write(tmp_path, [ev("1", "InsufficientFunds")])
    code, text = run(["--events", str(f), "--signature", sign(f.read_bytes())])
    assert code == 0 and "signature: verified" in text


def test_a_wrong_signature_is_exit_two_and_decides_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv(cli.SECRET_ENV, SECRET)
    f = write(tmp_path, [ev("1", "InsufficientFunds")])
    code, text = run(["--events", str(f), "--signature", "AAAA"])
    assert code == 2 and "does not match" in text and "events:" not in text


def test_a_signature_with_no_secret_is_exit_two(tmp_path, monkeypatch):
    monkeypatch.delenv(cli.SECRET_ENV, raising=False)
    f = write(tmp_path, [ev("1", "InsufficientFunds")])
    code, text = run(["--events", str(f), "--signature", "x"])
    assert code == 2 and cli.SECRET_ENV in text


def test_the_script_has_no_send_or_write_path():
    src = (ROOT / "scripts" / "unit_returns_replay.py").read_text()
    for banned in ("requests", "urllib", "socket", "smtplib", "anthropic", "openai", ".write_text(", ".write_bytes(", "open("):
        assert banned not in src
