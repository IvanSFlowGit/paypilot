# Copyright (c) 2026 Ivan Skachek (github.com/IvanSFlowGit)
# PolyForm Noncommercial License 1.0.0 - see LICENSE and NOTICE.
# Commercial use requires a separate licence from the author.
"""Read-only by construction for the three accounting sources: Xero, QuickBooks Online, Sage.

Facts below were read first-hand from each vendor's own pages on 2026-11-23 and are
recorded in ``docs/claims.md``. This module makes no network call and holds no credential.

Three vendors, three different guarantees, stated rather than blurred:

* Xero has granular ``.read`` scopes. A token holding only those cannot write, so
  read-only is enforced BY THE VENDOR. The broad scopes (``accounting.transactions``,
  ``accounting.invoices``) are deprecated and write-capable, and are refused here.
  Xero's Remote MCP server exposes read AND write tools behind the same URL; which
  ones work is decided by the scopes granted, so this module also holds an allow-list
  of MCP tool names and refuses every other one.
* Sage has a ``readonly`` scope beside ``full_access``. Enforced by the vendor.
* QuickBooks Online has NO read-only scope: its one accounting scope grants read and
  write. Read-only there is enforced ONLY BY OUR CLIENT, so :func:`ensure_read_request`
  refuses anything but a GET on the ``query`` endpoint carrying a ``select`` statement.
  A token for QuickBooks can write whatever this code does, which is why the DPA and
  the per-client credential come before any live call and why the guard is tested by
  mutation.
"""

from __future__ import annotations

import re

XERO = "xero"
QBO = "qbo"
SAGE = "sage"

#: Scopes a read-only reader may request. ``offline_access`` is the refresh-token scope and
#: moves no data. Anything not listed is refused, including every write-capable scope.
ALLOWED_SCOPES: dict[str, frozenset[str]] = {
    XERO: frozenset(
        {
            "accounting.invoices.read",
            "accounting.payments.read",
            "accounting.banktransactions.read",
            "accounting.contacts.read",
            "offline_access",
        }
    ),
    QBO: frozenset({"com.intuit.quickbooks.accounting"}),
    SAGE: frozenset({"readonly"}),
}

#: Whether the vendor's own scope can stop a write. False means OUR client is the only guard.
READ_ONLY_BY_VENDOR_SCOPE: dict[str, bool] = {XERO: True, QBO: False, SAGE: True}

#: Each vendor's invoice read scope, without which there is nothing to read.
REQUIRED_SCOPE: dict[str, str] = {
    XERO: "accounting.invoices.read",
    QBO: "com.intuit.quickbooks.accounting",
    SAGE: "readonly",
}

#: Xero Remote MCP tools that only read, with the scope each needs. Every other tool,
#: including every ``create_``, ``update_``, ``authorise_`` and ``void_`` one, is refused.
XERO_MCP_READ_TOOLS: dict[str, str | None] = {
    "list_invoices": "accounting.invoices.read",
    "list_bank_transactions": "accounting.banktransactions.read",
    "list_payments": "accounting.payments.read",
    "list_contacts": "accounting.contacts.read",
    "get_connected_tenants": None,
}


class ScopeRefused(ValueError):
    """A scope, tool or request that is not read-only. The message names it, never a credential."""


def check_scopes(vendor: str, scopes: object) -> str:
    """Return how read-only is enforced (``"vendor"`` or ``"client"``) or raise.

    Refuses an unknown vendor, an empty request, a non-string scope, any scope outside the
    allow-list, and a request that lacks the vendor's invoice read scope.
    """
    if vendor not in ALLOWED_SCOPES:
        raise ScopeRefused(f"vendor: {vendor!r} is not one of {sorted(ALLOWED_SCOPES)}")
    if isinstance(scopes, str) or not isinstance(scopes, (list, tuple, set, frozenset)) or not scopes:
        raise ScopeRefused("scopes: expected a non-empty list of scope strings")
    asked = list(scopes)
    for s in asked:
        if not isinstance(s, str) or not s:
            raise ScopeRefused("scopes: every scope must be a non-empty string")
    bad = sorted(s for s in asked if s not in ALLOWED_SCOPES[vendor])
    if bad:
        raise ScopeRefused(f"{vendor}: scope not allowed for a read-only reader: {', '.join(bad)}")
    if REQUIRED_SCOPE[vendor] not in asked:
        raise ScopeRefused(f"{vendor}: the invoice read scope {REQUIRED_SCOPE[vendor]} is missing")
    return "vendor" if READ_ONLY_BY_VENDOR_SCOPE[vendor] else "client"


def check_xero_mcp_tool(name: object, granted: object) -> str:
    """Allow one Xero Remote MCP tool call. ``granted`` is the scope list the token holds."""
    if not isinstance(name, str) or name not in XERO_MCP_READ_TOOLS:
        raise ScopeRefused(f"xero mcp: tool {name!r} is not on the read-only allow-list")
    check_scopes(XERO, granted)
    need = XERO_MCP_READ_TOOLS[name]
    if need is not None and need not in list(granted):  # type: ignore[arg-type]
        raise ScopeRefused(f"xero mcp: {name} needs {need}, which was not granted")
    return name


_QBO_QUERY_PATH = re.compile(r"^/v3/company/[A-Za-z0-9_-]{1,40}/query$")
_QBO_SELECT = re.compile(r"^\s*select\s", re.I)


def ensure_read_request(vendor: str, method: object, path: object, query: object = None) -> None:
    """Refuse any request that is not a read. The single guard QuickBooks has.

    Applies to every vendor: only GET. For QuickBooks the path must be the company ``query``
    endpoint and the statement must begin with ``select``, so even a GET cannot reach the
    ``?operation=delete`` or ``?operation=void`` forms the vendor documents on POST.
    """
    if vendor not in ALLOWED_SCOPES:
        raise ScopeRefused(f"vendor: {vendor!r} is not one of {sorted(ALLOWED_SCOPES)}")
    if method != "GET":
        raise ScopeRefused(f"{vendor}: only GET is permitted, got {method!r}")
    if not isinstance(path, str) or not path.startswith("/"):
        raise ScopeRefused(f"{vendor}: path must be an absolute path")
    if path.startswith("//") or "://" in path or "\\" in path or ".." in path or re.search(r"[\x00-\x20\x7f]", path):
        raise ScopeRefused(f"{vendor}: path must stay on the vendor host and carry no control characters")
    if vendor == QBO:
        if not _QBO_QUERY_PATH.fullmatch(path):
            raise ScopeRefused("qbo: only /v3/company/<realm>/query is permitted")
        if not isinstance(query, str) or not _QBO_SELECT.match(query):
            raise ScopeRefused("qbo: the query statement must start with select")
        if re.search(r"[&%?#\x00-\x1f\x7f]", query):
            raise ScopeRefused("qbo: the statement may not carry &, %, ?, # or control characters")
        if re.search(r"operation\s*=", query, re.I):
            raise ScopeRefused("qbo: an operation parameter is never permitted")
