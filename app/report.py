# Copyright (c) 2026 Ivan S (github.com/IvanSFlowGit)
# PolyForm Noncommercial License 1.0.0 - see LICENSE and NOTICE.
# Commercial use requires a separate licence from the author.
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
from datetime import UTC, datetime, timedelta

from app.attribution import holdout_pct, holdout_seed
from app.money import exponent, minor_to_major
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

    for arm in arms.values():
        arm["recovery_rate"] = _rate(arm["recovered"], arm["count"])
        arm["median_time_to_recovery_hours"] = (
            round(_median(arm["times"]) / 3600, 2) if arm["times"] else None
        )
        del arm["times"]

    for code, bucket in by_currency.items():
        bucket["failed_value"] = minor_to_major(bucket["failed_minor"], code)
        bucket["recovered_value"] = minor_to_major(bucket["recovered_minor"], code)

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
        # Derived from the cohort actually in this report, not from the live env
        # var. Reading the deployment's setting made the footer print
        # "Holdout set to 0%" directly under a holdout arm showing 33.3%.
        "holdout": {
            "pct": (
                round(100 * arms[ARM_HOLDOUT]["count"] / len(rows)) if rows else 0
            ),
            "configured_pct": holdout_pct(),
            "seed_set": holdout_seed() != "paypilot-holdout-v1",
        },
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

def _money(value: float, currency: str) -> str:
    """Format with the currency's own decimal places.

    A hardcoded ",.2f" printed yen with two decimals it does not have, and
    truncated the third decimal off KWD - reintroducing at the display layer
    the exact bug app/money.py exists to prevent.
    """
    return f"{value:,.{exponent(currency)}f}"


def _pct(value) -> str:
    return "n/a" if value is None else f"{value * 100:.1f}%"


def render_html(report: dict, *, sample: bool = False) -> str:
    """Render the report as a self-contained page.

    No external assets: the app ships a strict same-origin CSP, and a dashboard
    that silently fails to style itself is worse than a plain one.
    """
    totals = report["totals"]
    attr = report["attribution"]

    money_rows = "".join(
        f"<tr><td>{html.escape(code.upper())}</td>"
        f"<td>{b['failed_count']}</td><td>{_money(b['failed_value'], code)}</td>"
        f"<td>{b['recovered_count']}</td><td>{_money(b['recovered_value'], code)}</td></tr>"
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
    banner = (
        "<p class='sample'><b>Sample data.</b> These are illustrative figures on a "
        "fixed cohort, not measured results. A real deployment reports the same "
        "view from its own recovery ledger, behind an admin token.</p>"
        if sample else ""
    )

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<title>PayPilot recovery report</title>
<style>
 /* Brand tokens copied from the landing page so the dashboard is visibly the
    same product. Kept inline because a strict same-origin CSP ships with the
    app and an unstyled dashboard is worse than a plain one. */
 :root {{
   --bg: #0b1120; --panel: #131c31; --panel-2: #1a2540; --line: #25324f;
   --ink: #e7ecf6; --muted: #9fb0cc; --brand: #4f8cff; --brand-2: #38e1b0;
   --warn: #ffb454; --radius: 14px;
   --sans: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
 }}
 * {{ box-sizing: border-box; }}
 html, body {{ margin: 0; padding: 0; }}
 body {{
   font-family: var(--sans);
   background: radial-gradient(1200px 600px at 70% -10%, #16213c 0%, var(--bg) 55%);
   color: var(--ink); line-height: 1.55; -webkit-font-smoothing: antialiased;
 }}
 .wrap {{ max-width: 60rem; margin: 0 auto; padding: 0 1.25rem 4rem; }}
 header.top {{
   display: flex; align-items: center; justify-content: space-between;
   gap: 1rem; padding: 1.25rem 0 2rem; flex-wrap: wrap;
 }}
 .brand {{ display: flex; align-items: center; gap: .6rem; text-decoration: none; color: var(--ink); }}
 .mark {{
   width: 34px; height: 34px; border-radius: 9px; display: grid; place-items: center;
   background: linear-gradient(135deg, var(--brand), var(--brand-2));
   color: #0b1120; font-weight: 700; font-size: 1.05rem;
 }}
 .brand b {{ font-size: 1.12rem; letter-spacing: -.01em; }}
 .back {{
   display: inline-flex; align-items: center; gap: .45rem; text-decoration: none;
   color: var(--ink); background: var(--panel); border: 1px solid var(--line);
   border-radius: 10px; padding: .5rem .9rem; font-size: .92rem;
 }}
 .back:hover {{ background: var(--panel-2); border-color: var(--brand); }}
 h1 {{ font-size: 1.6rem; letter-spacing: -.02em; margin: 0 0 .35rem; }}
 .lede {{ color: var(--muted); margin: 0 0 1.6rem; }}
 h2 {{ font-size: 1.02rem; margin: 2.4rem 0 .8rem; letter-spacing: .02em;
      text-transform: uppercase; color: var(--muted); }}
 .kpis {{ display: grid; gap: .9rem; grid-template-columns: repeat(auto-fit, minmax(9rem, 1fr)); }}
 .kpi {{ background: var(--panel); border: 1px solid var(--line);
         border-radius: var(--radius); padding: 1rem 1.1rem; }}
 .kpi b {{ display: block; font-size: 1.9rem; letter-spacing: -.02em; line-height: 1.1; }}
 .kpi span {{ color: var(--muted); font-size: .88rem; }}
 .card {{ background: var(--panel); border: 1px solid var(--line);
          border-radius: var(--radius); overflow: hidden; }}
 .scroll {{ overflow-x: auto; }}
 table {{ border-collapse: collapse; width: 100%; min-width: 34rem; }}
 th, td {{ text-align: left; padding: .72rem 1.1rem; border-bottom: 1px solid var(--line); }}
 th {{ font-weight: 600; font-size: .82rem; text-transform: uppercase;
       letter-spacing: .04em; color: var(--muted); background: var(--panel-2); }}
 tr:last-child td {{ border-bottom: none; }}
 td {{ font-variant-numeric: tabular-nums; }}
 .note, .sample {{ border-radius: var(--radius); padding: .85rem 1.1rem;
                   font-size: .92rem; margin: 0 0 1.4rem; }}
 .note {{ background: rgba(255,180,84,.10); border: 1px solid rgba(255,180,84,.35); color: #ffd9a3; }}
 .sample {{ background: rgba(79,140,255,.10); border: 1px solid rgba(79,140,255,.35); color: #cfe0ff; }}
 .foot {{ color: var(--muted); font-size: .92rem; margin-top: 1.2rem; }}
 .foot b {{ color: var(--ink); }}
 @media (max-width: 34rem) {{ .kpi b {{ font-size: 1.5rem; }} }}
</style></head><body>
<div class="wrap">
  <header class="top">
    <a class="brand" href="/"><span class="mark">P</span><b>PayPilot</b></a>
    <a class="back" href="/">&#8592; Back to PayPilot</a>
  </header>

  <h1>Recovery report</h1>
  <p class="lede">What failed, what we did about it, and how much of the result
  we can honestly claim.</p>
  {banner}

  <div class="kpis">
    <div class="kpi"><b>{totals['failed']}</b><span>failed</span></div>
    <div class="kpi"><b>{totals['messaged']}</b><span>messaged</span></div>
    <div class="kpi"><b>{totals['clicked']}</b><span>clicked</span></div>
    <div class="kpi"><b>{totals['recovered']}</b><span>recovered</span></div>
    <div class="kpi"><b>{totals['churned']}</b><span>churned</span></div>
  </div>

  <h2>Money, by currency</h2>
  <div class="card scroll"><table>
    <thead><tr><th>Currency</th><th>Failed</th><th>Value at risk</th>
    <th>Recovered</th><th>Value recovered</th></tr></thead>
    <tbody>{money_rows}</tbody>
  </table></div>

  <h2>Attribution</h2>
  {note}
  <div class="card scroll"><table>
    <thead><tr><th>Arm</th><th>Invoices</th><th>Recovered</th>
    <th>Recovery rate</th><th>Median time</th></tr></thead>
    <tbody>{arm_rows}</tbody>
  </table></div>
  <p class="foot">Baseline <b>{html.escape(attr['baseline_kind'])}</b> at
  {_pct(attr['baseline_rate'])}, treated at {_pct(attr['treated_rate'])},
  lift {'withheld' if attr['lift_pp'] is None else f"{attr['lift_pp']:+.2f} pp"}.
  Holdout is {report['holdout']['pct']}% of this cohort
  (configured: {report['holdout']['configured_pct']}%).</p>
</div>
</body></html>"""


# ---------------------------------------------------------------------------
# Public sample
# ---------------------------------------------------------------------------

#: A small, fixed cohort used only for the public sample dashboard. Deliberately
#: unflattering: a holdout that recovers on its own, a partial payment, a churn,
#: and an untouched arm - so the page shows what honest attribution looks like
#: rather than a wall of wins.
_SAMPLE_ROWS = [
    # (invoice, currency, amount_minor, holdout, sent, outcome, recovered_minor,
    #  hours_to_recovery)
    #
    # The hours are plausible, not flattering. A recovery takes as long as the
    # customer takes to notice and act, so a cohort recovering in "0.0h" reads
    # as broken data - which is what this sample showed before these were added.
    ("in_sample_01", "eur", 4900, False, True, "recovered", 4900, 6),
    ("in_sample_02", "eur", 12900, False, True, "recovered", 12900, 31),
    ("in_sample_03", "eur", 4900, False, True, "messaged", None, None),
    ("in_sample_04", "eur", 29900, False, True, "recovered", 15000, 52),
    ("in_sample_05", "eur", 4900, False, True, "churned", None, None),
    ("in_sample_06", "gbp", 8500, False, True, "recovered", 8500, 19),
    ("in_sample_07", "gbp", 8500, False, True, "messaged", None, None),
    ("in_sample_08", "eur", 4900, True, False, "recovered", 4900, 78),
    ("in_sample_09", "eur", 4900, True, False, "failed", None, None),
    ("in_sample_10", "eur", 9900, True, False, "churned", None, None),
    ("in_sample_11", "eur", 4900, False, False, "recovered", 4900, 44),
    ("in_sample_12", "eur", 4900, False, False, "failed", None, None),
]


def sample_report() -> dict:
    """Build the public sample dashboard from a fixed in-memory cohort.

    Runs against a throwaway ``:memory:`` store so the page never touches, and
    can never expose, a real client's recovery ledger. The numbers are sample
    data and the page says so; the point is to show the SHAPE of honest
    attribution - arms reported separately, lift withheld while the arms are
    too small - not to imply measured performance.
    """
    from app.store import Store

    store = Store(":memory:")
    try:
        for (invoice, currency, minor, holdout, sent, outcome, recovered,
             hours) in _SAMPLE_ROWS:
            store.record_failure(
                invoice_id=invoice, customer_id=f"cus_{invoice[-2:]}",
                amount_minor=minor, currency=currency,
                failure_code="card_expired", holdout=holdout,
                stripe_customer_id=f"cus_{invoice[-2:]}",
            )
            if sent:
                store.record_message(invoice_id=invoice, status="sent",
                                     provider_message_id=f"rs_{invoice}")
                store.transition(invoice, STATE_MESSAGED, reason="sample")
            if outcome == "recovered":
                store.transition(invoice, STATE_RECOVERED, reason="sample",
                                 recovered_amount_minor=recovered)
            elif outcome == "churned":
                store.transition(invoice, STATE_CHURNED, reason="sample")
            if hours:
                # Backdate the failure so the elapsed time is realistic. The
                # live path always stamps the real instant.
                failed_at = datetime.now(UTC) - timedelta(hours=hours)
                store.backdate(invoice, failed_at=failed_at.isoformat(timespec="seconds"))
        return build_report(store)
    finally:
        store.close()
