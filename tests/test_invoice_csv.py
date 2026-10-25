"""The CSV importer: row-by-row, conserved, never echoes a cell."""

from __future__ import annotations

from datetime import date

import pytest

from app import invoice_csv as ic

SALT = "test-salt-not-a-secret"
MAP = {"invoice_no": "Number", "customer": "Client", "amount": "Total", "due": "Due date",
       "status": "State", "currency": "Cur"}
SMAP = {"unpaid": "open", "paid": "paid", "void": "cancelled"}

# SYNTHETIC. Invented names and numbers; not an export from any real system.
GOOD = (
    "Number,Client,Total,Due date,State,Cur\n"
    "A-1,Northwind,125.00,2026-10-01,Unpaid,USD\n"
    "A-2,Contoso,80.50,2026-09-01,Paid,USD\n"
    "A-3,Fabrikam,300,2026-11-15,Void,EUR\n"
)


def run(text, **kw):
    kw.setdefault("column_map", MAP)
    kw.setdefault("salt", SALT)
    kw.setdefault("status_map", SMAP)
    return ic.import_invoices(text, **kw)


def test_a_clean_file_imports_every_row_and_conserves_the_count():
    r = run(GOOD)
    assert (r.total, len(r.records), len(r.rejects)) == (3, 3, 0)
    assert [x.status for x in r.records] == ["open", "paid", "cancelled"]
    assert r.records[0].amount_minor == 12_500 and r.records[0].currency == "usd"
    assert r.records[0].due == date(2026, 10, 1)


def test_no_record_carries_the_customer_name():
    r = run(GOOD)
    blob = repr(r.records)
    for name in ("Northwind", "Contoso", "Fabrikam"):
        assert name not in blob


def test_bom_crlf_and_quoted_commas_are_handled():
    text = "﻿Number,Client,Total,Due date,State,Cur\r\n" \
           'A-1,"Smith, Jones & Co",10.00,2026-10-01,Unpaid,USD\r\n'
    r = run(text)
    assert (r.total, len(r.records)) == (1, 1)


def test_hostile_punctuation_in_a_cell_is_rejected_not_crashed():
    text = 'Number,Client,Total,Due date,State,Cur\nA"1\',Bob,10.00,2026-10-01,Unpaid,USD\n'
    r = run(text)
    assert r.total == 1 and len(r.rejects) == 1
    assert "invoice_no" in r.rejects[0].reason and 'A"1' not in r.rejects[0].reason


def test_a_bad_row_is_rejected_on_its_own_with_its_line_number():
    text = GOOD + "A-4,Initech,12,50,2026-10-01,Unpaid,USD\n" + "A-5,Hooli,7.00,2026-10-02,Unpaid,USD\n"
    r = run(text)
    assert r.total == 5
    assert len(r.records) == 4 and len(r.rejects) == 1
    assert r.rejects[0].line == 5
    r.check()


def test_duplicate_invoice_numbers_reject_the_second():
    r = run(GOOD + "A-1,Other,5.00,2026-10-01,Unpaid,USD\n")
    assert len(r.records) == 3 and len(r.rejects) == 1
    assert "duplicate" in r.rejects[0].reason


def test_an_unmapped_status_is_unknown_never_guessed():
    r = run("Number,Client,Total,Due date,State,Cur\nA-1,Bob,10.00,2026-10-01,Pending review,USD\n")
    assert r.records[0].status == "unknown"


def test_a_missing_due_date_imports_as_no_due_date_not_dropped():
    r = run("Number,Client,Total,Due date,State,Cur\nA-1,Bob,10.00,,Unpaid,USD\n")
    assert r.records[0].due is None


def test_blank_lines_are_not_rows():
    r = run(GOOD + "\n,,,,,\n")
    assert r.total == 3


def test_a_renamed_header_refuses_the_whole_file_not_a_silent_empty_import():
    with pytest.raises(ic.ImportRefused) as exc:
        run(GOOD.replace("Due date", "Date due"))
    assert "due" in str(exc.value)


def test_an_unmapped_required_column_refuses():
    bad = {k: v for k, v in MAP.items() if k != "amount"}
    with pytest.raises(ic.ImportRefused):
        run(GOOD, column_map=bad)


def test_an_unknown_logical_column_refuses():
    with pytest.raises(ic.ImportRefused):
        run(GOOD, column_map={**MAP, "balance": "Total"})


def test_currency_needs_a_column_or_a_default():
    no_cur = {k: v for k, v in MAP.items() if k != "currency"}
    with pytest.raises(ic.ImportRefused):
        run(GOOD, column_map=no_cur)
    r = run("Number,Client,Total,Due date,State\nA-1,Bob,10.00,2026-10-01,Unpaid\n",
            column_map=no_cur, default_currency="gbp")
    assert r.records[0].currency == "gbp"


def test_empty_and_oversize_files_refuse():
    with pytest.raises(ic.ImportRefused):
        run("")
    with pytest.raises(ic.ImportRefused):
        run("x" * (ic.MAX_CHARS + 1))


def test_the_row_cap_refuses(monkeypatch):
    monkeypatch.setattr(ic, "MAX_ROWS", 2)
    with pytest.raises(ic.ImportRefused):
        run(GOOD)


def test_the_conservation_check_can_fail():
    r = run(GOOD)
    r.total += 1
    with pytest.raises(AssertionError):
        r.check()
