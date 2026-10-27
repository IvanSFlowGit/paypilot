"""M3 rework: the environment guard, the deferred set, the two-tenant behavioural proof, and
the pre-M3 migration. Offline and hermetic. Fake Resend transport; no network."""

from __future__ import annotations

import ast
import hashlib
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from app import loop, mailer
from app.store import Database, Store
from app.tenant_config import (
    DEMO_TENANT,
    TENANT_ENV_FIELDS,
    TENANT_KEY_ENV,
    config_for,
    save_tenant_config,
    set_platform_fields,
)

ROOT = Path(__file__).resolve().parents[1]
TENANT_FILE = "app/tenant_config.py"

#: Names the deferred contract watches on top of the tenant-level set. These are
#: platform-level in the design, but they are read at the webhook boundary, which M4/M5
#: move to a per-tenant secret. Listed here so the boundary is explicit and tested.
DEFERRED_WATCH_NAMES = frozenset({
    "PAYPILOT_ALLOW_UNSIGNED_WEBHOOKS", "ADMIN_TOKEN", "WEBHOOK_SECRET", "DECISION_API_TOKEN",
})

#: (module, env-name) -> how many read sites are DEFERRED to M4/M5. Each entry is a decision,
#: with its reason. Counts, not a set: a second read of an already-deferred name is a new
#: deferred read, and it fails the exact-count test below.
DEFERRED_TO_M4_M5 = {
    ("app/api.py", "PAYPILOT_ALLOW_UNSIGNED_WEBHOOKS"): 1,  # M5: unsigned webhooks decided per tenant in code
    ("app/api.py", "STRIPE_WEBHOOK_SECRET"): 2,             # M5: per-tenant webhook URL and secret
    ("app/api.py", "RECHARGE_CLIENT_SECRET"): 2,            # M5: per-tenant webhook secret
    ("app/api.py", "CHARGEBEE_WEBHOOK_SECRET"): 2,          # M5: per-tenant webhook secret
    ("app/api.py", "ADMIN_TOKEN"): 2,                       # M4: principal tokens replace the static token
    ("app/api.py", "WEBHOOK_SECRET"): 2,                    # M4/M5: legacy shared secret, retired in multi mode
    ("app/decision.py", "DECISION_API_TOKEN"): 1,           # M4: decision-API tokens become per-principal
}

WATCHED = frozenset(TENANT_ENV_FIELDS) | DEFERRED_WATCH_NAMES


def _docstring_ids(tree: ast.AST) -> set[int]:
    ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                ids.add(id(body[0].value))
    return ids


#: Reference to the closed list itself. A loop over it reads every tenant name at once, which
#: is a read the literal scan cannot see, so the reference is a finding in its own right.
_TABLE_NAME = "TENANT_ENV_FIELDS"


def _fold(node: ast.AST) -> str | None:
    """Constant-fold a string built from literals: "PAYPILOT_" + "X", implicit concatenation."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _fold(node.left), _fold(node.right)
        if left is not None and right is not None:
            return left + right
    if isinstance(node, ast.JoinedStr):
        parts: list[str] = []
        for part in node.values:
            if isinstance(part, ast.Constant):
                parts.append(part.value)
            elif isinstance(part, ast.FormattedValue):
                inner = _fold(part.value)  # f"PAYPILOT_{'MAX_TOUCHES'}" folds to the full name
                if inner is None:
                    return None
                parts.append(inner)
            else:
                return None
        return "".join(parts)
    return None


def scan_env_literals(root: Path, names=WATCHED) -> list[tuple[str, int, str]]:
    """Every string that names a watched environment variable, anywhere under ``root/app``.

    Catches a direct ``os.getenv("X")``, a helper that takes the name (``_int_env("X")``), a
    module constant (``NAME = "X"``), a name built by concatenation or an f-string
    (``"PAYPILOT_" + "X"``), and any reference to the closed list TENANT_ENV_FIELDS outside
    the config module. Recursive, so a read in ``app/bank/`` is not missed. Docstrings are
    skipped, so prose that names a variable is not a read.
    """
    found: list[tuple[str, int, str]] = []
    for path in sorted((root / "app").rglob("*.py")):
        rel = path.relative_to(root).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"))
        skip = _docstring_ids(tree)
        covered: set[int] = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.BinOp, ast.JoinedStr)) and id(node) not in covered:
                folded = _fold(node)
                if folded is not None:
                    for sub in ast.walk(node):
                        covered.add(id(sub))
                    if folded in names:
                        found.append((rel, node.lineno, folded))
                    continue
            if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                    and node.value in names and id(node) not in skip and id(node) not in covered):
                found.append((rel, node.lineno, node.value))
            if isinstance(node, ast.Name) and node.id == _TABLE_NAME:
                found.append((rel, node.lineno, _TABLE_NAME))
            if isinstance(node, ast.Attribute) and node.attr == _TABLE_NAME:
                found.append((rel, node.lineno, _TABLE_NAME))
            if isinstance(node, ast.ImportFrom) and any(a.name == _TABLE_NAME for a in node.names):
                found.append((rel, node.lineno, _TABLE_NAME))
    return found


def _outside_config(hits):
    return [h for h in hits if h[0] != TENANT_FILE]


def test_no_tenant_env_name_outside_the_config_module_except_the_deferred_set():
    """Any literal naming a tenant-level variable, at import or call time, direct or through
    a helper or constant, fails unless it is one of the DEFERRED_TO_M4_M5 reads."""
    offenders = [
        f"{rel}:{line} {name}"
        for rel, line, name in _outside_config(scan_env_literals(ROOT))
        if (rel, name) not in DEFERRED_TO_M4_M5
    ]
    assert offenders == [], f"tenant-level env name read outside app/tenant_config.py: {offenders}"


def test_the_deferred_set_is_exact():
    """Any new read of a deferred name fails, including a second read of one already deferred.
    A deferred read that disappears also fails: the set shrinks with the code, never silently."""
    from collections import Counter

    observed = Counter((rel, name) for rel, _line, name in _outside_config(scan_env_literals(ROOT)))
    expected = Counter(DEFERRED_TO_M4_M5)
    added = {k: observed[k] - expected[k] for k in observed if observed[k] > expected[k]}
    gone = {k: expected[k] - observed[k] for k in expected if expected[k] > observed[k]}
    assert not added, f"new deferred read (add it with a reason, or move it to the config): {added}"
    assert not gone, f"deferred read is gone (lower its count in DEFERRED_TO_M4_M5): {gone}"


def test_the_guard_catches_each_planted_form(tmp_path):
    """Controls. The guard must flag a direct read, a helper-wrapped read and a module
    constant, and must ignore a platform read and a docstring that names a variable."""
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "planted.py").write_text(
        '"""PAYPILOT_MAX_TOUCHES"""\n'
        'import os\n'
        'DIRECT = os.getenv("PAYPILOT_MAX_TOUCHES")\n'
        'def _int_env(name, default):\n'
        '    return default\n'
        'HELPED = _int_env("PAYPILOT_HOLDOUT_PCT", 0)\n'
        'SEND_NAME = "PAYPILOT_SEND_EMAIL"\n'
        'VIA_CONSTANT = os.environ.get(SEND_NAME)\n'
        'PLATFORM = os.getenv("PAYPILOT_DB_PATH")\n',
        encoding="utf-8",
    )
    hits = scan_env_literals(tmp_path)
    assert sorted(name for _r, _l, name in hits) == [
        "PAYPILOT_HOLDOUT_PCT", "PAYPILOT_MAX_TOUCHES", "PAYPILOT_SEND_EMAIL",
    ], hits


def test_the_real_app_is_scanned_not_vacuously():
    """The scan must see the platform-level set in use, or it proves nothing."""
    names = {name for _r, _l, name in scan_env_literals(ROOT, WATCHED)}
    assert names, "the scan found no watched name at all"


def test_the_guard_catches_the_blind_spots_too(tmp_path):
    """Planted controls for the forms the first guard missed: a read in a subpackage, a name
    built by concatenation, an f-string, a loop over the closed table, and an import of it."""
    (tmp_path / "app" / "bank").mkdir(parents=True)
    (tmp_path / "app" / "bank" / "sub.py").write_text(
        'import os\nSUB = os.getenv("PAYPILOT_MAX_TOUCHES")\n', encoding="utf-8")
    (tmp_path / "app" / "built.py").write_text(
        'import os\n'
        'A = os.getenv("PAYPILOT_" + "MAX_TOUCHES")\n'
        'B = os.getenv(f"PAYPILOT_{"MAX_TOUCHES"}")\n'
        'C = os.getenv("PAYPILOT_DB_PATH")\n',  # platform: must NOT be flagged
        encoding="utf-8",
    )
    (tmp_path / "app" / "loop_over.py").write_text(
        'from app.tenant_config import TENANT_ENV_FIELDS\n'
        'for name in TENANT_ENV_FIELDS:\n'
        '    pass\n',
        encoding="utf-8",
    )
    hits = scan_env_literals(tmp_path)
    flagged = sorted(name for _r, _l, name in hits)
    assert flagged.count("PAYPILOT_MAX_TOUCHES") == 3, hits
    assert "TENANT_ENV_FIELDS" in flagged, hits
    assert "PAYPILOT_DB_PATH" not in flagged, hits


def test_the_real_app_has_no_reference_to_the_closed_table_outside_config():
    """No module outside app/tenant_config.py may read the closed list: a loop over it would
    read every tenant name without a literal the literal scan could find."""
    refs = [h for h in scan_env_literals(ROOT) if h[2] == _TABLE_NAME]
    assert [h for h in refs if h[0] != TENANT_FILE] == [], refs


# --- two tenants, one database, the real recovery path --------------------------------

def _failed_event(invoice_id: str, customer: str = "cus_shared") -> dict:
    return {
        "id": f"evt_{invoice_id}",
        "type": "invoice.payment_failed",
        "data": {"object": {
            "object": "invoice", "id": invoice_id, "customer": customer,
            "subscription": "sub_shared", "customer_email": "cust@mail.test",
            "amount_due": 4900, "currency": "eur", "attempt_count": 1,
            "payment_intent": {"last_payment_error": {"decline_code": "expired_card"}},
            "metadata": {"paypilot_customer_id": "cust_001"},
        }},
    }


@pytest.fixture
def two_tenants(tmp_path, monkeypatch):
    """Three tenants in one database, each with a different configuration. The sends go to a
    fake transport that records who was mailed, from which address, with which key."""
    import app.ingest as ingest_module

    monkeypatch.setenv(TENANT_KEY_ENV, Fernet.generate_key().decode("ascii"))
    monkeypatch.setenv("PAYPILOT_TENANCY", "multi")
    monkeypatch.setenv("PAYPILOT_PUBLIC_BASE_URL", "https://approve.paypilot.test")
    # The environment carries values that a multi-mode tenant must NEVER inherit.
    monkeypatch.setenv("PAYPILOT_UPDATE_URL", "https://leak.env.test/update")
    monkeypatch.setenv("PAYPILOT_BUSINESS_NAME", "Env Leak Billing")
    monkeypatch.setenv("PAYPILOT_SEND_EMAIL", "1")
    monkeypatch.setenv("PAYPILOT_ALLOWED_RECIPIENTS", "*")
    monkeypatch.setenv("RESEND_API_KEY", "re_env_leak")
    monkeypatch.setattr(ingest_module, "_retriever", None)

    sent: list[dict] = []

    def _fake_post(payload, api_key, **_kw):
        sent.append({"to": payload["to"], "from": payload.get("from"), "text": payload.get("text", ""),
                     "subject": payload.get("subject", ""), "key": api_key})
        return True, f"msg_{len(sent)}", None

    monkeypatch.setattr(mailer, "_post_to_resend", _fake_post)

    path = tmp_path / "multi.db"
    db = Database(path, owner_tenant="tn_alpha")
    tenants = {
        "tn_alpha": dict(
            behaviour={"approval_mode": "off", "approver_emails": ["ops@alpha.test"],
                       "from_address": "Alpha <billing@alpha.test>", "business_name": "Alpha Billing",
                       "update_url": "https://alpha.example.test/update", "holdout_pct": 0,
                       "send_cooldown_hours": 24, "max_touches": 3},
            secrets={"resend_api_key": "re_alpha_secret"},
            switch=dict(send_enabled=True, allowed_recipients=["cust@mail.test"],
                        allowed_from_domains=["alpha.test"]),
        ),
        "tn_beta": dict(
            behaviour={"approval_mode": "email", "approver_emails": ["ops@beta.test"],
                       "from_address": "Beta <billing@beta.test>", "business_name": "Beta Billing",
                       "update_url": "https://beta.example.test/update", "holdout_pct": 0,
                       "send_cooldown_hours": 0, "max_touches": 3},
            secrets={"resend_api_key": "re_beta_secret"},
            switch=dict(send_enabled=True, allowed_recipients=["cust@mail.test"],
                        allowed_from_domains=["beta.test"]),
        ),
        "tn_gamma": dict(
            behaviour={"approval_mode": "off", "from_address": "Gamma <billing@gamma.test>",
                       "business_name": "Gamma Billing", "holdout_pct": 100,
                       "update_url": "https://gamma.example.test/update"},
            secrets={"resend_api_key": "re_gamma_secret"},
            switch=dict(send_enabled=True, allowed_recipients=["cust@mail.test"],
                        allowed_from_domains=["gamma.test"]),
        ),
    }
    for tid, spec in tenants.items():
        save_tenant_config(db, tid, spec["behaviour"], spec["secrets"])
        set_platform_fields(db, tid, **spec["switch"])
    stores = {tid: Store(db, tenant_id=tid) for tid in tenants}
    yield {"db": db, "stores": stores, "sent": sent}
    for store in stores.values():
        store.close()


def test_each_tenant_recovery_uses_its_own_config_and_nothing_leaks(two_tenants):
    stores, sent = two_tenants["stores"], two_tenants["sent"]
    shared = "in_shared_1"

    # alpha: direct send, its own key, sender, link and business name
    alpha = loop.handle_payment_failed(_failed_event(shared), stores["tn_alpha"])
    assert alpha["delivery"]["status"] == mailer.STATUS_SENT, alpha["delivery"]
    assert len(sent) == 1
    assert sent[0]["to"] == ["cust@mail.test"]
    assert sent[0]["from"] == "Alpha <billing@alpha.test>"
    assert sent[0]["key"] == "re_alpha_secret"
    assert "https://alpha.example.test/update" in sent[0]["text"]
    assert "Alpha Billing" in sent[0]["text"]

    # beta: approval mode, so the approver is mailed and the customer is not
    beta = loop.handle_payment_failed(_failed_event(shared), stores["tn_beta"])
    assert beta["delivery"]["status"] != mailer.STATUS_SENT, beta["delivery"]
    beta_mail = [m for m in sent if m["key"] == "re_beta_secret"]
    assert beta_mail, "beta's approval request was never handed to its own key"
    assert all(m["to"] == ["ops@beta.test"] for m in beta_mail)
    assert not any(m["to"] == ["cust@mail.test"] and m["key"] == "re_beta_secret" for m in sent)

    # beta's cooldown is 0, so a second invoice for the same customer is NOT suppressed
    beta_again = loop.handle_payment_failed(_failed_event("in_shared_beta_2"), stores["tn_beta"])
    assert beta_again["delivery"].get("error") != "cooldown_active", beta_again["delivery"]

    # gamma: holdout 100, so the invoice is withheld and nothing is sent
    before = len(sent)
    gamma = loop.handle_payment_failed(_failed_event(shared), stores["tn_gamma"])
    assert gamma["delivery"]["status"] == "holdout", gamma["delivery"]
    assert len(sent) == before

    # alpha's cooldown (24h) suppresses a second invoice for the same customer; beta has none
    second = loop.handle_payment_failed(_failed_event("in_shared_2"), stores["tn_alpha"])
    assert second["delivery"].get("error") == "cooldown_active", second["delivery"]

    # no tenant's data crossed into another: each store holds the invoice it was given
    assert stores["tn_alpha"].get_failure(shared)["state"] == "messaged"
    assert stores["tn_beta"].get_failure(shared)["state"] != "messaged"
    assert stores["tn_gamma"].get_failure(shared)["holdout"]

    # no env value leaked into any tenant's output
    blob = " ".join(m["text"] + m["from"] for m in sent)
    assert "leak.env.test" not in blob and "Env Leak" not in blob
    assert all(m["key"] != "re_env_leak" for m in sent)


def test_the_demo_tenant_never_sends_even_when_its_row_says_so(two_tenants):
    set_platform_fields(two_tenants["db"], DEMO_TENANT, send_enabled=True, allowed_recipients=["*"])
    assert config_for(DEMO_TENANT, two_tenants["db"]).send_enabled is False


def test_two_tenants_have_different_configs_by_value(two_tenants):
    db = two_tenants["db"]
    a, b = config_for("tn_alpha", db), config_for("tn_beta", db)
    assert (a.approval_mode, b.approval_mode) == ("off", "email")
    assert (a.send_cooldown_hours, b.send_cooldown_hours) == (24.0, 0.0)
    assert a.update_url != b.update_url
    assert a.approver_emails != b.approver_emails


# --- migration: a pre-M3 current-schema file gains the empty table and keeps its rows ---------

def _content_digest(conn) -> str:
    digest = hashlib.sha256()
    for table in ("failures", "events", "messages", "transitions", "approvals", "early_paid"):
        for row in conn.execute(f"SELECT * FROM {table} ORDER BY 1, 2").fetchall():
            digest.update(repr(tuple(row)).encode("utf-8"))
    return digest.hexdigest()


def test_a_pre_m3_current_file_gains_the_empty_table_and_keeps_every_row(tmp_path, monkeypatch):
    monkeypatch.setenv("PAYPILOT_TENANCY", "single")
    path = tmp_path / "pre_m3.db"
    db = Database(path, owner_tenant="default")
    store = Store(db, tenant_id="default")
    store.record_failure(invoice_id="in_pre_1", customer_id="cust_001", amount_minor=4900,
                         currency="eur", failure_code="expired_card", attempt_count=1,
                         stripe_customer_id="cus_1", subscription_id="sub_1", holdout=False)
    db.conn.execute("DROP TABLE tenant_config")
    db.conn.commit()
    before = _content_digest(db.conn)
    store.close()
    db.close()

    reopened = Database(path, owner_tenant="default")
    try:
        assert _content_digest(reopened.conn) == before, "an existing row changed on open"
        assert reopened.conn.execute("SELECT COUNT(*) FROM tenant_config").fetchone()[0] == 0
    finally:
        reopened.close()



def test_gmail_and_imap_credentials_come_from_the_tenant_secrets(tmp_path, monkeypatch):
    """A tenant's mailbox credentials are its own secrets, read at the point of use. An
    environment value a tenant does not own must not make it look configured."""
    from app import gmail_drafts, imap_drafts

    monkeypatch.setenv(TENANT_KEY_ENV, Fernet.generate_key().decode("ascii"))
    monkeypatch.setenv("PAYPILOT_TENANCY", "multi")
    monkeypatch.setenv("PAYPILOT_GOOGLE_CLIENT_ID", "env-id")
    monkeypatch.setenv("PAYPILOT_GOOGLE_CLIENT_SECRET", "env-secret")
    monkeypatch.setenv("PAYPILOT_GMAIL_REFRESH_TOKEN", "env-token")
    monkeypatch.setenv("PAYPILOT_IMAP_HOST", "env.imap.test")
    monkeypatch.setenv("PAYPILOT_IMAP_PORT", "993")
    monkeypatch.setenv("PAYPILOT_IMAP_USER", "env@imap.test")
    monkeypatch.setenv("PAYPILOT_IMAP_PASSWORD", "env-password")

    db = Database(tmp_path / "creds.db", owner_tenant="tn_has")
    save_tenant_config(db, "tn_has",
                       {"imap_host": "imap.has.test", "imap_port": "993", "imap_user": "has@has.test"},
                       {"imap_password": "own-password", "gmail_client_id": "own-id",
                        "gmail_client_secret": "own-secret", "gmail_refresh_token": "own-token"})
    save_tenant_config(db, "tn_none", {}, {})
    has = config_for("tn_has", db)
    none = config_for("tn_none", db)

    assert gmail_drafts.configured(has) is True
    assert imap_drafts.configured(has) is True
    assert gmail_drafts.configured(none) is False, "an env Gmail credential leaked into a tenant"
    assert imap_drafts.configured(none) is False, "an env IMAP credential leaked into a tenant"
    assert has.imap_password == "own-password" and none.imap_password == ""
