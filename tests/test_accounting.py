"""Xero, QuickBooks and Sage: read-only guards and invoice mappers. Synthetic bodies, offline, no key."""

from __future__ import annotations

import json
from datetime import date

import pytest

from app import accounting_map as am
from app import accounting_scopes as sc
from app import invoice_source as inv

SALT = "test-salt-not-a-secret"


def L(obj):
    """Round-trip through JSON the way a vendor body arrives, then parse exactly."""
    return am.loads_exact(json.dumps(obj))


# ---------------- scopes: what each vendor lets us ask for ----------------

def test_xero_read_scopes_are_accepted_and_enforced_by_the_vendor():
    assert sc.check_scopes("xero", ["accounting.invoices.read", "offline_access"]) == "vendor"


@pytest.mark.parametrize(
    "scope", ["accounting.invoices", "accounting.transactions", "accounting.transactions.read",
              "accounting.payments", "accounting.settings.read", "bankfeedsv2", "full_access"],
)
def test_xero_write_deprecated_and_unrelated_scopes_are_refused(scope):
    with pytest.raises(sc.ScopeRefused):
        sc.check_scopes("xero", ["accounting.invoices.read", scope])


def test_sage_readonly_ok_full_access_refused():
    assert sc.check_scopes("sage", ["readonly"]) == "vendor"
    with pytest.raises(sc.ScopeRefused):
        sc.check_scopes("sage", ["readonly", "full_access"])
    with pytest.raises(sc.ScopeRefused):
        sc.check_scopes("sage", ["full_access"])


def test_quickbooks_has_no_read_only_scope_so_the_client_is_the_only_guard():
    assert sc.check_scopes("qbo", ["com.intuit.quickbooks.accounting"]) == "client"
    with pytest.raises(sc.ScopeRefused):
        sc.check_scopes("qbo", ["com.intuit.quickbooks.accounting", "com.intuit.quickbooks.payment"])


def test_scope_input_shapes_are_refused():
    for bad in ("accounting.invoices.read", [], None, [""], [1]):
        with pytest.raises(sc.ScopeRefused):
            sc.check_scopes("xero", bad)
    with pytest.raises(sc.ScopeRefused):
        sc.check_scopes("netsuite", ["x"])


def test_the_invoice_read_scope_is_required():
    with pytest.raises(sc.ScopeRefused):
        sc.check_scopes("xero", ["accounting.contacts.read"])


# ---------------- Xero Remote MCP: only reading tools ----------------

@pytest.mark.parametrize("tool", ["list_invoices", "list_payments", "list_bank_transactions"])
def test_xero_mcp_read_tools_pass_with_the_scope(tool):
    granted = ["accounting.invoices.read", "accounting.payments.read", "accounting.banktransactions.read"]
    assert sc.check_xero_mcp_tool(tool, granted) == tool


@pytest.mark.parametrize(
    "tool", ["create_invoice", "update_invoice", "authorise_invoice", "void_invoice", "create_payment",
             "create_bank_transaction", "update_contact", "list_nothing", "", None],
)
def test_xero_mcp_write_and_unknown_tools_are_refused(tool):
    with pytest.raises(sc.ScopeRefused):
        sc.check_xero_mcp_tool(tool, ["accounting.invoices.read"])


def test_xero_mcp_read_tool_without_its_scope_is_refused():
    with pytest.raises(sc.ScopeRefused):
        sc.check_xero_mcp_tool("list_payments", ["accounting.invoices.read"])


# ---------------- QuickBooks client guard ----------------

def test_qbo_only_a_get_select_on_the_query_endpoint_passes():
    sc.ensure_read_request("qbo", "GET", "/v3/company/9130/query", "select * from Invoice")
    sc.ensure_read_request("qbo", "GET", "/v3/company/9130/query", "  SELECT * FROM Invoice WHERE Balance > '0'")


@pytest.mark.parametrize(
    "method,path,query",
    [
        ("POST", "/v3/company/9130/invoice", None),
        ("DELETE", "/v3/company/9130/invoice", None),
        ("PUT", "/v3/company/9130/query", "select * from Invoice"),
        ("GET", "/v3/company/9130/invoice/130/pdf", None),
        ("GET", "/v3/company/9130/invoice", "select 1"),
        ("GET", "/v3/company/9130/query", "delete from Invoice"),
        ("GET", "/v3/company/9130/query", None),
        ("GET", "/v3/company/9130/query", "select * from Invoice operation=void"),
        ("GET", "v3/company/9130/query", "select 1"),
    ],
)
def test_qbo_every_other_request_is_refused(method, path, query):
    with pytest.raises(sc.ScopeRefused):
        sc.ensure_read_request("qbo", method, path, query)


def test_every_vendor_refuses_a_non_get():
    for v in ("xero", "sage"):
        with pytest.raises(sc.ScopeRefused):
            sc.ensure_read_request(v, "POST", "/api.xro/2.0/Invoices")
        sc.ensure_read_request(v, "GET", "/api.xro/2.0/Invoices")


# ---------------- exact numbers ----------------

def test_loads_exact_keeps_every_number_as_the_string_sent():
    assert L({"a": 0.07, "b": 100, "c": 100.0}) == {"a": "0.07", "b": "100", "c": "100.0"}


# ---------------- Xero mapper ----------------

def xero(**kw):
    base = {
        "InvoiceID": "inv-uuid-1", "InvoiceNumber": "INV-1001", "Type": "ACCREC", "Status": "AUTHORISED",
        "Contact": {"ContactID": "c-1"}, "Date": "2026-10-01", "DueDate": "2026-10-31",
        "Total": 2500.00, "AmountDue": 2500.00, "AmountPaid": 0.00, "CurrencyCode": "EUR",
    }
    base.update(kw)
    return L(base)


def test_xero_authorised_receivable_maps_to_open():
    r = am.map_xero_invoice(xero(), salt=SALT)
    assert (r.source, r.invoice_no, r.status, r.amount_minor, r.currency) == ("xero", "INV-1001", "open", 250000, "eur")
    assert r.due == date(2026, 10, 31) and r.issued == date(2026, 10, 1)
    assert "c-1" not in repr(r)  # a hashed ref only


@pytest.mark.parametrize(
    "status,expect", [("PAID", "paid"), ("VOIDED", "cancelled"), ("DELETED", "cancelled")]
)
def test_xero_closed_statuses(status, expect):
    p = xero(Status=status, AmountDue=0.00)
    assert am.map_xero_invoice(p, salt=SALT).status == expect


@pytest.mark.parametrize("status", ["DRAFT", "SUBMITTED"])
def test_xero_unissued_statuses_are_unmapped_not_chased(status):
    r = am.map_xero_invoice(xero(Status=status), salt=SALT)
    assert isinstance(r, am.Unmapped) and "not an issued invoice" in r.reason


def test_xero_a_bill_the_client_owes_is_refused():
    assert isinstance(am.map_xero_invoice(xero(Type="ACCPAY"), salt=SALT), am.Unmapped)


def test_xero_unknown_status_is_unmapped():
    assert isinstance(am.map_xero_invoice(xero(Status="ARCHIVED"), salt=SALT), am.Unmapped)


def test_xero_authorised_with_nothing_due_contradicts_itself():
    r = am.map_xero_invoice(xero(AmountDue=0.00), salt=SALT)
    assert isinstance(r, am.Unmapped) and "AmountDue is zero" in r.reason


def test_xero_microsoft_date_form_is_refused_not_guessed():
    r = am.map_xero_invoice(xero(DueDate="/Date(1439434356790+0000)/"), salt=SALT)
    assert isinstance(r, am.Unmapped) and "/Date()/" in r.reason


def test_xero_midnight_datestring_is_read():
    r = am.map_xero_invoice(xero(DueDate="/Date(1)/", DueDateString="2026-10-31T00:00:00"), salt=SALT)
    assert r.due == date(2026, 10, 31)


def test_xero_missing_contact_or_currency_or_number_is_unmapped():
    for k in ("Contact", "CurrencyCode", "InvoiceNumber", "Total"):
        p = xero()
        del p[k]
        assert isinstance(am.map_xero_invoice(p, salt=SALT), am.Unmapped), k


def test_xero_a_float_amount_is_refused():
    p = json.loads(json.dumps({"InvoiceNumber": "A", "Type": "ACCREC", "Status": "AUTHORISED",
                               "Contact": {"ContactID": "c"}, "Total": 0.07, "AmountDue": 1,
                               "CurrencyCode": "EUR"}))
    assert isinstance(am.map_xero_invoice(p, salt=SALT), am.Unmapped)


# ---------------- QuickBooks mapper ----------------

def qbo(**kw):
    base = {
        "Id": "130", "DocNumber": "1037", "TxnDate": "2026-10-01", "DueDate": "2026-10-31",
        "TotalAmt": 362.07, "Balance": 362.07, "CustomerRef": {"value": "24", "name": "Sonnenschein"},
        "CurrencyRef": {"value": "GBP"},
    }
    base.update(kw)
    return L(base)


def test_qbo_open_invoice_has_no_status_field_and_is_derived_from_balance():
    r = am.map_qbo_invoice(qbo(), salt=SALT, default_currency=None)
    assert (r.status, r.amount_minor, r.currency, r.invoice_no) == ("open", 36207, "gbp", "1037")


def test_qbo_zero_balance_is_paid():
    assert am.map_qbo_invoice(qbo(Balance=0), salt=SALT, default_currency=None).status == "paid"
    assert am.map_qbo_invoice(qbo(Balance=0.0), salt=SALT, default_currency=None).status == "paid"


def test_qbo_partial_payment_is_open():
    r = am.map_qbo_invoice(qbo(Balance=100.00), salt=SALT, default_currency=None)
    assert r.status == "open" and r.amount_minor == 36207


def test_qbo_void_is_recognised_by_zero_total_and_private_note():
    r = am.map_qbo_invoice(qbo(TotalAmt=0, Balance=0, PrivateNote="Voided"), salt=SALT, default_currency=None)
    assert isinstance(r, am.Unmapped) and "voided" in r.reason


def test_qbo_zero_total_without_the_void_note_is_unmapped_not_paid():
    r = am.map_qbo_invoice(qbo(TotalAmt=0, Balance=0), salt=SALT, default_currency=None)
    assert isinstance(r, am.Unmapped) and "not marked Voided" in r.reason


def test_qbo_home_currency_needs_a_supplied_default():
    p = qbo()
    del p["CurrencyRef"]
    assert isinstance(am.map_qbo_invoice(p, salt=SALT, default_currency=None), am.Unmapped)
    assert am.map_qbo_invoice(p, salt=SALT, default_currency="USD").currency == "usd"


def test_qbo_missing_docnumber_cannot_be_matched_to_a_bank_line():
    p = qbo()
    del p["DocNumber"]
    r = am.map_qbo_invoice(p, salt=SALT, default_currency=None)
    assert isinstance(r, am.Unmapped) and "DocNumber" in r.reason


def test_qbo_balance_above_total_is_unmapped():
    assert isinstance(am.map_qbo_invoice(qbo(Balance=999.00), salt=SALT, default_currency=None), am.Unmapped)


def test_qbo_slash_dates_are_read():
    r = am.map_qbo_invoice(qbo(TxnDate="2026/10/01", DueDate="2026/10/31"), salt=SALT, default_currency=None)
    assert r.due == date(2026, 10, 31)


def test_qbo_zero_decimal_currency_balance_compares_numerically():
    p = {"Id": "1", "DocNumber": "9", "DueDate": "2026-10-31", "TotalAmt": "1000", "Balance": "400",
         "CustomerRef": {"value": "24"}, "CurrencyRef": {"value": "JPY"}}
    r = am.map_qbo_invoice(p, salt=SALT, default_currency=None)
    assert (r.status, r.amount_minor) == ("open", 1000)


# ---------------- Sage mapper ----------------

KEYS = {"invoice_no": "reference", "due": "due_date", "amount": "total_amount"}
STATUS = {"UNPAID": "open", "PAID": "paid"}


def sage(**kw):
    base = {
        "id": "sg-1", "reference": "INV-2001", "date": "2026-10-01", "due_date": "2026-10-31",
        "total_amount": 750.00, "outstanding_amount": 750.00, "currency": {"id": "GBP"},
        "status": {"id": "UNPAID", "displayed_as": "Unpaid"}, "contact": {"id": "ct-1"},
    }
    base.update(kw)
    return L(base)


def test_sage_maps_with_supplied_keys_and_status_table():
    r = am.map_sage_invoice(sage(), salt=SALT, keys=KEYS, status_map=STATUS)
    assert (r.source, r.invoice_no, r.status, r.amount_minor, r.currency) == ("sage", "INV-2001", "open", 75000, "gbp")


def test_sage_refuses_without_the_keys_the_page_did_not_state():
    r = am.map_sage_invoice(sage(), salt=SALT, keys={"invoice_no": "reference"}, status_map=STATUS)
    assert isinstance(r, am.Unmapped) and "due" in r.reason and "amount" in r.reason


def test_sage_refuses_without_a_status_table_or_with_an_unknown_id():
    assert isinstance(am.map_sage_invoice(sage(), salt=SALT, keys=KEYS, status_map={}), am.Unmapped)
    r = am.map_sage_invoice(sage(status={"id": "WEIRD"}), salt=SALT, keys=KEYS, status_map=STATUS)
    assert isinstance(r, am.Unmapped)


def test_sage_void_reason_is_cancelled_never_open():
    r = am.map_sage_invoice(sage(void_reason="duplicate"), salt=SALT, keys=KEYS, status_map=STATUS)
    assert isinstance(r, am.Unmapped) and "voided" in r.reason


# ---------------- conservation ----------------

def test_map_many_never_drops_a_row():
    items = [xero(), xero(Type="ACCPAY"), "not an object", xero(Status="DRAFT")]
    ok, bad = am.map_many(items, lambda p: am.map_xero_invoice(p, salt=SALT))
    assert len(ok) == 1 and len(bad) == 3 and len(ok) + len(bad) == len(items)
    with pytest.raises(inv.InvoiceError):
        am.map_many({"not": "a list"}, lambda p: p)


# ---------------- findings from the independent security review ----------------

@pytest.mark.parametrize(
    "vendor,path,query",
    [
        ("qbo", "/v3/company/1/query\n", "select * from Invoice"),
        ("qbo", "/v3/company/1/query", "select * from Invoice&operatio%6E=delete"),
        ("qbo", "/v3/company/1/query", "select * from Invoice\nupdate"),
        ("qbo", "/v3/company/1/query", "select * from Invoice#x"),
        ("xero", "//evil.example/x", None),
        ("sage", "/accounting/v3/../../x", None),
        ("xero", "/a\\b", None),
        ("xero", "/http://evil.example", None),
        ("sage", "/a b", None),
    ],
)
def test_the_read_guard_refuses_smuggled_paths_and_query_strings(vendor, path, query):
    with pytest.raises(sc.ScopeRefused):
        sc.ensure_read_request(vendor, "GET", path, query)
