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


# ---- opt-in ledger formats (property exports) -------------------------------------------

_UK = (
    "Tenancy ref,Tenant ref,Amount due,Due date,Status\n"
    'R-301,T-1,"£1,250.00",01/11/2026,Unpaid\n'
    "R-302,T-2,£950.00,15/10/2026,Unpaid\n"
    '"R-303",T-3,"1,000,000.50",03/09/2026,Paid\n'
)
_UK_MAP = {"invoice_no": "Tenancy ref", "customer": "Tenant ref", "amount": "Amount due",
           "due": "Due date", "status": "Status"}


def _uk(text=_UK, **kw):
    return ic.import_invoices(text, column_map=_UK_MAP, salt="test-salt-123", default_currency="gbp",
                           status_map={"unpaid": "open", "paid": "paid"}, **kw)


def test_uk_style_and_day_first_dates_import_when_the_caller_states_them():
    res = _uk(date_format="%d/%m/%Y", amount_style="uk")
    assert (res.total, len(res.records), len(res.rejects)) == (3, 3, 0)
    by = {r.invoice_no: r for r in res.records}
    assert by["R-301"].amount_minor == 125000 and by["R-301"].due.isoformat() == "2026-11-01"
    assert by["R-302"].amount_minor == 95000 and by["R-302"].due.isoformat() == "2026-10-15"
    assert by["R-303"].amount_minor == 100000050


def test_without_the_options_the_same_file_is_still_refused_row_by_row():
    res = _uk()
    assert len(res.records) == 0 and len(res.rejects) == 3 and res.total == 3


def test_the_same_date_text_means_different_days_under_different_stated_formats():
    day_first = _uk("Tenancy ref,Tenant ref,Amount due,Due date,Status\nX,Y,1.00,02/01/2026,Unpaid\n",
                    date_format="%d/%m/%Y").records
    month_first = _uk("Tenancy ref,Tenant ref,Amount due,Due date,Status\nX,Y,1.00,02/01/2026,Unpaid\n",
                      date_format="%m/%d/%Y").records
    assert day_first[0].due.isoformat() == "2026-01-02"
    assert month_first[0].due.isoformat() == "2026-02-01"


@pytest.mark.parametrize("amount", ["-50.00", "(50.00)", "50.00£", "1,25.00", "1,2500.00", "12,50", "£", "1 250.00"])
def test_uk_style_still_refuses_anything_that_is_not_a_clean_positive_amount(amount):
    text = f"Tenancy ref,Tenant ref,Amount due,Due date,Status\nR-1,T-1,\"{amount}\",01/11/2026,Unpaid\n"
    res = _uk(text, date_format="%d/%m/%Y", amount_style="uk")
    assert len(res.records) == 0 and len(res.rejects) == 1


@pytest.mark.parametrize("due", ["1/2/26", "01/13/2026", "01/11/2026 extra", "tomorrow"])
def test_a_stated_date_format_still_refuses_other_shapes_and_two_digit_years(due):
    text = f"Tenancy ref,Tenant ref,Amount due,Due date,Status\nR-1,T-1,10.00,{due},Unpaid\n"
    res = _uk(text, date_format="%d/%m/%Y", amount_style="uk")
    assert len(res.records) == 0 and len(res.rejects) == 1


def test_an_unlisted_format_or_style_refuses_the_whole_file():
    with pytest.raises(ic.ImportRefused):
        _uk(date_format="%y/%m/%d")
    with pytest.raises(ic.ImportRefused):
        _uk(amount_style="eu")


def test_an_iso_date_is_unambiguous_so_it_is_still_accepted_under_a_stated_format():
    text = "Tenancy ref,Tenant ref,Amount due,Due date,Status\nR-1,T-1,10.00,2026-11-01,Unpaid\n"
    res = _uk(text, date_format="%d/%m/%Y", amount_style="uk")
    assert [r.due.isoformat() for r in res.records] == ["2026-11-01"]
