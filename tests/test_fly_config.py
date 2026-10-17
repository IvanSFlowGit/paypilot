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


def _save_module():
    import importlib.util
    spec = importlib.util.spec_from_file_location("save_token", ROOT / "scripts/save_token.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


# The exact text that ended up in the token file on 2026-10-17: 43 characters.
THE_COMMAND = "pbpaste > ~/.config/paypilot/decision_token"


def test_save_token_refuses_the_command_text_and_empty():
    m = _save_module()
    assert len(THE_COMMAND) == 43
    assert m.validate(THE_COMMAND) and "whitespace" in m.validate(THE_COMMAND)
    assert m.validate("") == "empty"
    assert m.validate("Zq9xK2mP7vL4wR8tNoSpaces") is None


def test_save_token_writes_0600_and_prints_no_part_of_the_value(tmp_path, monkeypatch, capsys):
    import stat
    m = _save_module()
    f = tmp_path / "cfg" / "decision_token"
    monkeypatch.setattr(m, "TOKEN_FILE", f)
    token = "Zq9xK2mP7vL4wR8tNoSpaces"
    monkeypatch.setattr(m.getpass, "getpass", lambda prompt="": token + "\n")
    assert m.main([]) == 0
    assert stat.S_IMODE(f.stat().st_mode) == 0o600
    assert f.read_text().strip() == token
    out = capsys.readouterr().out
    assert str(len(token)) in out
    for i in range(len(token) - 5):
        assert token[i:i + 6] not in out


def test_save_token_will_not_overwrite_without_force(tmp_path, monkeypatch):
    m = _save_module()
    f = tmp_path / "decision_token"
    f.write_text("existing\n")
    monkeypatch.setattr(m, "TOKEN_FILE", f)
    monkeypatch.setattr(m.getpass, "getpass", lambda prompt="": "Zq9xK2mP7vL4wR8t")
    assert m.main([]) == 2 and f.read_text() == "existing\n"
    assert m.main(["--force"]) == 0 and f.read_text().strip() == "Zq9xK2mP7vL4wR8t"


def test_save_token_refuses_whitespace_through_main(tmp_path, monkeypatch):
    m = _save_module()
    f = tmp_path / "decision_token"
    monkeypatch.setattr(m, "TOKEN_FILE", f)
    monkeypatch.setattr(m.getpass, "getpass", lambda prompt="": THE_COMMAND)
    assert m.main([]) == 1 and not f.exists()


def test_the_walk_refuses_the_command_text_before_any_call(tmp_path, monkeypatch):
    m = _walk_module()
    monkeypatch.delenv("DECISION_API_TOKEN", raising=False)
    f = tmp_path / "decision_token"
    f.write_text(THE_COMMAND + "\n")
    f.chmod(0o600)
    token, why = m.load_token(f)
    assert token is None and "whitespace" in why and "save-token" in why


# --- rotating the decision token ---------------------------------------------

def _rotate_module():
    import importlib.util
    import sys
    sys.path.insert(0, str(ROOT / "scripts"))
    spec = importlib.util.spec_from_file_location("rotate_decision_token", ROOT / "scripts/rotate_decision_token.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _fake_flyctl(tmp_path, monkeypatch, *, listing="", import_exit=0):
    """A flyctl on PATH that records argv and stdin of the import call."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (tmp_path / "argv").write_text("")
    script = bindir / "flyctl"
    script.write_text(
        "#!/bin/sh\n"
        f'echo "$@" >> "{tmp_path}/argv"\n'
        'if [ "$1 $2" = "secrets list" ]; then printf "%s" "$FAKE_LISTING"; exit 0; fi\n'
        f'if [ "$1 $2" = "secrets import" ]; then cat > "{tmp_path}/stdin"; exit {import_exit}; fi\n'
        "exit 0\n"
    )
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}:/usr/bin:/bin")
    monkeypatch.setenv("FAKE_LISTING", listing)


def _rotate_setup(tmp_path, monkeypatch, **kw):
    r = _rotate_module()
    f = tmp_path / "cfg" / "decision_token"
    monkeypatch.setattr(r.save_token, "TOKEN_FILE", f)
    monkeypatch.setattr(r, "verify", lambda host: (True, "start returned 200"))
    _fake_flyctl(tmp_path, monkeypatch, **kw)
    return r, f


def test_rotation_sends_the_value_on_stdin_never_in_argv_and_prints_none_of_it(tmp_path, monkeypatch, capsys):
    r, f = _rotate_setup(tmp_path, monkeypatch, listing="NAME DIGEST\nPAYPILOT_CONSENT_KEY abc\n")
    assert r.main([]) == 0
    token = f.read_text().strip()
    assert (tmp_path / "stdin").read_text() == f"DECISION_API_TOKEN={token}\n"
    assert token not in (tmp_path / "argv").read_text()
    out = capsys.readouterr().out
    assert str(len(token)) in out
    for i in range(len(token) - 5):
        assert token[i:i + 6] not in out


def test_an_existing_secret_needs_rotate_and_nothing_is_changed_without_it(tmp_path, monkeypatch):
    r, f = _rotate_setup(tmp_path, monkeypatch, listing="NAME DIGEST\nDECISION_API_TOKEN abc\n")
    assert r.main([]) == 1
    assert not f.exists() and not (tmp_path / "stdin").exists()
    assert r.main(["--rotate"]) == 0 and f.exists()


def test_a_flyctl_failure_leaves_no_local_file(tmp_path, monkeypatch):
    r, f = _rotate_setup(tmp_path, monkeypatch, listing="", import_exit=1)
    assert r.main([]) == 1
    assert not f.exists()


def test_an_unlistable_secret_set_is_exit_2_and_changes_nothing(tmp_path, monkeypatch):
    r, f = _rotate_setup(tmp_path, monkeypatch)
    monkeypatch.setattr(r, "_fly", lambda *a, **k: __import__("subprocess").CompletedProcess(a, 1, "", ""))
    assert r.main([]) == 2 and not f.exists()


# --- commands handed to a person must not wrap --------------------------------

HANDED_OVER = ("consent-walk", "save-token", "rotate-now", "consent-check")
WRAP_LIMIT = 70  # a terminal pane wrapped a 79 character line into two commands on 2026-10-17


def test_every_handed_over_make_line_is_short_enough_not_to_wrap():
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    for target in HANDED_OVER:
        assert re.search(rf"^{re.escape(target)}:", makefile, re.M), f"{target} is not a Makefile target"
        line = f"make -C ~/Documents/Automation\\ /paypilot {target}"
        assert len(line) <= WRAP_LIMIT, f"{len(line)} characters: {line}"


def test_a_walk_saves_its_id_and_a_bare_check_reads_it(tmp_path, monkeypatch, capsys):
    m = _walk_module()
    monkeypatch.setattr(m, "LAST_ID_FILE", tmp_path / "cfg" / "last_consent_id")
    monkeypatch.setattr(m, "TOKEN_FILE", tmp_path / "decision_token")
    (tmp_path / "decision_token").write_text("Zq9xK2mP7vL4wR8t\n")
    (tmp_path / "decision_token").chmod(0o600)
    monkeypatch.delenv("DECISION_API_TOKEN", raising=False)
    seen = []

    def fake_get(url, token=None):
        seen.append(url)
        if "/bank/consent/start" in url:
            return 200, {"authorize_url": "https://bank.invalid/v1/authorize", "state": "s"}
        if "/bank/consent/callback" in url:
            return 200, {"consent_id": "fake-abc", "state": "live"}
        return 200, {"consent_id": "fake-abc", "state": "live", "provider": "fake"}

    monkeypatch.setattr(m, "_get", fake_get)
    monkeypatch.setattr(m, "load_token", lambda path=m.TOKEN_FILE: ("Zq9xK2mP7vL4wR8t", "file"))
    monkeypatch.setattr(sys_argv(), "argv", ["consent_walk.py"])
    assert m.main() == 0
    assert m.LAST_ID_FILE.read_text().strip() == "fake-abc"
    seen.clear()
    monkeypatch.setattr(sys_argv(), "argv", ["consent_walk.py", "--check"])
    assert m.main() == 0 and seen and seen[0].endswith("/bank/consents/fake-abc")


def test_a_bare_check_with_no_saved_id_exits_2_and_calls_nothing(tmp_path, monkeypatch):
    m = _walk_module()
    monkeypatch.setattr(m, "LAST_ID_FILE", tmp_path / "last_consent_id")
    monkeypatch.setattr(m, "load_token", lambda path=m.TOKEN_FILE: ("Zq9xK2mP7vL4wR8t", "file"))
    called = []
    monkeypatch.setattr(m, "_get", lambda *a, **k: called.append(a))
    monkeypatch.setattr(sys_argv(), "argv", ["consent_walk.py", "--check"])
    assert m.main() == 2 and not called


def sys_argv():
    import sys
    return sys



def test_the_rotation_proof_blames_the_token_only_for_401_and_503():
    r = _rotate_module()
    assert r.judge(200)[0] and r.judge(404, "unknown_provider")[0] and r.judge(400)[0]
    ok, msg = r.judge(401)
    assert not ok and "disagree" in msg
    ok, msg = r.judge(503, "not_configured")
    assert not ok and "no DECISION_API_TOKEN" in msg
    assert "bearer accepted" in r.judge(404, "unknown_provider")[1]
