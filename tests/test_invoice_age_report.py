"""The ageing CLI: three exit states, conserved counts, a golden report, no sends."""

from __future__ import annotations

import importlib.util
import io
import json
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parents[1] / "scripts" / "invoice_age_report.py"
_spec = importlib.util.spec_from_file_location("invoice_age_report", _PATH)
cli = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cli)

# SYNTHETIC data only.
CSV = (
    "Number,Client,Total,Due date,State,Cur\n"
    "A-1,Northwind,125.00,2026-10-24,Unpaid,USD\n"
    "A-2,Contoso,80.50,2026-08-01,Unpaid,USD\n"
    "A-3,Fabrikam,300,2026-09-20,Paid,EUR\n"
    "A-4,Initech,10.00,,Unpaid,USD\n"
    "A-5,Hooli,50.00,2026-09-30,Unpaid,USD\n"
)
CSV_ARGS = ["--map", "invoice_no=Number", "--map", "customer=Client", "--map", "amount=Total",
            "--map", "due=Due date", "--map", "status=State", "--map", "currency=Cur",
            "--status", "unpaid=open", "--status", "paid=paid", "--today", "2026-10-25"]


@pytest.fixture(autouse=True)
def salt(monkeypatch):
    monkeypatch.setenv(cli.SALT_ENV, "test-salt-not-a-secret")


def run(argv):
    buf = io.StringIO()
    code = cli.main(argv, out=buf)
    return code, buf.getvalue()


def test_golden_report_for_a_clean_csv(tmp_path):
    f = tmp_path / "e.csv"
    f.write_text(CSV)
    code, text = run(["--csv", str(f), *CSV_ARGS])
    assert code == 0
    assert "rows: 5 read, 5 accepted, 0 rejected, 0 unmapped" in text
    assert "  overdue_1_7          1   125.00 USD" in text
    assert "  overdue_8_30         1   50.00 USD" in text
    assert "  overdue_61_plus      1   80.50 USD" in text
    assert "  closed               1   300.00 EUR" in text
    assert "  no_due_date          1   10.00 USD" in text
    assert text.rstrip().endswith("this is an ageing count, not a chasing plan.")
    assert "dry run: nothing was sent or written." in text
    assert str(f.resolve()) in text


def test_every_bucket_prints_even_at_zero(tmp_path):
    f = tmp_path / "e.csv"
    f.write_text(CSV)
    _, text = run(["--csv", str(f), *CSV_ARGS])
    for b in cli.BUCKETS:
        assert b in text


def test_rejected_rows_exit_one_and_are_listed_without_cell_values(tmp_path):
    f = tmp_path / "e.csv"
    f.write_text(CSV + "A-6,Bob,12,50,2026-10-01,Unpaid,USD\n")
    code, text = run(["--csv", str(f), *CSV_ARGS])
    assert code == 1 and "1 rejected" in text and "reject line 7" in text
    assert "12,50" not in text and "Bob" not in text


def test_no_names_appear_anywhere_in_the_report(tmp_path):
    f = tmp_path / "e.csv"
    f.write_text(CSV)
    _, text = run(["--csv", str(f), *CSV_ARGS])
    for name in ("Northwind", "Contoso", "Fabrikam", "Initech", "Hooli"):
        assert name not in text


@pytest.mark.parametrize("path", ["relative.csv", "/nonexistent/x.csv"])
def test_an_unreadable_subject_is_exit_two_and_prints_no_report(path):
    code, text = run(["--csv", path, *CSV_ARGS])
    assert code == 2 and "NOT RUN" in text and "rows:" not in text


def test_a_missing_salt_is_exit_two_not_a_default(tmp_path, monkeypatch):
    monkeypatch.delenv(cli.SALT_ENV)
    f = tmp_path / "e.csv"
    f.write_text(CSV)
    code, text = run(["--csv", str(f), *CSV_ARGS])
    assert code == 2 and "salt" in text


def test_a_renamed_header_is_exit_two_not_an_empty_clean_report(tmp_path):
    f = tmp_path / "e.csv"
    f.write_text(CSV.replace("Due date", "Date due"))
    code, text = run(["--csv", str(f), *CSV_ARGS])
    assert code == 2 and "rows:" not in text


MERCURY = [
    {"id": "i1", "invoiceNumber": "M-1", "customerId": "c1", "status": "Unpaid",
     "totalAmount": "100.00", "dueDate": "2026-10-20", "currency": "USD"},
    {"id": "i2", "invoiceNumber": "M-2", "customerId": "c2", "status": "Paid",
     "totalAmount": "40.00", "dueDate": "2026-10-01", "currency": "USD"},
    {"id": "i3", "invoiceNumber": "M-3", "customerId": "c3", "status": "Draft",
     "totalAmount": "9.00", "dueDate": "2026-10-01", "currency": "USD"},
]
MKEYS = ["--key", "amount=totalAmount", "--key", "due=dueDate", "--key", "currency=currency",
         "--today", "2026-10-25"]


def test_mercury_json_counts_unmapped_and_exits_one(tmp_path):
    f = tmp_path / "m.json"
    f.write_text(json.dumps(MERCURY))
    code, text = run(["--mercury-json", str(f), *MKEYS])
    assert code == 1
    assert "rows: 3 read, 2 accepted, 0 rejected, 1 unmapped" in text
    assert "  overdue_1_7          1   100.00 USD" in text


def test_mercury_json_without_the_unstated_keys_maps_nothing(tmp_path):
    f = tmp_path / "m.json"
    f.write_text(json.dumps(MERCURY))
    code, text = run(["--mercury-json", str(f), "--today", "2026-10-25"])
    assert code == 1 and "0 accepted" in text and "3 unmapped" in text


def test_the_script_has_no_send_or_write_path():
    src = _PATH.read_text()
    for banned in ("smtplib", "requests", "urllib", "open(", "write_text", "sendmail", "post("):
        assert banned not in src
