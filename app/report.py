"""The recovery dashboard: what happened, and how much of it we can claim.

Everything here is computed from the ledger in :mod:`app.store`, so the numbers
survive a restart and can be re-derived from the stored events rather than
trusted from a counter.

The design rule throughout is **do not flatter the product**:

* Invoices split into three arms. ``treated`` got at least one message that
  actually sent. ``holdout`` was deliberately withheld. ``untouched`` was
  neither - a dry run, a suppressed recipient, or a failure still in flight.
  Lumping ``untouched`` in with ``treated`` would credit us for recoveries on
  invoices we never contacted, which is the single easiest way to fake this
  number.
* Money never crosses currencies. Totals are per currency, always with the
  code attached.
* A lift figure is withheld until both arms have enough invoices to mean
  anything, and says so rather than printing a confident percentage from four
  data points.
"""

from __future__ import annotations

import html
from datetime import datetime

from app.attribution import holdout_pct, holdout_seed
from app.store import (
    STATE_CHURNED,
    STATE_CLICKED,
    STATE_MESSAGED,
    STATE_RECOVERED,
    get_store,
)

#: Below this many invoices in an arm, a comparison between arms is noise.
#: Reported explicitly rather than silently suppressed.
MIN_ARM_SIZE = 30

ARM_TREATED = "treated"
ARM_HOLDOUT = "holdout"
ARM_UNTOUCHED = "untouched"


def _arm(row: dict, sent_count: int) -> str:
    if row.get("holdout"):
        return ARM_HOLDOUT
    return ARM_TREATED if sent_count > 0 else ARM_UNTOUCHED


def _seconds_between(start: str, end: str) -> float | None:
    try:
        return (datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds()
    except (TypeError, ValueError):
        return None


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2


def _rate(recovered: int, total: int) -> float | None:
    """Recovery rate, or None when there is nothing to divide by.

    None rather than 0.0 on an empty arm: "no data" and "nothing recovered" are
    different statements, and rendering the first as the second is a lie a
    dashboard tells easily.
    """
    if not total:
        return None
    return round(recovered / total, 4)


def build_report(store=None) -> dict:
    """Compute the full recovery report from the ledger."""
    store = store or get_store()
    rows = store.list_failures()

    arms: dict[str, dict] = {
        name: {"count": 0, "recovered": 0, "churned": 0, "times": []}
        for name in (ARM_TREATED, ARM_HOLDOUT, ARM_UNTOUCHED)
    }
    by_currency: dict[str, dict] = {}
    states = {STATE_MESSAGED: 0, STATE_CLICKED: 0, STATE_RECOVERED: 0, STATE_CHURNED: 0}

    for row in rows:
        sent_count = store.sent_message_count(row["invoice_id"])
        arm = arms[_arm(row, sent_count)]
        arm["count"] += 1

        state = row["state"]
        if state in states:
            states[state] += 1

        currency = row["currency"]
        bucket = by_currency.setdefault(
            currency,
            {"failed_count": 0, "failed_minor": 0, "recovered_count": 0, "recovered_minor": 0},
        )
        bucket["failed_count"] += 1
        bucket["failed_minor"] += int(row["amount_minor"] or 0)

        if state == STATE_RECOVERED:
            arm["recovered"] += 1
            # Fall back to the invoice amount only when the closing event
            # carried no figure; a partial payment must never be rounded up to
            # the full invoice.
            recovered_minor = row["recovered_amount_minor"]
            if recovered_minor is None:
                recovered_minor = int(row["amount_minor"] or 0)
            bucket["recovered_count"] += 1
            bucket["recovered_minor"] += int(recovered_minor)
            elapsed = _seconds_between(row["failed_at"], row["recovered_at"])
            if elapsed is not None:
                arm["times"].append(elapsed)
        elif state == STATE_CHURNED:
            arm["churned"] += 1

    for name, arm in arms.items():
        arm["recovery_rate"] = _rate(arm["recovered"], arm["count"])
        arm["median_time_to_recovery_hours"] = (
            round(_median(arm["times"]) / 3600, 2) if arm["times"] else None
        )
        del arm["times"]

    for bucket in by_currency.values():
        bucket["failed_value"] = round(bucket["failed_minor"] / 100, 2)
        bucket["recovered_value"] = round(bucket["recovered_minor"] / 100, 2)

    return {
        "totals": {
            "failed": len(rows),
            # Cumulative, not current-state. An invoice that was messaged and
            # then recovered is no longer sitting in "messaged", so a live-state
            # count would report FEWER messages the better the tool performed.
            "messaged": store.count_ever_reached(STATE_MESSAGED),
            "clicked": store.count_ever_reached(STATE_CLICKED),
            "recovered": states[STATE_RECOVERED],
            "churned": states[STATE_CHURNED],
        },
        "by_currency": by_currency,
        "arms": arms,
        "attribution": _attribution(arms),
        "holdout": {"pct": holdout_pct(), "seed_set": holdout_seed() != "paypilot-holdout-v1"},
    }


def _attribution(arms: dict) -> dict:
    """Compare the treated arm against whichever baseline is available.

    Prefers the holdout, because a randomised control is the only comparison
    that supports a causal claim. Falls back to the untouched arm, which is a
    weaker observational baseline and is labelled as such rather than being
    passed off as an experiment.
    """
    treated = arms[ARM_TREATED]
    holdout = arms[ARM_HOLDOUT]
    untouched = arms[ARM_UNTOUCHED]

    if holdout["count"]:
        baseline, kind = holdout, "randomised_holdout"
    elif untouched["count"]:
        baseline, kind = untouched, "observational_untouched"
    else:
        return {
            "baseline_kind": "none",
            "baseline_rate": None,
            "treated_rate": treated["recovery_rate"],
            "lift_pp": None,
            "note": "No baseline available. Every recorded failure was messaged, "
                    "so no part of the recovery rate can be attributed yet.",
        }

    under_powered = (
        treated["count"] < MIN_ARM_SIZE or baseline["count"] < MIN_ARM_SIZE
    )
    lift = None
    if (
        not under_powered
        and treated["recovery_rate"] is not None
        and baseline["recovery_rate"] is not None
    ):
        lift = round((treated["recovery_rate"] - baseline["recovery_rate"]) * 100, 2)

    note = ""
    if under_powered:
        note = (
            f"Lift withheld: an arm has fewer than {MIN_ARM_SIZE} invoices "
            f"(treated {treated['count']}, baseline {baseline['count']}). "
            "The rates below are real but the difference between them is noise."
        )
    elif kind == "observational_untouched":
        note = (
            "Baseline is observational, not randomised: these invoices went "
            "unmessaged incidentally rather than by assignment, so the "
            "difference is suggestive, not causal. Enable PAYPILOT_HOLDOUT_PCT "
            "for a real control."
        )

    return {
        "baseline_kind": kind,
        "baseline_rate": baseline["recovery_rate"],
        "baseline_n": baseline["count"],
        "treated_rate": treated["recovery_rate"],
        "treated_n": treated["count"],
        "lift_pp": lift,
        "note": note,
    }


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def _pct(value) -> str:
    return "n/a" if value is None else f"{value * 100:.1f}%"


def render_html(report: dict) -> str:
    """Render the report as a self-contained page.

    No external assets: the app ships a strict same-origin CSP, and a dashboard
    that silently fails to style itself is worse than a plain one.
    """
    totals = report["totals"]
    attr = report["attribution"]

    money_rows = "".join(
        f"<tr><td>{html.escape(code.upper())}</td>"
        f"<td>{b['failed_count']}</td><td>{b['failed_value']:,.2f}</td>"
        f"<td>{b['recovered_count']}</td><td>{b['recovered_value']:,.2f}</td></tr>"
        for code, b in sorted(report["by_currency"].items())
    ) or "<tr><td colspan='5'>No failures recorded yet.</td></tr>"

    def _arm_row(name: str, arm: dict) -> str:
        hours = arm["median_time_to_recovery_hours"]
        median = "n/a" if hours is None else f"{hours}h"
        return (
            f"<tr><td>{html.escape(name)}</td><td>{arm['count']}</td>"
            f"<td>{arm['recovered']}</td><td>{_pct(arm['recovery_rate'])}</td>"
            f"<td>{median}</td></tr>"
        )

    arm_rows = "".join(_arm_row(name, a) for name, a in report["arms"].items())

    note = (
        f"<p class='note'>{html.escape(attr['note'])}</p>" if attr.get("note") else ""
    )

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>PayPilot recovery report</title>
<style>
 body {{ font: 15px/1.5 system-ui, sans-serif; margin: 2rem auto; max-width: 52rem;
        padding: 0 1rem; color: #111; background: #fff; }}
 h1 {{ font-size: 1.4rem; }} h2 {{ font-size: 1.05rem; margin-top: 2rem; }}
 table {{ border-collapse: collapse; width: 100%; margin: .5rem 0 1rem; }}
 th, td {{ text-align: left; padding: .4rem .6rem; border-bottom: 1px solid #e5e5e5; }}
 th {{ font-weight: 600; background: #fafafa; }}
 .kpis {{ display: flex; flex-wrap: wrap; gap: 1rem; }}
 .kpi {{ border: 1px solid #e5e5e5; border-radius: 8px; padding: .7rem 1rem; min-width: 8rem; }}
 .kpi b {{ display: block; font-size: 1.5rem; }}
 .note {{ background: #fff8e1; border-left: 3px solid #e0a800; padding: .6rem .8rem;
          font-size: .9rem; }}
 @media (prefers-color-scheme: dark) {{
   body {{ background: #111; color: #eee; }} th {{ background: #1b1b1b; }}
   th, td {{ border-bottom-color: #2a2a2a; }} .kpi {{ border-color: #2a2a2a; }}
   .note {{ background: #2a2411; }}
 }}
</style></head><body>
<h1>PayPilot recovery report</h1>
<div class="kpis">
  <div class="kpi"><b>{totals['failed']}</b>failed</div>
  <div class="kpi"><b>{totals['messaged']}</b>messaged</div>
  <div class="kpi"><b>{totals['clicked']}</b>clicked</div>
  <div class="kpi"><b>{totals['recovered']}</b>recovered</div>
  <div class="kpi"><b>{totals['churned']}</b>churned</div>
</div>

<h2>Money, by currency</h2>
<table><thead><tr><th>Currency</th><th>Failed</th><th>Value at risk</th>
<th>Recovered</th><th>Value recovered</th></tr></thead>
<tbody>{money_rows}</tbody></table>

<h2>Attribution</h2>
{note}
<table><thead><tr><th>Arm</th><th>Invoices</th><th>Recovered</th>
<th>Recovery rate</th><th>Median time</th></tr></thead>
<tbody>{arm_rows}</tbody></table>
<p>Baseline: <b>{html.escape(attr['baseline_kind'])}</b> at {_pct(attr['baseline_rate'])};
treated at {_pct(attr['treated_rate'])};
lift {'withheld' if attr['lift_pp'] is None else f"{attr['lift_pp']:+.2f} pp"}.
Holdout is set to {report['holdout']['pct']}%.</p>
</body></html>"""
