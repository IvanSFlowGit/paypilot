"""The bank provider seam, and the two layers that keep it read-only.

The cases that decide whether this guard survives contact with a real week
are the ones it must WAVE THROUGH, not the ones it blocks. A guard that
refuses `payment_failed_events` or an endpoint on a host called
payments.bank.example is switched off by Friday, and then every shape it
caught correctly is live again. Those two are carried here as controls.
"""

from __future__ import annotations

import pathlib
import shutil
import subprocess
import sys
from datetime import datetime, timezone

import pytest

from app.bank import provider as bp


@pytest.fixture(autouse=True)
def _clean_registry():
    """Restore the registry. A test that leaks a name poisons every test after."""
    before = dict(bp._REGISTERED)
    yield
    bp._REGISTERED.clear()
    bp._REGISTERED.update(before)


class _Conforming:
    """Implements the protocol and nothing else."""

    PROVIDER_NAME = "fixture"

    def authorize_url(self, *, state, redirect_uri, scopes):
        return "https://bank.example/auth"

    def exchange_code(self, *, code, redirect_uri):
        return bp.TokenSet("a", None, datetime.now(timezone.utc), None, ())

    def refresh(self, *, refresh_token):
        return bp.TokenSet("a", None, datetime.now(timezone.utc), None, ())

    def accounts(self, *, access_token):
        return ()

    def transactions(self, *, access_token, account_id, since, until):
        return ()

    def verify_webhook(self, *, body, headers):
        return True

    def parse_return_event(self, *, body):
        return None


# --------------------------------------------------------------------------
# What must be allowed through
# --------------------------------------------------------------------------

def test_a_conforming_adapter_registers_and_is_listed():
    bp.register(_Conforming)
    assert "fixture" in bp.providers()
    assert bp.get("fixture") is _Conforming


def test_the_protocol_is_satisfiable_at_runtime():
    assert isinstance(_Conforming(), bp.BankProvider)


def test_a_money_noun_with_no_mutating_verb_is_ordinary_reading():
    """Control. Reading failed payments IS the product; blocking it kills the guard."""

    class Reader(_Conforming):
        PROVIDER_NAME = "reader"

        def payment_failed_events(self, *, access_token):
            return ()

    bp.register(Reader)
    assert "reader" in bp.providers()


def test_authorize_url_carries_a_mutating_verb_and_is_still_fine():
    """Control. authorize is in MUTATING_VERBS and url is not a money noun."""
    assert bp._method_refusal("authorize_url") is None
    assert "authorize" in bp.MUTATING_VERBS


def test_a_host_named_for_payments_is_not_a_payment_endpoint():
    """Control. Only the path is read, because a bank may be named anything.

    The host is HYPHENATED on purpose. payments.bank.example is one dotted
    token that _words never splits, so it passes whether the checker reads
    the path or the whole URL and proves nothing about either. Mutating the
    checker to read the whole URL is what exposed that, and this input is
    the one where the two readings diverge.
    """

    class HostNamed(_Conforming):
        PROVIDER_NAME = "hostnamed"
        ENDPOINTS = {"accounts": "https://payments-api.bank.example/v1/accounts"}

    bp.register(HostNamed)
    assert "hostnamed" in bp.providers()


def test_an_ordinary_transactions_endpoint_passes():
    class Txns(_Conforming):
        PROVIDER_NAME = "txns"
        ENDPOINTS = {"txns": "https://bank.example/v1/accounts/{id}/transactions"}

    bp.register(Txns)
    assert "txns" in bp.providers()


# --------------------------------------------------------------------------
# Layer 1: method names
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "name",
    ["initiate_payment", "create_transfer", "submit_payout", "schedule_mandate"],
)
def test_a_mutating_verb_with_a_money_noun_refuses(name):
    attrs = {"PROVIDER_NAME": "bad", name: lambda self: None}
    bad = type("Bad", (_Conforming,), attrs)
    with pytest.raises(bp.PaymentInitiationRefused, match="could move money"):
        bp.register(bad)


@pytest.mark.parametrize("name", ["payments", "pay", "transfers", "pis"])
def test_a_bare_money_noun_refuses(name):
    attrs = {"PROVIDER_NAME": "bad", name: lambda self: None}
    bad = type("Bad", (_Conforming,), attrs)
    with pytest.raises(bp.PaymentInitiationRefused, match="read-only"):
        bp.register(bad)


def test_the_refusal_names_the_verb_and_the_noun_it_found():
    reason = bp._method_refusal("create_payment")
    assert reason is not None
    assert "create" in reason and "payment" in reason


def test_an_underscored_helper_is_not_inspected():
    """A private helper is not the interface, and naming it is not a claim."""

    class Private(_Conforming):
        PROVIDER_NAME = "private"

        def _create_payment_url(self):  # noqa: D401
            return ""

    bp.register(Private)
    assert "private" in bp.providers()


# --------------------------------------------------------------------------
# Layer 2: declared endpoints. A generic request helper has an innocent name,
# so the name check cannot see the endpoint it is pointed at.
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "url",
    [
        "https://bank.example/open-banking/v3.1/pisp/domestic-payments",
        "https://bank.example/pis/v3/accounts",
        "https://bank.example/v1/payouts",
        "/v2/standing_order/mandates",
    ],
)
def test_a_payment_path_segment_refuses(url):
    attrs = {"PROVIDER_NAME": "bad", "ENDPOINTS": {"x": url}}
    bad = type("Bad", (_Conforming,), attrs)
    with pytest.raises(bp.PaymentInitiationRefused, match="payment endpoint"):
        bp.register(bad)


def test_endpoints_given_as_a_sequence_are_checked_too():
    attrs = {"PROVIDER_NAME": "bad", "ENDPOINTS": ("https://b.example/v1/payments",)}
    bad = type("Bad", (_Conforming,), attrs)
    with pytest.raises(bp.PaymentInitiationRefused):
        bp.register(bad)


def test_a_non_string_endpoint_is_refused_rather_than_skipped():
    attrs = {"PROVIDER_NAME": "bad", "ENDPOINTS": {"x": None}}
    bad = type("Bad", (_Conforming,), attrs)
    with pytest.raises(bp.RegistryError, match="not a string"):
        bp.register(bad)


# --------------------------------------------------------------------------
# The registry's own refusals
# --------------------------------------------------------------------------

def test_an_adapter_missing_a_protocol_method_is_refused_and_it_is_named():
    class Partial:
        PROVIDER_NAME = "partial"

        def accounts(self, *, access_token):
            return ()

    with pytest.raises(bp.RegistryError, match="transactions"):
        bp.register(Partial)


def test_an_adapter_with_no_provider_name_is_refused():
    class Nameless(_Conforming):
        PROVIDER_NAME = ""

    with pytest.raises(bp.RegistryError, match="PROVIDER_NAME"):
        bp.register(Nameless)


def test_two_adapters_cannot_claim_one_name():
    class A(_Conforming):
        PROVIDER_NAME = "clash"

    class B(_Conforming):
        PROVIDER_NAME = "clash"

    bp.register(A)
    with pytest.raises(bp.RegistryError, match="both claim"):
        bp.register(B)


def test_registering_something_that_is_not_a_class_is_refused():
    with pytest.raises(bp.RegistryError, match="expects a class"):
        bp.register(_Conforming())


def test_an_unknown_provider_is_refused_and_the_known_ones_are_named():
    bp.register(_Conforming)
    with pytest.raises(bp.RegistryError, match="unknown provider"):
        bp.get("nobody")
    try:
        bp.get("nobody")
    except bp.RegistryError as exc:
        assert "fixture" in str(exc)


def test_a_non_string_provider_name_is_refused_rather_than_coerced():
    """Strict where app.rails normalises, and that divergence is deliberate.

    A rail name is typed by a person. A provider name arrives as a URL path
    segment on /webhooks/bank/{provider}, where whitespace is a sign of
    something wrong rather than of sloppy typing.
    """
    with pytest.raises(bp.RegistryError, match="must be a string"):
        bp.get(7)


# --------------------------------------------------------------------------
# The protocol itself. No method on it moves money, asserted rather than read.
# --------------------------------------------------------------------------

def test_no_required_method_would_be_refused_by_the_deny_list():
    for name in bp.REQUIRED_METHODS:
        assert bp._method_refusal(name) is None, name


def test_the_required_set_is_exactly_the_protocol_and_is_sorted():
    on_protocol = {
        name
        for name, value in vars(bp.BankProvider).items()
        if not name.startswith("_") and callable(value)
    }
    assert set(bp.REQUIRED_METHODS) == on_protocol
    assert list(bp.REQUIRED_METHODS) == sorted(bp.REQUIRED_METHODS)


def test_the_two_marker_sets_do_not_overlap():
    """A word that is both a verb and a noun would make the AND test a no-op."""
    assert not (bp.MUTATING_VERBS & bp.MONEY_NOUNS)


# --------------------------------------------------------------------------
# Import-time: a subprocess, because a module already in sys.modules does not
# re-run its own validation.
# --------------------------------------------------------------------------

_APP = pathlib.Path(__file__).resolve().parent.parent / "app"


def _tree(tmp_path: pathlib.Path) -> pathlib.Path:
    root = tmp_path / "copy"
    root.mkdir()
    shutil.copytree(_APP, root / "app", ignore=shutil.ignore_patterns("__pycache__"))
    return root


def _import_provider(root: pathlib.Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", "import app.bank.provider"],
        cwd=root,
        capture_output=True,
        text=True,
    )


def test_the_copied_provider_imports_cleanly(tmp_path):
    """Positive control. Without it every refusal below could be a bad copy."""
    done = _import_provider(_tree(tmp_path))
    assert done.returncode == 0, done.stderr


def test_renaming_a_protocol_method_breaks_the_import_in_both_directions(tmp_path):
    root = _tree(tmp_path)
    path = root / "app" / "bank" / "provider.py"
    src = path.read_text()
    assert src.count('    "accounts",\n') == 1
    path.write_text(src.replace('    "accounts",\n', '    "balances",\n'))
    done = _import_provider(root)
    assert done.returncode != 0
    assert "declared-only ['balances']" in done.stderr
    assert "protocol-only ['accounts']" in done.stderr


def test_a_money_noun_on_the_protocol_breaks_the_import(tmp_path):
    """The protocol cannot quietly grow a method the deny-list would refuse."""
    root = _tree(tmp_path)
    path = root / "app" / "bank" / "provider.py"
    src = path.read_text()
    src = src.replace(
        '    "accounts",\n    "authorize_url",',
        '    "accounts",\n    "authorize_url",\n    "create_payment",',
    )
    src = src.replace(
        "    def exchange_code(",
        "    def create_payment(self, *, amount_minor: int) -> None: ...\n\n"
        "    def exchange_code(",
    )
    path.write_text(src)
    done = _import_provider(root)
    assert done.returncode != 0
    assert "the protocol is wrong, not the deny-list" in done.stderr
