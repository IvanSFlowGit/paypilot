"""Every store whose default is a FILE must be pointed at the Fly volume.

WHY THIS EXISTS. app/bank/consent.py defaults its database to the RELATIVE path
"consents.db". In the container WORKDIR is /app, which the Dockerfile's
`chown -R appuser:appuser /app` makes writable, so a consent token would be
written SUCCESSFULLY into the image layer and then wiped by the next deploy.
Nothing would error. fly.toml already carries that hazard in prose for the two
stores beside it, and prose is not a mechanism: before this file, nothing in the
repository read fly.toml at all.

A lost consent token is worse than a lost decision row, because recovering it
means another consent journey with somebody's bank.

WHY THE RULE IS ABOUT THE DEFAULT AND NOT ABOUT THE NAME. A blanket "every
*_DB_PATH must appear in fly.toml" would fire on PAYPILOT_AUDIT_DB_PATH, which
is correct as it stands: it defaults to :memory: AND app/audit.py:216 only
enables the queryable log `if os.getenv(_AUDIT_DB_ENV)`, so unset means the
feature is off by design rather than writing somewhere doomed. A gate that
refuses correct work is switched off by Friday, so this one asks the question
that actually separates the two cases: does the default resolve to a file?
"""

from __future__ import annotations

import json
import pathlib
import re
import subprocess

ROOT = pathlib.Path(__file__).resolve().parents[1]
FLY = ROOT / "fly.toml"

# name -> the literal its default falls back to, read from the source rather
# than restated, so a changed default reds this row instead of sliding past.
DEFAULTS = {
    "PAYPILOT_DB_PATH": ("app/store.py", "_DEFAULT_DB_PATH"),
    "PAYPILOT_DECISION_AUDIT_DB_PATH": ("app/decision_audit.py", ":memory:"),
    "PAYPILOT_CONSENT_DB_PATH": ("app/bank/consent.py", "consents.db"),
    "PAYPILOT_AUDIT_DB_PATH": ("app/audit.py", ":memory:"),
}


def _fly_env() -> dict[str, str]:
    """The [env] block only. A name in a comment is not a setting."""
    text = FLY.read_text(encoding="utf-8")
    block = text.split("[env]", 1)[1].split("\n[", 1)[0]
    out = {}
    for line in block.splitlines():
        line = line.split("#", 1)[0].strip()
        m = re.match(r'^([A-Z_]+)\s*=\s*"([^"]*)"$', line)
        if m:
            out[m.group(1)] = m.group(2)
    return out


def test_every_store_env_is_still_named_in_the_source():
    """Otherwise this file asserts a mapping the code has moved on from."""
    for name, (rel, _) in DEFAULTS.items():
        src = (ROOT / rel).read_text(encoding="utf-8")
        assert name in src, f"{name} is no longer read in {rel}"


def test_a_file_backed_default_is_overridden_onto_the_volume():
    env = _fly_env()
    for name, (rel, default) in DEFAULTS.items():
        if default == ":memory:":
            continue  # unset means off or in-process, not a doomed file
        assert name in env, (
            f"{name} defaults to a FILE ({default!r} in {rel}) and is not in "
            f"fly.toml [env], so the container writes it into the image layer "
            f"and every deploy wipes it, silently"
        )
        assert env[name].startswith("/data/"), (
            f"{name} is set to {env[name]!r}, which is not on the mounted volume"
        )


def test_the_in_memory_defaults_are_not_required_to_be_set():
    """The control. Without it the rule above could be a blanket one and nobody
    would notice it was refusing a correct configuration."""
    env = _fly_env()
    assert DEFAULTS["PAYPILOT_AUDIT_DB_PATH"][1] == ":memory:"
    # Absent is fine here, and the suite must not start demanding it.
    assert "PAYPILOT_AUDIT_DB_PATH" not in env or env["PAYPILOT_AUDIT_DB_PATH"].startswith("/data/")


def test_the_fake_bank_is_explicit_wherever_it_is_set():
    """A provider returning synthetic data must never be on by accident.

    app/bank/fake.py:128 raises unless the env is set to something other than
    "" or "0", and says so: it "must never be reachable by a configuration
    typo". So if fly.toml sets it at all, it is set to a value that enables it
    on purpose rather than to something that silently does nothing.
    """
    env = _fly_env()
    if "PAYPILOT_FAKE_BANK" in env:
        assert env["PAYPILOT_FAKE_BANK"] not in ("", "0"), (
            "PAYPILOT_FAKE_BANK is present but set to a value fake.py treats as "
            "off, which reads as enabled and is not"
        )


# --- the consent key target -------------------------------------------------
#
# This asserts the SOURCE cannot print the key, not that a run did not print
# one. flyctl cannot authenticate from this environment at all (it fails with
# tls: failed to verify certificate: x509: OSStatus -26276), so the target is
# unrunnable here and a behavioural test would be a test of nothing. Saying so
# is part of the assertion: what is proven is that no line in the recipe can
# emit the value, which is the property that matters because a credential in a
# transcript is burned.

def _consent_key_recipe() -> list[str]:
    lines = (ROOT / "Makefile").read_text(encoding="utf-8").splitlines()
    start = next(i for i, ln in enumerate(lines) if ln.startswith("consent-key:"))
    out = []
    for ln in lines[start + 1:]:
        if ln and not ln[0].isspace():
            break
        out.append(ln)
    assert out, "consent-key recipe is empty"
    return out


def test_the_consent_key_target_exists():
    assert any("flyctl secrets set PAYPILOT_CONSENT_KEY" in ln for ln in _consent_key_recipe())


def test_the_key_variable_is_only_ever_handed_to_flyctl():
    """The one assertion that matters. $$k may be passed to flyctl and its
    LENGTH may be printed; anything else is a path to a burned credential."""
    allowed = (
        'flyctl secrets set PAYPILOT_CONSENT_KEY="$$k"',
        '"$${#k}"',
    )
    for ln in _consent_key_recipe():
        if "$$k" not in ln:
            continue
        rest = ln
        for a in allowed:
            rest = rest.replace(a, "")
        assert "$$k" not in rest, (
            f"the key variable is used somewhere other than the flyctl call or "
            f"its length, which is how a credential reaches a screen: {ln.strip()!r}"
        )


def test_it_refuses_rather_than_rotating_an_existing_key():
    body = "\n".join(_consent_key_recipe())
    assert "refusing: PAYPILOT_CONSENT_KEY already exists" in body
    assert "undecryptable" in body, "the reason has to travel with the refusal"


def test_it_fails_closed_when_it_cannot_read_the_secrets():
    """"the grep found nothing" and "the question was never asked" are
    different answers, and only one of them means it is safe to set a key."""
    body = "\n".join(_consent_key_recipe())
    assert "cannot read the current secrets" in body
    assert "exit 2" in body, "an unreadable list must not share an exit code with a refusal"


# --- the documented env surface ---------------------------------------------
#
# The README states a count. A count in prose goes stale on the next variable
# added, and this estate has a register full of figures that travelled between
# documents by being retyped. So the number is asserted against the probe
# rather than maintained by hand: adding a variable reds this row until the
# README is updated, which is the only thing that keeps the two in step.


def _probe() -> dict[str, list[str]]:
    out = subprocess.run(
        [str(ROOT / ".venv/bin/python"), str(ROOT / "scripts/env_surface.py"), "--json"],
        capture_output=True, text=True, cwd=ROOT,
    )
    assert out.returncode == 0, (
        f"env_surface.py exited {out.returncode}. Exit 2 means its own control "
        f"did not fire, so it has gone blind to constant indirection: {out.stderr}"
    )
    return json.loads(out.stdout)


def test_the_probe_control_fires_and_finds_the_indirect_read():
    """PAYPILOT_CONSENT_KEY reaches os.environ.get as a parameter default whose
    value is a module constant, two hops from any literal. If the probe stops
    finding it, the probe has the blind spot the grep had."""
    assert "PAYPILOT_CONSENT_KEY" in _probe()


def test_the_readme_count_matches_the_probe():
    n = len(_probe())
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert f"reads **{n}** environment variables" in readme, (
        f"the probe finds {n} variables and the README states a different "
        f"number; a count in prose has to be asserted or it goes stale"
    )


def test_every_documented_secret_is_actually_read_by_the_app():
    """A row for a variable nothing reads is worse than a missing row: it
    sends somebody to set something that has no effect."""
    surface = _probe()
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    block = readme.split("### Secrets", 1)[1].split("###", 1)[0]
    documented = set(re.findall(r"`([A-Z][A-Z0-9_]{3,})`", block))
    assert documented, "the secrets table parsed as empty"
    missing = sorted(documented - set(surface))
    assert not missing, f"documented but read nowhere in app/: {missing}"


# --- the live consent walk ----------------------------------------------------

def test_consent_walk_refuses_to_run_without_a_token(tmp_path):
    """Exit 2 means could-not-run, which must differ from 1 (a step refused).
    With no token nothing may be called, so this needs no network."""
    import os
    env = {k: v for k, v in os.environ.items() if k != "DECISION_API_TOKEN"}
    env["HOME"] = str(tmp_path)  # no real token file may satisfy this
    out = subprocess.run(
        [str(ROOT / ".venv/bin/python"), str(ROOT / "scripts/consent_walk.py")],
        capture_output=True, text=True, cwd=ROOT, env=env,
    )
    assert out.returncode == 2 and "Nothing was called" in out.stdout


def _walk_module():
    import importlib.util
    spec = importlib.util.spec_from_file_location("consent_walk", ROOT / "scripts/consent_walk.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_token_file_wider_than_0600_is_refused(tmp_path, monkeypatch):
    m = _walk_module()
    monkeypatch.delenv("DECISION_API_TOKEN", raising=False)
    f = tmp_path / "decision_token"
    f.write_text("not-a-real-token\n")
    f.chmod(0o644)
    token, why = m.load_token(f)
    assert token is None and "chmod 600" in why
    f.chmod(0o600)
    assert m.load_token(f) == ("not-a-real-token", "file")


def test_env_token_wins_over_the_file(tmp_path, monkeypatch):
    m = _walk_module()
    f = tmp_path / "decision_token"
    f.write_text("from-file\n")
    f.chmod(0o600)
    monkeypatch.setenv("DECISION_API_TOKEN", "from-env")
    assert m.load_token(f) == ("from-env", "env")


def test_a_one_entry_map_is_unwrapped_and_a_many_entry_map_is_not_guessed():
    m = _walk_module()
    assert m.unwrap('{"abc123xyz": "acme"}') == ("abc123xyz", "unwrapped from a one-entry JSON map")
    bearer, note = m.unwrap('{"k1secretvalue": "acme", "k2secretvalue": "beta"}')
    assert bearer is None and "acme" in note and "beta" in note
    assert "k1secretvalue" not in note and "k2secretvalue" not in note
    assert m.unwrap('{"k1secretvalue": "acme", "k2secretvalue": "beta"}', "beta")[0] == "k2secretvalue"
    assert m.unwrap("opaque-token-value") == ("opaque-token-value", "plain")


def test_a_401_diagnosis_leaks_no_part_of_the_token():
    m = _walk_module()
    token = "Zq9xK2mP7vL4wR8t"
    out = m.diagnose(token, "file")
    assert str(len(token)) in out
    for i in range(len(token) - 5):
        assert token[i:i + 6] not in out
