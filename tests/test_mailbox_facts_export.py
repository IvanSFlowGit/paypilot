"""mailbox_facts_export.py: the three exit states, no zero from an incomplete read, no mailbox content printed."""

from __future__ import annotations

import csv
import importlib.util
import io
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


cli = _load("mailbox_facts_export", ROOT / "scripts" / "mailbox_facts_export.py")
fake = _load("fake_imap_for_export", ROOT / "tests" / "test_mailbox_imap.py")

PROFILE = """csv = "invoices.csv"
default_currency = "usd"
[map]
invoice_no = "Number"
customer = "Client"
amount = "Total"
due = "Due date"
status = "State"
[status]
unpaid = "open"
"""
CSV = "Number,Client,Total,Due date,State\nINV-1042,Acme,2500.00,2026-09-01,unpaid\nINV-7,Beta,10.00,2026-10-01,unpaid\n"


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("PAYPILOT_CUSTOMER_SALT", "salt-for-tests-123")
    (tmp_path / "invoices.csv").write_text(CSV)
    (tmp_path / "profile.toml").write_text(PROFILE)
    return tmp_path


def run(env, folders, broken=(), extra=(), mails=None):
    s = fake.Server(
        mails
        if mails is not None
        else {"INBOX": [fake.mail("Re: INV-1042", "We will pay on 2026-11-14.")], "Sent": []},
        broken,
    )
    out = io.StringIO()
    argv = [
        "--profile",
        str(env / "profile.toml"),
        "--ours",
        "billing@us.example",
        "--out",
        str(env / "facts.csv"),
        "--today",
        "2026-11-10",
        *extra,
    ]
    for f in folders:
        argv += ["--folder", f]
    code = cli.main(argv, out=out, connect=s.conn)
    return code, out.getvalue(), s


def rows(env):
    return {r["invoice_no"]: r for r in csv.DictReader(open(env / "facts.csv"))}


def test_a_complete_read_writes_facts_and_exits_zero(env):
    code, text, _ = run(env, ["INBOX", "Sent"])
    r = rows(env)
    assert code == 0 and "COMPLETE" in text
    assert r["INV-1042"]["replies_received"] == "1" and r["INV-1042"]["promised_by"] == "2026-11-14"
    assert r["INV-7"]["replies_received"] == "0"


def test_an_incomplete_read_leaves_replies_empty_and_exits_one(env):
    code, text, _ = run(env, ["INBOX", "Sent"], broken=("Sent",))
    r = rows(env)
    assert (
        code == 1
        and "INCOMPLETE" in text
        and r["INV-7"]["replies_received"] == ""
        and r["INV-1042"]["replies_received"] == ""
    )


def test_nothing_readable_exits_two_and_writes_no_file(env):
    code, text, _ = run(env, ["INBOX"], broken=("INBOX",))
    assert code == 2 and "nothing measured" in text and not (env / "facts.csv").exists()


def test_a_missing_salt_exits_two(env, monkeypatch):
    monkeypatch.delenv("PAYPILOT_CUSTOMER_SALT")
    code, text, _ = run(env, ["INBOX"])
    assert code == 2 and not (env / "facts.csv").exists()


def test_disputed_is_never_written(env):
    run(env, ["INBOX"], mails={"INBOX": [fake.mail("INV-1042", "I dispute this invoice.")]})
    assert "disputed" not in open(env / "facts.csv").read().splitlines()[0]


def test_the_printed_summary_carries_counts_only(env):
    _, text, _ = run(env, ["INBOX"])
    for leak in ("ap@client.example", "billing@us.example", "We will pay", "INV-1042", "Acme"):
        assert leak not in text


def test_only_read_commands_reach_the_server(env):
    _, _, s = run(env, ["INBOX", "Sent"])
    assert {e[0] for e in s.log} <= fake.ALLOWED


def test_a_relative_out_path_is_refused(env):
    s = fake.Server({"INBOX": []})
    out = io.StringIO()
    code = cli.main(
        [
            "--profile",
            str(env / "profile.toml"),
            "--folder",
            "INBOX",
            "--ours",
            "b@x.example",
            "--out",
            "facts.csv",
        ],
        out=out,
        connect=s.conn,
    )
    assert code == 2


def test_the_exported_facts_feed_the_existing_report_and_a_promise_pauses_the_chase(env):
    rep = _load("invoice_rules_report_for_export", ROOT / "scripts" / "invoice_rules_report.py")
    run(env, ["INBOX", "Sent"])
    out = io.StringIO()
    code = rep.main(
        [
            "--profile",
            str(env / "profile.toml"),
            "--facts",
            str(env / "facts.csv"),
            "--today",
            "2026-11-10",
        ],
        out=out,
    )
    text = out.getvalue()
    assert code == 0 and "facts: 2 read, 2 matched" in text
    assert (
        "INV-1042" in text
        and "promise_pending" in text
        and "INV-7" in text
        and "overdue_30" in text
    )


def test_proposals_go_to_a_separate_review_file_and_never_into_the_facts(env):
    s = fake.Server({"INBOX": [fake.mail("Re: INV-1042", "This amount is wrong, we dispute it.")]})
    out = io.StringIO()
    asked = []

    def model(prompt):
        asked.append(prompt)
        return '{"label": "disputed", "evidence_index": 0}'

    code = cli.main(
        [
            "--profile",
            str(env / "profile.toml"),
            "--ours",
            "billing@us.example",
            "--out",
            str(env / "facts.csv"),
            "--today",
            "2026-11-10",
            "--folder",
            "INBOX",
            "--propose-disputes",
        ],
        out=out,
        connect=s.conn,
        complete=model,
    )
    props = list(csv.DictReader(open(env / "facts.csv.proposals.csv")))
    assert code == 0 and props == [
        {
            "invoice_no": "INV-1042",
            "proposed_label": "disputed",
            "evidence_message_id": "<a@x>",
            "state": "ok",
        }
    ]
    assert "disputed" not in open(env / "facts.csv").read()
    assert "ap@client.example" not in asked[0] and "a person sets disputed" in out.getvalue()


def test_with_no_model_switched_on_proposals_are_skipped_and_nothing_is_written(env, monkeypatch):
    monkeypatch.delenv("PAYPILOT_LLM_DRAFT", raising=False)
    s = fake.Server({"INBOX": [fake.mail("Re: INV-1042", "we dispute it")]})
    out = io.StringIO()
    cli.main(
        [
            "--profile",
            str(env / "profile.toml"),
            "--ours",
            "billing@us.example",
            "--out",
            str(env / "facts.csv"),
            "--today",
            "2026-11-10",
            "--folder",
            "INBOX",
            "--propose-disputes",
        ],
        out=out,
        connect=s.conn,
    )
    assert "proposals skipped" in out.getvalue() and not (env / "facts.csv.proposals.csv").exists()
