"""The five-state CLI: three exit states, conserved counts, no sends. Synthetic data only."""

from __future__ import annotations

import importlib.util
import io
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parents[1] / "scripts" / "invoice_rules_report.py"
_spec = importlib.util.spec_from_file_location("invoice_rules_report", _PATH)
cli = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cli)

CSV = (
    "Number,Client,Total,Due date,State,Cur\n"
    "A-1,Northwind,125.00,2026-10-05,Unpaid,USD\n"   # 35 days
    "A-2,Contoso,80.50,2026-08-01,Unpaid,USD\n"      # 100 days
    "A-3,Fabrikam,300,2026-09-20,Paid,EUR\n"
    "A-4,Initech,10.00,,Unpaid,USD\n"
    "A-5,Hooli,50.00,2026-10-30,Unpaid,USD\n"        # 10 days
    "A-6,Umbrella,70.00,2026-10-05,Unpaid,USD\n"     # 35 days, disputed
)
ARGS = ["--map", "invoice_no=Number", "--map", "customer=Client", "--map", "amount=Total",
        "--map", "due=Due date", "--map", "status=State", "--map", "currency=Cur",
        "--status", "unpaid=open", "--status", "paid=paid", "--today", "2026-11-09"]
FACTS = (
    "invoice_no,chases_sent,replies_received,disputed,promised_by\n"
    "A-2,2,0,,\n"
    "A-6,1,1,yes,\n"
)


@pytest.fixture(autouse=True)
def salt(monkeypatch):
    monkeypatch.setenv(cli.SALT_ENV, "test-salt-not-a-secret")


def run(argv):
    buf = io.StringIO()
    return cli.main(argv, out=buf), buf.getvalue()


def files(tmp_path, facts=None):
    f = tmp_path / "e.csv"
    f.write_text(CSV)
    argv = ["--csv", str(f), *ARGS]
    if facts is not None:
        g = tmp_path / "f.csv"
        g.write_text(facts)
        argv += ["--facts", str(g)]
    return argv


def test_golden_report_with_facts(tmp_path):
    code, text = run(files(tmp_path, FACTS))
    assert code == 0
    assert "rows: 6 read, 6 accepted, 0 rejected" in text
    assert "facts: 2 read, 2 matched, 0 matched no invoice, 0 rejected; 4 invoice(s) had no facts row" in text
    assert "A-2                     no_reply            no_reply" in text
    assert "A-6                     disputed            disputed" in text
    assert "A-1                     overdue_30          overdue_30" in text
    assert "A-5                     UNKNOWN             below_first_chase" in text
    assert "A-4                     UNKNOWN             no_due_date" in text
    assert "A-3                     UNKNOWN             not_open" in text
    assert "dry run: nothing was sent or written." in text


def test_without_facts_no_reply_cannot_fire(tmp_path):
    code, text = run(files(tmp_path))
    assert code == 0
    assert "facts: none given" in text
    assert "A-2                     overdue_60          overdue_60" in text
    assert "  no_reply                 0" in text


def test_every_label_line_prints_even_at_zero(tmp_path):
    _, text = run(files(tmp_path))
    for label in cli.LABELS:
        assert f"  {label}" in text


def test_orphan_and_bad_facts_exit_one_and_are_listed(tmp_path):
    bad = FACTS + "ZZ-9,1,1,,\nA-1,x,,,\nA-5,0,0,maybe,\n"
    code, text = run(files(tmp_path, bad))
    assert code == 1
    assert "facts row matches no invoice: ZZ-9" in text
    assert "disputed: expected" in text


def test_an_empty_replies_cell_is_untracked_not_zero(tmp_path):
    facts = "invoice_no,chases_sent,replies_received\nA-2,2,\n"
    _, text = run(files(tmp_path, facts))
    assert "A-2                     UNKNOWN             replies_untracked" in text


@pytest.mark.parametrize("facts", ["invoice_no,surprise\nA-1,1\n", "chases_sent\n1\n", ""])
def test_a_bad_facts_header_is_exit_two_and_prints_no_report(tmp_path, facts):
    code, text = run(files(tmp_path, facts))
    assert code == 2 and "Nothing was measured." in text and "rows:" not in text


def test_a_missing_salt_is_exit_two(tmp_path, monkeypatch):
    monkeypatch.delenv(cli.SALT_ENV)
    code, text = run(files(tmp_path))
    assert code == 2 and "rows:" not in text


def test_a_renamed_header_is_exit_two_not_an_empty_clean_report(tmp_path):
    f = tmp_path / "e.csv"
    f.write_text(CSV.replace("Number", "Num"))
    code, text = run(["--csv", str(f), *ARGS])
    assert code == 2 and "rows:" not in text


def test_the_script_has_no_send_or_write_path():
    src = _PATH.read_text()
    for banned in ("requests", "urllib", "socket", "smtplib", "anthropic", "openai", ".write_text(", "open("):
        assert banned not in src


DEMO = Path(__file__).resolve().parents[1] / "examples" / "invoice-demo" / "profile.toml"


def test_the_bundled_demo_runs_clean_and_shows_every_label():
    code, text = run(["--profile", str(DEMO), "--today", "2026-11-09"])
    assert code == 0
    assert "rows: 9 read, 9 accepted, 0 rejected" in text
    for label in cli.LABELS:
        assert f"  {label:<20}" in text
        assert f"  {label:<20}     0" not in text or label == "UNKNOWN"
    assert "D-109                   overdue_60          overdue_60" in text


def test_a_profile_is_equivalent_to_the_flags(tmp_path):
    f = tmp_path / "e.csv"
    f.write_text(CSV)
    flags = ["--csv", str(f), *ARGS]
    prof = tmp_path / "p.toml"
    prof.write_text(
        'csv = "e.csv"\n[map]\ninvoice_no="Number"\ncustomer="Client"\namount="Total"\n'
        'due="Due date"\nstatus="State"\ncurrency="Cur"\n[status]\nunpaid="open"\npaid="paid"\n'
    )
    a = run(flags)
    b = run(["--profile", str(prof), "--today", "2026-11-09"])
    assert a[0] == b[0] == 0
    strip = lambda t: "\n".join(x for x in t.splitlines() if not x.startswith("subject:"))  # noqa: E731
    assert strip(a[1]) == strip(b[1])


def test_a_flag_overrides_the_profile(tmp_path):
    f = tmp_path / "e.csv"
    f.write_text(CSV)
    prof = tmp_path / "p.toml"
    prof.write_text('csv = "does-not-exist.csv"\n')
    code, text = run(["--profile", str(prof), "--csv", str(f), *ARGS])
    assert code == 0 and "rows: 6 read" in text


@pytest.mark.parametrize(
    "body",
    ['map = "notatable"\n', 'csv = [1]\n', 'csv = "x.csv"\n[map]\nbroken', ""],
)
def test_a_bad_profile_is_exit_two(tmp_path, body):
    prof = tmp_path / "p.toml"
    prof.write_text(body)
    code, text = run(["--profile", str(prof)])
    assert code == 2 and "rows:" not in text


def test_a_relative_profile_path_is_refused():
    code, text = run(["--profile", "profile.toml"])
    assert code == 2 and "absolute" in text


def test_an_unknown_profile_key_is_refused_even_when_the_rest_would_run(tmp_path):
    f = tmp_path / "e.csv"
    f.write_text(CSV)
    prof = tmp_path / "p.toml"
    prof.write_text('csv = "e.csv"\nsurprise = 1\n')
    code, text = run(["--profile", str(prof), *ARGS])
    assert code == 2 and "unknown key" in text and "rows:" not in text


ROOT = Path(__file__).resolve().parents[1]


def test_the_committed_golden_output_matches_a_fresh_run():
    code, text = run(["--profile", str(DEMO), "--today", "2026-11-09"])
    fresh = "\n".join(x for x in text.splitlines() if not x.startswith("subject:")) + "\n"
    assert code == 0
    assert (ROOT / "examples" / "invoice-demo" / "expected.txt").read_text() == fresh


def test_the_scheduled_workflow_runs_the_cli_read_only_and_asserts_the_golden():
    wf = (ROOT / ".github" / "workflows" / "invoice-rules.yml").read_text()
    assert "schedule:" in wf and "workflow_dispatch:" in wf
    assert "contents: read" in wf and "write" not in wf.split("permissions:")[1].split("jobs:")[0]
    assert "scripts/invoice_rules_report.py" in wf
    assert "diff -u examples/invoice-demo/expected.txt" in wf
    assert "secrets." not in wf
    minute = [ln for ln in wf.splitlines() if "cron:" in ln][0].split('"')[1].split()[0]
    assert minute not in ("0", "00")
