"""The offline provider, and the one test that needs a real module on disk.

Most of this is ordinary behaviour. The load-bearing ones are:

  THE OPT-IN REFUSAL, because a fake reachable by a config typo is worse than
  no fake, and the refusal is the only thing standing between synthetic rows
  and a report.

  THE WEBHOOK REFUSING BRANCH, in all three states: a good signature, a wrong
  one, and no header at all. A fake that returned True would make every
  caller's rejection path untestable.

  THE WINDOW, asserted against an injected clock rather than against today,
  so the suite cannot start passing vacuously next month.

  AND THE SUBPROCESS IMPORT TESTS, which copy the tree, edit a real adapter
  module, and import it in a fresh interpreter. The in-process tests in
  test_bank_provider.py prove the deny-list logic; these prove that the
  refusal actually breaks the import of a module somebody wrote, which is a
  different claim and the one that matters.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app import rails
from app.bank import fake
from app.bank import provider as bp

FIXED = datetime(2026, 3, 4, 12, 0, tzinfo=timezone.utc)


def _bank(**kw):
    kw.setdefault("enabled", True)
    kw.setdefault("clock", lambda: FIXED)
    return fake.FakeBank(**kw)


# --- the opt-in gate --------------------------------------------------------


def test_refuses_to_construct_with_no_opt_in(monkeypatch):
    monkeypatch.delenv(fake.ENABLE_ENV, raising=False)
    with pytest.raises(bp.ProviderError, match="FakeBank is off"):
        fake.FakeBank()


def test_env_var_enables_it(monkeypatch):
    monkeypatch.setenv(fake.ENABLE_ENV, "1")
    assert fake.FakeBank() is not None


@pytest.mark.parametrize("value", ["", "0"])
def test_empty_and_zero_do_not_enable_it(monkeypatch, value):
    """A var set to 0 reads as off, or 'unset it' becomes 'set it to 0'."""
    monkeypatch.setenv(fake.ENABLE_ENV, value)
    with pytest.raises(bp.ProviderError):
        fake.FakeBank()


def test_explicit_flag_enables_it_with_no_env(monkeypatch):
    monkeypatch.delenv(fake.ENABLE_ENV, raising=False)
    assert fake.FakeBank(enabled=True) is not None


# --- it satisfies the protocol it claims -----------------------------------


def test_registered_under_its_own_name():
    assert bp.get("fake") is fake.FakeBank


def test_satisfies_the_protocol():
    assert isinstance(_bank(), bp.BankProvider)


def test_declares_every_required_method():
    missing = [m for m in bp.REQUIRED_METHODS if not callable(getattr(fake.FakeBank, m, None))]
    assert missing == []


def test_endpoints_never_resolve():
    """RFC 2606 reserves .invalid, so a stray request fails at DNS."""
    for key, url in fake.FakeBank.ENDPOINTS.items():
        assert ".invalid" in url, f"{key} could resolve: {url}"


# --- consent, and the two clocks -------------------------------------------


def test_authorize_url_carries_state_and_scopes():
    url = _bank().authorize_url(
        state="s-123", redirect_uri="https://app.invalid/cb", scopes=("accounts",)
    )
    assert "state=s-123" in url
    assert "scope=accounts" in url
    assert url.startswith(fake.FakeBank.ENDPOINTS["authorize"])


def test_authorize_url_refuses_empty_state():
    with pytest.raises(bp.ProviderError, match="CSRF"):
        _bank().authorize_url(state="", redirect_uri="https://app.invalid/cb", scopes=())


def test_the_two_expiries_are_far_apart():
    """Minutes against days. An equal pair passes under either reading."""
    ts = _bank().exchange_code(code="c1", redirect_uri="https://app.invalid/cb")
    assert ts.access_expires_at == FIXED + timedelta(minutes=10)
    assert ts.consent_expires_at == FIXED + timedelta(days=90)
    assert ts.consent_expires_at - ts.access_expires_at > timedelta(days=89)


def test_refresh_moves_the_access_expiry_and_not_the_consent():
    bank = _bank()
    ts = bank.exchange_code(code="c1", redirect_uri="https://app.invalid/cb")
    r = bank.refresh(refresh_token=ts.refresh_token)
    assert r.access_expires_at == FIXED + timedelta(minutes=10)
    assert r.consent_expires_at is None, "a refresh must not extend a PSD2 consent"


def test_refresh_refuses_a_foreign_token():
    with pytest.raises(bp.ProviderError, match="not issued"):
        _bank().refresh(refresh_token="someone-elses-token")


def test_clock_must_be_timezone_aware():
    bank = fake.FakeBank(enabled=True, clock=lambda: datetime(2026, 3, 4, 12, 0))
    with pytest.raises(bp.ProviderError, match="naive"):
        bank.exchange_code(code="c1", redirect_uri="https://app.invalid/cb")


# --- reads ------------------------------------------------------------------


def test_reads_refuse_a_foreign_access_token():
    with pytest.raises(bp.ProviderError, match="not issued"):
        _bank().accounts(access_token="bearer-from-somewhere-else")


def test_every_value_carries_the_marker():
    """A figure that escapes into a report has to announce itself."""
    bank = _bank()
    ts = bank.exchange_code(code="c1", redirect_uri="https://app.invalid/cb")
    accts = bank.accounts(access_token=ts.access_token)
    assert accts, "no accounts to check"
    for a in accts:
        assert a.account_id.startswith(fake.MARKER)
        assert fake.MARKER in a.display_label
    tx = bank.transactions(
        access_token=ts.access_token,
        account_id=accts[0].account_id,
        since=FIXED - timedelta(days=30),
        until=FIXED,
    )
    assert tx, "no transactions to check"
    for t in tx:
        assert t.transaction_id.startswith(fake.MARKER)
        assert fake.MARKER in t.description


def test_no_account_carries_a_full_iban_or_a_holder_name():
    for a in _bank().accounts(access_token="fake-access-x"):
        assert len(a.iban_tail) == 4 and a.iban_tail.isdigit()


def test_rows_land_inside_the_window_asked_for():
    """Against an INJECTED clock, so this cannot pass vacuously next month.

    AND IT DOES NOT CATCH A DATE WRITTEN INTO THE FILE, which is measured
    rather than assumed: mutating booked_at to a hardcoded 2026-03-01 inside
    this window leaves this test GREEN, because the hardcoded day happens to
    satisfy it. Only test_a_different_window_moves_every_row reds under that
    mutation. So this one proves the rows respect a window and that one
    proves the window is an input. Do not strengthen this test and delete
    that one; it is the only input where the two readings diverge.
    """
    since, until = FIXED - timedelta(days=30), FIXED
    tx = _bank().transactions(
        access_token="fake-access-x", account_id="FAKE-ACC-1", since=since, until=until
    )
    assert tx
    for t in tx:
        assert since < t.booked_at < until, f"{t.transaction_id} outside the window"


def test_a_different_window_moves_every_row():
    """If the rows were dated in the file this would return the same dates."""
    bank = _bank()
    kw = dict(access_token="fake-access-x", account_id="FAKE-ACC-1")
    a = bank.transactions(since=FIXED - timedelta(days=30), until=FIXED, **kw)
    b = bank.transactions(
        since=FIXED - timedelta(days=400), until=FIXED - timedelta(days=370), **kw
    )
    assert [t.booked_at for t in a] != [t.booked_at for t in b]


def test_window_must_not_be_inverted_or_empty():
    bank = _bank()
    kw = dict(access_token="fake-access-x", account_id="FAKE-ACC-1")
    with pytest.raises(bp.ProviderError, match="not after"):
        bank.transactions(since=FIXED, until=FIXED, **kw)
    with pytest.raises(bp.ProviderError, match="not after"):
        bank.transactions(since=FIXED, until=FIXED - timedelta(days=1), **kw)


def test_window_must_be_timezone_aware():
    bank = _bank()
    with pytest.raises(bp.ProviderError, match="aware"):
        bank.transactions(
            access_token="fake-access-x",
            account_id="FAKE-ACC-1",
            since=datetime(2026, 3, 1, 12, 0),
            until=datetime(2026, 3, 4, 12, 0),
        )


# --- the webhook, in all three states --------------------------------------


def test_a_good_signature_verifies():
    body = b"rail=sepa;code=AC04"
    headers = {fake.SIGNATURE_HEADER: fake.sign_body(body)}
    assert _bank().verify_webhook(body=body, headers=headers) is True


def test_a_wrong_signature_is_refused():
    body = b"rail=sepa;code=AC04"
    assert _bank().verify_webhook(body=body, headers={fake.SIGNATURE_HEADER: "0" * 64}) is False


def test_a_missing_signature_header_is_refused():
    assert _bank().verify_webhook(body=b"rail=sepa;code=AC04", headers={}) is False


def test_a_tampered_body_is_refused():
    """The signature is over the BODY, so moving a digit must break it."""
    body = b"rail=sepa;code=AC04;amount_minor=4500"
    headers = {fake.SIGNATURE_HEADER: fake.sign_body(body)}
    tampered = body.replace(b"4500", b"9900")
    assert _bank().verify_webhook(body=tampered, headers=headers) is False


def test_the_signature_header_is_matched_case_insensitively():
    body = b"rail=sepa;code=AC04"
    headers = {fake.SIGNATURE_HEADER.lower(): fake.sign_body(body)}
    assert _bank().verify_webhook(body=body, headers=headers) is True


# --- return events ----------------------------------------------------------


def test_a_return_event_parses_and_reaches_the_rails_table():
    ev = _bank().parse_return_event(
        body=b"rail=sepa;code=AC04;amount_minor=4500;currency=EUR"
    )
    assert ev is not None
    assert (ev.rail, ev.code, ev.amount_minor) == ("sepa", "AC04", 4500)
    decision = rails.lookup(ev.rail, ev.code)
    assert decision.matched is True


def test_an_entry_with_no_code_is_none_rather_than_a_guess():
    """None is the third answer: the feed spoke and it was not a return."""
    assert _bank().parse_return_event(body=b"type=balance;amount_minor=100") is None


def test_an_unparseable_body_is_none_rather_than_an_error():
    assert _bank().parse_return_event(body=b"\xff\xfe nonsense") is None


def test_a_return_event_carries_the_providers_own_words_for_the_unmapped_case():
    ev = _bank().parse_return_event(body=b"rail=ach;code=R01;amount_minor=100")
    assert ev is not None
    assert "R01" in ev.raw_diagnostic


def test_an_unmapped_code_lands_on_the_fallback_rather_than_erroring():
    """The fake can emit a code no table holds; the rail must say so."""
    ev = _bank().parse_return_event(body=b"rail=ach;code=R99;amount_minor=100")
    assert ev is not None
    decision = rails.lookup(ev.rail, ev.code)
    assert decision.matched is False
    assert decision.fallback_reason


# --- the subprocess tests: a real module on disk ---------------------------


def _import_in_copy(edit) -> subprocess.CompletedProcess:
    """Copy the tree, apply `edit` to app/bank/fake.py, import it fresh.

    A fresh interpreter is the point: the deny-list runs at import, and this
    process has already imported the module successfully.
    """
    root = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory() as tmp:
        dst = Path(tmp) / "tree"
        shutil.copytree(
            root / "app",
            dst / "app",
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )
        target = dst / "app" / "bank" / "fake.py"
        target.write_text(edit(target.read_text()))
        return subprocess.run(
            [sys.executable, "-c", "import app.bank.fake"],
            cwd=dst,
            capture_output=True,
            text=True,
            timeout=60,
        )


def test_the_unedited_module_imports_cleanly_in_a_copy():
    """The positive control. Without it a failure proves only that the copy
    is broken, and every refusal below would be worthless."""
    r = _import_in_copy(lambda s: s)
    assert r.returncode == 0, r.stderr


def test_a_money_moving_method_breaks_the_import_of_a_real_adapter():
    """The claim the in-process tests cannot make: a module somebody wrote,
    on disk, declaring a mutating verb plus a money noun, does not load."""

    def edit(src: str) -> str:
        anchor = "    # --- internals ----"
        assert anchor in src
        return src.replace(
            anchor,
            "    def create_payment(self, **kw):\n"
            "        raise NotImplementedError\n\n" + anchor,
        )

    r = _import_in_copy(edit)
    assert r.returncode != 0
    assert "PaymentInitiationRefused" in r.stderr
    assert "create_payment" in r.stderr or "could move money" in r.stderr


def test_a_payment_endpoint_breaks_the_import_of_a_real_adapter():
    def edit(src: str) -> str:
        old = '"consents": f"{FAKE_HOST}/v1/consents",'
        assert old in src
        return src.replace(
            old, old + '\n        "x": f"{FAKE_HOST}/v1/payments/initiate",'
        )

    r = _import_in_copy(edit)
    assert r.returncode != 0
    assert "PaymentInitiationRefused" in r.stderr


def test_a_bare_money_noun_method_breaks_the_import():
    """No mutating verb at all: `payments` alone is hard-banned."""

    def edit(src: str) -> str:
        anchor = "    # --- internals ----"
        return src.replace(
            anchor, "    def payments(self, **kw):\n        return ()\n\n" + anchor
        )

    r = _import_in_copy(edit)
    assert r.returncode != 0
    assert "PaymentInitiationRefused" in r.stderr


def test_an_innocent_money_noun_read_still_imports():
    """The passing case that decides whether the guard survives a real week.
    A money noun with no mutating verb is ordinary reading."""

    def edit(src: str) -> str:
        anchor = "    # --- internals ----"
        return src.replace(
            anchor,
            "    def payment_failed_events(self, **kw):\n        return ()\n\n" + anchor,
        )

    r = _import_in_copy(edit)
    assert r.returncode == 0, r.stderr
