# Copyright (c) 2026 Ivan Skachek (github.com/IvanSFlowGit)
# PolyForm Noncommercial License 1.0.0 - see LICENSE and NOTICE.
# Commercial use requires a separate licence from the author.
"""The real-cluster script must report "could not run" as exit 2, never as a pass.

These tests do not start Postgres (the sandbox this is written in cannot). They pin the two properties
the CI job leans on: strict mode looks ONLY at PAYPILOT_PG_BIN, and a missing cluster is exit 2.
The cluster itself is exercised by .github/workflows/postgres.yml.
"""

from __future__ import annotations

from pathlib import Path

from scripts import pg_rls_check


def test_strict_mode_ignores_the_fallback_candidates(tmp_path, monkeypatch):
    monkeypatch.setenv("PAYPILOT_PG_BIN_STRICT", "1")
    monkeypatch.setenv("PAYPILOT_PG_BIN", str(tmp_path / "nope"))
    # A candidate that DOES have an initdb must not rescue a strict lookup.
    real = tmp_path / "real"
    real.mkdir()
    (real / "initdb").write_text("")
    monkeypatch.setattr(pg_rls_check, "BIN_CANDIDATES", (str(real),))
    assert pg_rls_check.find_bin() is None


def test_strict_mode_finds_the_directory_it_was_given(tmp_path, monkeypatch):
    (tmp_path / "initdb").write_text("")
    monkeypatch.setenv("PAYPILOT_PG_BIN_STRICT", "1")
    monkeypatch.setenv("PAYPILOT_PG_BIN", str(tmp_path))
    assert pg_rls_check.find_bin() == Path(tmp_path)


def test_without_strict_mode_a_candidate_still_rescues_the_laptop_default(tmp_path, monkeypatch):
    # The control: strict mode is the thing that changes the answer, not a lookup that finds nothing.
    real = tmp_path / "real"
    real.mkdir()
    (real / "initdb").write_text("")
    monkeypatch.delenv("PAYPILOT_PG_BIN_STRICT", raising=False)
    monkeypatch.setenv("PAYPILOT_PG_BIN", str(tmp_path / "nope"))
    monkeypatch.setattr(pg_rls_check, "BIN_CANDIDATES", (str(real),))
    assert pg_rls_check.find_bin() == real


def test_a_missing_cluster_is_exit_two_not_a_pass(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("PAYPILOT_PG_BIN_STRICT", "1")
    monkeypatch.setenv("PAYPILOT_PG_BIN", str(tmp_path / "nope"))
    assert pg_rls_check.main() == 2
    assert "NOT a pass" in capsys.readouterr().out


def test_the_concurrency_thresholds_were_written_down_before_a_run():
    # Pinned so a later edit that loosens a bound to make a run pass shows up as a failing test with a
    # reason beside it, not as a quiet constant change. Change these only with a new claims row.
    assert (pg_rls_check.CONC_TENANTS, pg_rls_check.CONC_SESSIONS_PER_TENANT,
            pg_rls_check.CONC_TXS_PER_SESSION) == (4, 4, 25)
    assert (pg_rls_check.CONC_P95_SECONDS, pg_rls_check.CONC_MAX_SECONDS,
            pg_rls_check.CONC_WALL_SECONDS) == (1.0, 5.0, 60.0)
