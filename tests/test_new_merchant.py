"""The merchant install kit generator.

What matters here is what must NOT happen: a merchant pointed at the public demo
app, a merchant's details landing in this public repository, a secret printed,
a stage skipped, or a kit overwritten.
"""

from __future__ import annotations

import importlib.util
import json
import pathlib
import stat
import tomllib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _mod():
    spec = importlib.util.spec_from_file_location("new_merchant", ROOT / "scripts" / "new_merchant.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def nm(tmp_path, monkeypatch):
    monkeypatch.setenv("PAYPILOT_MERCHANT_HOME", str(tmp_path / "clients"))
    return _mod()


def _init(nm, slug="acme", **over):
    argv = ["init", slug, "--name", "Acme Ltd", "--approver", "owner@acme.test",
            "--update-url", "https://acme.test/billing"]
    for k, v in over.items():
        argv += [f"--{k.replace('_', '-')}", v]
    return nm.main(argv)


def _env(path):
    return dict(line.split("=", 1) for line in path.read_text().splitlines() if "=" in line)


# -- what init writes --------------------------------------------------------


def test_init_writes_the_four_files_privately(nm, tmp_path, capsys):
    assert _init(nm) == 0
    d = tmp_path / "clients" / "paypilot-acme"
    assert sorted(p.name for p in d.iterdir()) == ["RUNBOOK.md", "fly.toml", "merchant.json", "secrets.env"]
    assert stat.S_IMODE(d.stat().st_mode) == 0o700
    assert stat.S_IMODE((d / "secrets.env").stat().st_mode) == 0o600
    assert stat.S_IMODE((d / "merchant.json").stat().st_mode) == 0o600


def test_init_prints_no_secret_and_stores_them_only_in_the_env_file(nm, tmp_path, capsys):
    _init(nm)
    out = capsys.readouterr().out
    env = _env(tmp_path / "clients" / "paypilot-acme" / "secrets.env")

    assert len(env["ADMIN_TOKEN"]) >= 40 and len(env["PAYPILOT_PII_SALT"]) == 64
    assert env["ADMIN_TOKEN"] not in out and env["PAYPILOT_PII_SALT"] not in out
    record = (tmp_path / "clients" / "paypilot-acme" / "merchant.json").read_text()
    assert env["ADMIN_TOKEN"] not in record and env["PAYPILOT_PII_SALT"] not in record


def test_the_two_secrets_it_cannot_generate_are_left_blank_not_invented(nm, tmp_path):
    _init(nm)
    env = _env(tmp_path / "clients" / "paypilot-acme" / "secrets.env")
    assert env["RESEND_API_KEY"] == "" and env["STRIPE_WEBHOOK_SECRET"] == ""


def test_every_kit_gets_its_own_generated_secrets(nm, tmp_path):
    _init(nm, "acme")
    _init(nm, "bolt")
    a = _env(tmp_path / "clients" / "paypilot-acme" / "secrets.env")
    b = _env(tmp_path / "clients" / "paypilot-bolt" / "secrets.env")
    assert a["ADMIN_TOKEN"] != b["ADMIN_TOKEN"] and a["PAYPILOT_PII_SALT"] != b["PAYPILOT_PII_SALT"]


def test_a_new_kit_starts_with_sending_off(nm, tmp_path):
    _init(nm)
    env = _env(tmp_path / "clients" / "paypilot-acme" / "secrets.env")
    assert env["PAYPILOT_SEND_EMAIL"] == "0"
    assert env["PAYPILOT_APPROVAL_MODE"] == "email"
    assert env["PAYPILOT_PUBLIC_BASE_URL"] == "https://paypilot-acme.fly.dev"
    assert "*" not in env["PAYPILOT_ALLOWED_RECIPIENTS"]
    assert env["PAYPILOT_HOLDOUT_PCT"] == "0"


def test_the_link_guard_is_told_about_the_merchants_update_host(nm, tmp_path):
    """The guard's host list replaces the Stripe defaults, so omitting the
    merchant's host would block the first real fallback message."""
    _init(nm)
    hosts = _env(tmp_path / "clients" / "paypilot-acme" / "secrets.env")["PAYPILOT_ALLOWED_LINK_HOSTS"].split(",")
    assert "acme.test" in hosts and "billing.stripe.com" in hosts and "invoice.stripe.com" in hosts


def test_the_from_address_does_not_pretend_to_be_the_merchants_own_domain(nm, tmp_path):
    _init(nm)
    env = _env(tmp_path / "clients" / "paypilot-acme" / "secrets.env")
    assert env["PAYPILOT_FROM_EMAIL"] == "Acme Ltd via PayPilot <billing@streamflow.solutions>"


# -- fly.toml: never the demo app ---------------------------------------------


def test_the_rendered_fly_config_cannot_touch_the_demo_app_or_its_volume(nm, tmp_path):
    _init(nm)
    text = (tmp_path / "clients" / "paypilot-acme" / "fly.toml").read_text()
    cfg = tomllib.loads(text)

    assert cfg["app"] == "paypilot-acme"
    assert cfg["mounts"]["source"] == "paypilot_acme_data"
    assert "paypilot_data" not in text
    assert "PAYPILOT_FAKE_BANK" not in cfg["env"], "the offline fake bank must not exist on a merchant"
    assert cfg["http_service"]["max_machines_running"] == 1, "one machine, one ledger"
    assert cfg["env"]["PAYPILOT_DB_PATH"] == "/data/paypilot.db"
    assert cfg["env"]["PAYPILOT_TENANT"] == "default", "a merchant must not own the demo tenant"
    assert cfg["env"]["PAYPILOT_LEGACY_TENANT"] == "default"
    assert "`demo`" not in text


def test_a_template_without_the_expected_lines_is_refused_not_passed_through(nm):
    with pytest.raises(nm.Refused):
        nm.render_fly_toml("acme", 'app = "something-else"\n[mounts]\n  source = "other"\n')
    with pytest.raises(nm.Refused):
        nm.render_fly_toml("acme", 'app = "paypilot"\n')
    # tenant lines missing: refuse rather than emit a config with no tenant decision
    with pytest.raises(nm.Refused):
        nm.render_fly_toml("acme", 'app = "paypilot"\n[mounts]\n  source = "paypilot_data"\n')


def test_the_real_repo_fly_toml_still_matches_what_the_generator_rewrites(nm):
    """If someone renames the app or volume in fly.toml, this goes red here
    rather than shipping a merchant config that quietly points at the demo."""
    cfg = tomllib.loads(nm.render_fly_toml("acme"))
    assert cfg["app"] == "paypilot-acme"


def test_the_runbook_names_the_webhook_url_and_all_four_events(nm, tmp_path):
    _init(nm)
    rb = (tmp_path / "clients" / "paypilot-acme" / "RUNBOOK.md").read_text()
    assert "https://paypilot-acme.fly.dev/webhooks/stripe" in rb
    for ev in ("invoice.payment_failed", "invoice.paid", "invoice.payment_succeeded",
               "customer.subscription.deleted"):
        assert ev in rb


# -- refusals ----------------------------------------------------------------


@pytest.mark.parametrize("slug", ["demo", "paypilot", "test", "staging", "prod"])
def test_reserved_names_are_refused(nm, slug, tmp_path):
    assert _init(nm, slug) == 1
    assert not (tmp_path / "clients").exists()


@pytest.mark.parametrize("slug", ["ab", "Acme", "acme_co", "-acme", "acme-", "1acme", "a" * 40, "ac me", "../x"])
def test_bad_slugs_are_refused(nm, slug):
    assert _init(nm, slug) == 1


def test_an_existing_kit_is_never_overwritten(nm, tmp_path):
    assert _init(nm) == 0
    env_before = (tmp_path / "clients" / "paypilot-acme" / "secrets.env").read_text()
    assert _init(nm) == 1
    assert (tmp_path / "clients" / "paypilot-acme" / "secrets.env").read_text() == env_before


@pytest.mark.parametrize("bad", ["not-an-email", "a@b", "a@b.test, nope", "", "a b@c.test"])
def test_bad_approvers_are_refused(nm, bad):
    assert nm.main(["init", "acme", "--name", "Acme", "--approver", bad,
                    "--update-url", "https://acme.test/b"]) == 1


@pytest.mark.parametrize("bad", ["http://acme.test/b", "ftp://x.test", "https://", "acme.test/b",
                                 "https://user:pw@acme.test/b", "javascript:alert(1)", ""])
def test_the_update_url_must_be_plain_https(nm, bad):
    assert nm.main(["init", "acme", "--name", "Acme", "--approver", "o@a.test",
                    "--update-url", bad]) == 1


def test_a_missing_update_url_is_refused_because_the_default_is_the_demo_page(nm):
    assert nm.main(["init", "acme", "--name", "Acme", "--approver", "o@a.test"]) == 1


@pytest.mark.parametrize("name", ["", "A" * 81, "Acme\nInjected: 1", "Acme <b>"])
def test_hostile_names_are_refused(nm, name):
    assert nm.main(["init", "acme", "--name", name, "--approver", "o@a.test",
                    "--update-url", "https://acme.test/b"]) == 1


def test_a_hostile_from_address_is_refused(nm):
    assert _init(nm, from_email="Acme <b@x.test>\r\nBcc: spy@evil.test") == 1


def test_multiple_approvers_are_kept_in_order_and_deduplicated(nm, tmp_path):
    nm.main(["init", "acme", "--name", "Acme", "--approver", "A@x.test, b@x.test,a@x.test",
             "--update-url", "https://acme.test/b"])
    env = _env(tmp_path / "clients" / "paypilot-acme" / "secrets.env")
    assert env["PAYPILOT_APPROVER_EMAILS"] == "a@x.test,b@x.test"


# -- stages ------------------------------------------------------------------


def _stage(nm, *a):
    return nm.main(["stage", "acme", *a])


def test_test_stage_turns_sending_on_for_the_approver_and_test_address_only(nm, tmp_path):
    _init(nm, test_recipient="qa@acme.test")
    assert _stage(nm, "test") == 0
    env = _env(tmp_path / "clients" / "paypilot-acme" / "secrets.env")
    assert env["PAYPILOT_SEND_EMAIL"] == "1"
    assert env["PAYPILOT_ALLOWED_RECIPIENTS"] == "qa@acme.test,owner@acme.test"


def test_live_cannot_be_reached_without_both_claims(nm):
    _init(nm)
    _stage(nm, "test")
    assert _stage(nm, "live") == 1
    assert _stage(nm, "live", "--dpa-signed") == 1
    assert _stage(nm, "live", "--test-passed") == 1


def test_live_cannot_skip_the_test_stage(nm, tmp_path):
    _init(nm)
    assert _stage(nm, "live", "--dpa-signed", "--test-passed") == 1
    env = _env(tmp_path / "clients" / "paypilot-acme" / "secrets.env")
    assert env["PAYPILOT_SEND_EMAIL"] == "0" and "*" not in env["PAYPILOT_ALLOWED_RECIPIENTS"]


def test_live_opens_the_allowlist_and_keeps_the_generated_secrets(nm, tmp_path):
    _init(nm)
    before = _env(tmp_path / "clients" / "paypilot-acme" / "secrets.env")
    _stage(nm, "test")
    assert _stage(nm, "live", "--dpa-signed", "--test-passed") == 0
    env = _env(tmp_path / "clients" / "paypilot-acme" / "secrets.env")
    assert env["PAYPILOT_ALLOWED_RECIPIENTS"] == "*" and env["PAYPILOT_SEND_EMAIL"] == "1"
    assert env["ADMIN_TOKEN"] == before["ADMIN_TOKEN"]
    assert env["PAYPILOT_PII_SALT"] == before["PAYPILOT_PII_SALT"]
    assert json.loads((tmp_path / "clients" / "paypilot-acme" / "merchant.json").read_text())["stage"] == "live"


def test_stage_dry_is_the_off_switch_from_any_stage(nm, tmp_path):
    _init(nm)
    _stage(nm, "test")
    _stage(nm, "live", "--dpa-signed", "--test-passed")
    assert _stage(nm, "dry") == 0
    env = _env(tmp_path / "clients" / "paypilot-acme" / "secrets.env")
    assert env["PAYPILOT_SEND_EMAIL"] == "0" and "*" not in env["PAYPILOT_ALLOWED_RECIPIENTS"]


def test_live_does_not_step_back_to_test_without_going_dry_first(nm):
    _init(nm)
    _stage(nm, "test")
    _stage(nm, "live", "--dpa-signed", "--test-passed")
    assert _stage(nm, "test") == 1


def test_an_unknown_stage_or_merchant_is_refused(nm):
    _init(nm)
    assert _stage(nm, "prod") == 1
    assert nm.main(["stage", "ghost", "test"]) == 1


# -- the kit passes the real preflight ----------------------------------------


def _preflight():
    spec = importlib.util.spec_from_file_location("approval_preflight", ROOT / "scripts" / "approval_preflight.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _fill_blanks(path):
    lines = []
    for line in path.read_text().splitlines():
        key, _, val = line.partition("=")
        if key in ("RESEND_API_KEY", "STRIPE_WEBHOOK_SECRET") and not val:
            line = f"{key}=filled-{key.lower()}"
        lines.append(line)
    path.write_text("\n".join(lines) + "\n")


def test_a_fresh_kit_is_not_ready_until_the_operator_fills_the_two_blanks(nm, tmp_path, capsys):
    _init(nm)
    env = tmp_path / "clients" / "paypilot-acme" / "secrets.env"
    assert _preflight().main(["--env-file", str(env)]) == 1
    out = capsys.readouterr().out
    assert "RESEND_API_KEY" in out and "STRIPE_WEBHOOK_SECRET" in out


@pytest.mark.parametrize("stage_args", [[], ["test"]])
def test_a_filled_kit_passes_preflight(nm, tmp_path, capsys, monkeypatch, stage_args):
    """The generator and the checker must agree, or onboarding dead-ends."""
    for k in ("PAYPILOT_ALLOWED_RECIPIENTS", "RESEND_API_KEY", "STRIPE_WEBHOOK_SECRET", "ADMIN_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    _init(nm)
    if stage_args:
        _stage(nm, *stage_args)
    env = tmp_path / "clients" / "paypilot-acme" / "secrets.env"
    _fill_blanks(env)
    assert _preflight().main(["--env-file", str(env)]) == 0
    assert "READY" in capsys.readouterr().out


# -- nothing about a merchant can land in this repository ---------------------


def test_the_default_kit_location_is_outside_the_repo(monkeypatch):
    monkeypatch.delenv("PAYPILOT_MERCHANT_HOME", raising=False)
    home = _mod().home().resolve()
    assert ROOT.resolve() not in (home, *home.parents)


def test_a_rendered_merchant_config_in_the_repo_directory_is_gitignored():
    ignore = (ROOT / ".gitignore").read_text()
    assert "fly.paypilot-*.toml" in ignore, (
        "ppmerchant copies a merchant's fly.toml next to the Dockerfile to deploy it; "
        "without this a slug, which names the merchant, could be committed to a public repo"
    )
