# Copyright (c) 2026 Ivan Skachek (github.com/IvanSFlowGit)
# PolyForm Noncommercial License 1.0.0 - see LICENSE and NOTICE.
# Commercial use requires a separate licence from the author.
"""A walk through every refusal gate on invented data, with the result ASSERTED.

Run with ``make refusal-walk``. No key, no network, no real invoice. It sends seven synthetic
requests through the real code paths, prints what each one did, then prints the refusal view
the admin route serves, and EXITS NON-ZERO if the view does not show what it should.

Printing a table is not evidence that something ran: a table can be empty because nothing
happened, or because nothing was read. So each step names the row it expects, and the walk
fails when that row is missing or when a row appears that should not (a dry run and a
provider failure are NOT refusals, and the walk checks they are absent).

``collect()`` does the work and returns plain data, so ``scripts/render_refusals_page.py``
can publish the SAME run as a page: the page is what this code produced, not a description
of it. Timestamps and ids are left out of the returned rows, so the output is deterministic.

Everything is written to a temporary directory that is deleted afterwards. The mailer's
provider call is replaced by a stub that raises if a real send is ever attempted.

Exit: 0 every expectation met, 1 an expectation failed, 2 could not run.
"""

from __future__ import annotations

import os
import pathlib
import shutil
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

#: The rows the view must contain, and the two invoices that must NOT appear in it.
EXPECTED = (
    ("SYN-1", "input_validated", "decision_ledger"),
    ("SYN-2", "rule_lookup", "decision_audit"),
    ("SYN-4", "recipient_allowlist", "message_store"),
    ("SYN-5", "output_guard", "message_store"),
    ("", "output_guard", "audit_event_log"),
)
CONTROLS = ("SYN-3", "SYN-6")


def collect() -> dict:
    """Run the walk and return ``{steps, rows, sources, missing, unexpected, unclassified}``.

    Everything this changes (environment, the mailer function, the singletons) is restored in
    ``finally``: the suite runs it in-process, and a leaked variable or a replaced function
    breaks whichever test runs next.
    """
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="paypilot-refusal-walk-"))
    env_before = dict(os.environ)
    mailer_module = None
    post_before = None
    try:
        os.environ["PAYPILOT_DB_PATH"] = str(tmp / "store.db")
        for var in ("RESEND_API_KEY", "PAYPILOT_SEND_EMAIL", "PAYPILOT_ALLOWED_RECIPIENTS",
                    "PAYPILOT_AUDIT_DB_PATH", "PAYPILOT_TENANT"):
            os.environ.pop(var, None)

        from app import audit as audit_module
        from app import mailer, refusals
        from app.decision import handle_decision_request
        from app.decision_audit import SqliteDecisionAudit
        from app.store import Store, reset_store

        store = Store(tmp / "store.db")
        reset_store(store)
        tenant = store.tenant_id
        audit = SqliteDecisionAudit(":memory:")
        event_log = audit_module.AuditEventLog(":memory:")
        audit_module.reset_audit_log(event_log)

        def no_real_send(*a, **k):
            raise AssertionError("the walk attempted a real provider call")

        mailer_module, post_before = mailer, mailer._post_to_resend  # noqa: SLF001
        mailer._post_to_resend = no_real_send  # noqa: SLF001 - replaced per step below

        steps: list[dict] = []

        def step(n, what, outcome, control=False):
            steps.append({"n": n, "what": what, "outcome": outcome, "control": control})

        # 1. a body with a field the endpoint does not accept
        s, b = handle_decision_request(
            {"invoice_id": "SYN-1", "failure_code": "card_expired", "email": "x@example.test"}, audit, tenant)
        step(1, "decision request carrying an unaccepted field",
             f"{s} refused, refusal_id present: {'refusal_id' in b}")

        # 2. a failure code the rules table does not list
        s, b = handle_decision_request({"invoice_id": "SYN-2", "failure_code": "do_not_honor"}, audit, tenant)
        step(2, "decision for a code outside the rules table",
             f"{s}, rule_fired={b['rule_fired']}, rule_lookup={b['gates_checked'][1]['result']}")

        # 3. a matched decision: must NOT appear in the view
        s, b = handle_decision_request({"invoice_id": "SYN-3", "failure_code": "card_expired"}, audit, tenant)
        step(3, "decision for a listed code (control: not a refusal)",
             f"{s}, rule_fired={b['rule_fired']}", control=True)

        # 4. sending is on, recipient is not on the allowlist
        os.environ.update(PAYPILOT_SEND_EMAIL="1", RESEND_API_KEY="re_walk",
                          PAYPILOT_ALLOWED_RECIPIENTS="ok@example.test")
        out = mailer.send_dunning_email(
            invoice_id="SYN-4", to="stranger@example.test", subject="s", body="b", store=store)
        step(4, "send to an address not on the allowlist", f"{out['status']} ({out['error']})")

        # 5. the output guard refuses the message before any provider call
        mailer._post_to_resend = lambda *a, **k: (False, None, mailer.ERROR_FAILED_OUTPUT_GUARD)  # noqa: SLF001
        out = mailer.send_dunning_email(
            invoice_id="SYN-5", to="ok@example.test", subject="s", body="b", store=store)
        step(5, "message the output guard refuses", f"{out['status']} ({out['error']})")

        # 6. a provider failure: a failure, NOT a refusal
        mailer._post_to_resend = lambda *a, **k: (False, None, "http_500")  # noqa: SLF001
        out = mailer.send_dunning_email(
            invoice_id="SYN-6", to="ok@example.test", subject="s", body="b", store=store)
        step(6, "provider fails (control: not a refusal)", f"{out['status']} ({out['error']})", control=True)

        # 7. a drafted message the guard replaced with fixed copy, through the production writer
        audit_module.audit_llm_call(
            node="draft_message", model="synthetic", prompt_template_id="draft_message.v1", prompt="p",
            boundary=None, guards_failed=["foreign_url"], injection_suspected=False,
            fallback_used=True, duration_ms=1.0)
        step(7, "drafted message replaced with fixed copy", "recorded by audit_llm_call")

        view = refusals.unified(store, audit, audit_log=event_log)
        rows = sorted(
            ({"source": e["source"], "gate": e["gate"], "reason": e["reason"], "invoice_id": e["invoice_id"]}
             for e in view["refusals"]),
            key=lambda r: (r["invoice_id"] or "~", r["gate"]),
        )
        got = {(r["invoice_id"], r["gate"], r["source"]) for r in rows}
        return {
            "steps": steps,
            "rows": rows,
            "sources": view["sources"],
            "missing": sorted(set(EXPECTED) - got),
            "unexpected": sorted(g for g in got if g[0] in CONTROLS),
            "unclassified": view["unclassified"],
        }
    finally:
        os.environ.clear()
        os.environ.update(env_before)
        if mailer_module is not None:
            mailer_module._post_to_resend = post_before  # noqa: SLF001
        try:
            from app import audit as _a
            from app.store import reset_store as _r

            _a.reset_audit_log(None)
            _r(None)
        except Exception:  # noqa: BLE001
            pass
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    try:
        result = collect()
    except Exception as exc:  # noqa: BLE001
        print(f"could not run: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    print("PayPilot refusal walk (invented data, nothing leaves this machine)\n")
    for s in result["steps"]:
        print(f"  {s['n']}. {s['what']:<58} -> {s['outcome']}")
    print("\nRefusal view (what GET /refusals serves)\n")
    print(f"  sources: {result['sources']}")
    print(f"  {'source':<16} {'gate':<20} {'reason':<30} invoice")
    for r in result["rows"]:
        print(f"  {r['source']:<16} {r['gate']:<20} {r['reason']:<30} {r['invoice_id'] or '-'}")

    print()
    if result["missing"] or result["unexpected"] or result["unclassified"]:
        print("FAIL")
        for m in result["missing"]:
            print(f"  missing expected row: {m}")
        for u in result["unexpected"]:
            print(f"  row that must not exist: {u}")
        if result["unclassified"]:
            print(f"  unclassified reasons: {result['unclassified']}")
        return 1
    print(f"OK: {len(EXPECTED)} expected rows present, {len(CONTROLS)} controls absent, "
          f"0 unclassified, {len(result['rows'])} rows in the view.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
