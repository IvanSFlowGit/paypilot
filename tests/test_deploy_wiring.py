"""The migration is wired into the container start, and /health is a readiness check.

Before this, nothing in the repository ran the tenancy migration, while a push to
main deploys automatically. The first store call on an unmigrated volume raised
and every store route returned 500, yet /health did not touch the store, so the
deploy was reported healthy. These tests pin the three pieces that close that:
the start script, the Dockerfile that calls it, and the readiness check.
"""

from __future__ import annotations

import stat
import subprocess
import tomllib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import api
from app.store import Store, reset_store
from tests.test_tenancy_migration import _build_legacy

ROOT = Path(__file__).resolve().parents[1]
START = ROOT / "scripts" / "start.sh"


def _stub(bin_dir: Path, name: str, log: Path, exit_code: int) -> None:
    path = bin_dir / name
    path.write_text(f'#!/bin/sh\necho "{name} $*" >> "{log}"\nexit {exit_code}\n')
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


def _run_start(tmp_path: Path, migrate_exit: int) -> tuple[int, list[str]]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "calls.log"
    _stub(bin_dir, "python", log, migrate_exit)
    _stub(bin_dir, "uvicorn", log, 0)
    env = {"PATH": f"{bin_dir}:/usr/bin:/bin", "HOME": str(tmp_path)}
    done = subprocess.run(["/bin/sh", str(START)], env=env, capture_output=True, text=True)
    calls = log.read_text().splitlines() if log.exists() else []
    return done.returncode, calls


def test_the_start_script_migrates_then_serves(tmp_path):
    code, calls = _run_start(tmp_path, migrate_exit=0)
    assert code == 0
    assert len(calls) == 2
    assert calls[0] == "python -m app.migrate_tenancy --allow-create"
    assert calls[1] == "uvicorn app.api:app --host 0.0.0.0 --port 8000"
    internal = tomllib.loads((ROOT / "fly.toml").read_text())["http_service"]["internal_port"]
    assert internal == 8000, "start.sh serves 8000; fly.toml must route to it"


@pytest.mark.parametrize("migrate_exit", [1, 2])
def test_a_refused_or_failed_migration_never_reaches_uvicorn(tmp_path, migrate_exit):
    """Exit 1 is a refusal and exit 2 is could-not-run. Neither may serve."""
    code, calls = _run_start(tmp_path, migrate_exit=migrate_exit)
    assert code == migrate_exit
    assert calls == ["python -m app.migrate_tenancy --allow-create"]


def test_the_start_script_has_no_error_swallowing():
    text = START.read_text()
    code_lines = [ln for ln in text.splitlines() if ln.strip() and not ln.strip().startswith("#")]
    assert "set -eu" in code_lines, "the comment mentioning set -eu does not count"
    assert not any("|| true" in ln or "|| :" in ln for ln in code_lines)


def test_the_dockerfile_starts_through_the_script():
    docker = (ROOT / "Dockerfile").read_text()
    cmd_lines = [ln for ln in docker.splitlines() if ln.startswith("CMD")]
    assert cmd_lines == ['CMD ["sh", "scripts/start.sh"]']
    assert "COPY scripts/ ./scripts/" in docker


def test_fly_names_the_demo_tenant_for_both_variables():
    env = tomllib.loads((ROOT / "fly.toml").read_text())["env"]
    assert env["PAYPILOT_TENANT"] == "demo"
    assert env["PAYPILOT_LEGACY_TENANT"] == "demo"
    assert env["PAYPILOT_DB_PATH"].startswith("/data/")


def test_the_migration_is_not_the_release_command():
    """A release machine may not mount the volume; see scripts/start.sh."""
    deploy = tomllib.loads((ROOT / "fly.toml").read_text())["deploy"]
    assert "migrate_tenancy" not in deploy["release_command"]


# -- /health is a readiness check -------------------------------------------------


def test_health_is_green_on_a_current_ledger(isolated_store):
    r = TestClient(api.app).get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_health_is_503_on_a_legacy_file_and_names_only_the_class(tmp_path, monkeypatch):
    legacy = tmp_path / "legacy.db"
    _build_legacy(legacy)
    monkeypatch.setenv("PAYPILOT_DB_PATH", str(legacy))
    monkeypatch.setenv("PAYPILOT_COMMIT", "abc1234")
    reset_store(None)
    try:
        r = TestClient(api.app).get("/health")
    finally:
        reset_store(None)
    assert r.status_code == 503
    body = r.json()
    assert body["status"] == "unavailable"
    assert body["commit"] == "abc1234"
    assert body["reason"] == "LegacySchemaError"
    assert str(legacy) not in r.text


def test_health_is_503_when_the_ledger_belongs_to_another_tenant(tmp_path, monkeypatch):
    path = tmp_path / "owned.db"
    monkeypatch.setenv("PAYPILOT_TENANT", "demo")
    Store(path).close()
    monkeypatch.setenv("PAYPILOT_TENANT", "default")
    monkeypatch.setenv("PAYPILOT_DB_PATH", str(path))
    reset_store(None)
    try:
        r = TestClient(api.app).get("/health")
    finally:
        reset_store(None)
    assert r.status_code == 503
    assert r.json()["reason"] == "TenantMismatchError"


def test_health_does_not_open_the_ambient_store_in_multi_mode(monkeypatch):
    monkeypatch.setenv("PAYPILOT_TENANCY", "multi")
    r = TestClient(api.app).get("/health")
    assert r.status_code == 200


def test_the_start_script_is_a_posix_sh_script_without_crlf():
    raw = START.read_bytes()
    assert raw.startswith(b"#!/bin/sh\n")
    assert b"\r" not in raw, "CRLF would make /bin/sh fail on the first line"


def test_health_is_503_not_500_on_an_invalid_tenancy_value(monkeypatch):
    monkeypatch.setenv("PAYPILOT_TENANCY", "Multi-x")
    r = TestClient(api.app).get("/health")
    assert r.status_code == 503
    assert r.json()["status"] == "unavailable"
