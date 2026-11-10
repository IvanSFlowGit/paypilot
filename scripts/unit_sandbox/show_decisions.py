#!/usr/bin/env python3
"""Print the saved return decisions in plain language, newest first. Read only.

    python3 scripts/unit_sandbox/show_decisions.py [path-to-decisions.db]

Default file: ~/.paypilot-unit-decisions.db (written by run.sh). Nothing is sent or changed.
"""
import json
import os
import sqlite3
import sys

path = sys.argv[1] if len(sys.argv) > 1 else os.path.expanduser("~/.paypilot-unit-decisions.db")
if not os.path.exists(path):
    sys.exit(f"No decisions file at {path}. Start PayPilot with run.sh first.")
db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
rows = db.execute("select decided_at, invoice_id, decision from decision_audit order by decided_at desc").fetchall()
if not rows:
    sys.exit("No decisions recorded yet.")
for when, payment, raw in rows:
    d = json.loads(raw)
    inp = d.get("input", {})
    print(f"{when[:19]}  payment {payment}")
    print(f"  Return reason: {inp.get('reason') or 'not in the event'}  (code {inp.get('code') or 'unknown'})")
    if d.get("person_required"):
        print("  Decision: HELD. A person decides. Nothing is sent.")
    else:
        why = d.get("why", "").split("Source:")[0].strip()
        why = why.split(". ", 1)[1] if why.startswith("Row ") else why
        why = why.replace("Goes again:", "Can be tried again:").replace("What has to change first:", "What must change first:")
        print(f"  Decision: {why}")
        if d.get("customer_message"):
            print(f"  Message for the customer (a person sends it): {d['customer_message']}")
    print(f"  Acted on automatically: {'yes' if d.get('acted') else 'no'}\n")
