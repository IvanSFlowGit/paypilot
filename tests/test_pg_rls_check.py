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


def _schema_tables_with_tenant_id() -> dict[str, bool]:
    """table name -> has a tenant_id column, read from app/pg_schema.sql (no cluster needed)."""
    import re

    sql = (Path(__file__).resolve().parents[1] / "app" / "pg_schema.sql").read_text()
    found: dict[str, bool] = {}
    for m in re.finditer(r"CREATE TABLE IF NOT EXISTS (\w+) \((.*?)\n\);", sql, re.S):
        found[m.group(1)] = bool(re.search(r"^\s*tenant_id\b", m.group(2), re.M))
    return found


def test_the_tenant_id_exemptions_are_exactly_the_platform_tables_in_the_schema():
    # The column rule exempts PLATFORM_TENANT_ID_TABLES. If someone adds a tenant_id table to the schema
    # and forgets RLS, or adds an exemption nobody needs, this fails before a cluster is involved.
    # The six RLS tables come from the DO block in the schema, not from this test's own list.
    import re

    sql = (Path(__file__).resolve().parents[1] / "app" / "pg_schema.sql").read_text()
    block = re.search(r"FOREACH t IN ARRAY ARRAY\[(.*?)\]", sql, re.S).group(1)
    rls_tables = set(re.findall(r"'(\w+)'", block))
    assert len(rls_tables) == 6
    with_tenant_id = {t for t, has in _schema_tables_with_tenant_id().items() if has}
    assert with_tenant_id - rls_tables == set(pg_rls_check.PLATFORM_TENANT_ID_TABLES)


def test_the_column_rule_keys_on_the_column_and_not_on_a_grant():
    sql = pg_rls_check.UNFORCED_TENANT_TABLES_SQL
    assert "attname = 'tenant_id'" in sql
    assert "has_table_privilege" not in sql
    assert "relforcerowsecurity" in sql and "relrowsecurity" in sql
    for t in pg_rls_check.PLATFORM_TENANT_ID_TABLES:
        assert f"'{t}'" in sql
