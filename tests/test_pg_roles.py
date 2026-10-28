"""M9: per-tenant database logins. SQL composition, validation, derivation, and the backend switch."""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

from app import pg_backend, pg_roles
from tests.test_pg_backend import FakeNative

KEY = b"k" * 40


# -- names ----------------------------------------------------------------------------------------


def test_role_name_is_prefix_plus_tenant_and_round_trips():
    assert pg_roles.role_name("acme") == "pp_t_acme"
    assert pg_roles.tenant_from_role("pp_t_acme") == "acme"
    assert pg_roles.tenant_from_role("paypilot_tenant") is None


@pytest.mark.parametrize("bad", ["", "Acme", "a-b", "a b", "1abc", "a;drop", 'a"b', "a" * 70, None, 5])
def test_an_invalid_tenant_id_never_becomes_a_role(bad):
    with pytest.raises(pg_roles.RoleError):
        pg_roles.role_name(bad)


def test_a_name_past_the_postgres_limit_is_refused_not_truncated():
    ok = "a" * (pg_roles.MAX_ROLE_LEN - len(pg_roles.PREFIX))
    assert len(pg_roles.role_name(ok)) == pg_roles.MAX_ROLE_LEN
    with pytest.raises(pg_roles.RoleError, match="too long"):
        pg_roles.role_name(ok + "a")


# -- derived passwords ----------------------------------------------------------------------------


def test_the_password_is_stable_per_tenant_and_different_between_tenants_and_keys():
    a1 = pg_roles.derive_password("alpha", KEY)
    assert a1 == pg_roles.derive_password("alpha", KEY)
    assert a1 != pg_roles.derive_password("beta", KEY)
    assert a1 != pg_roles.derive_password("alpha", b"z" * 40)
    assert re.fullmatch(r"[0-9a-f]{40}", a1)


def test_a_short_or_missing_key_is_refused():
    with pytest.raises(pg_roles.RoleError):
        pg_roles.role_key({})
    with pytest.raises(pg_roles.RoleError):
        pg_roles.role_key({pg_roles.KEY_ENV: "short"})
    assert pg_roles.role_key({pg_roles.KEY_ENV: "x" * 32}) == b"x" * 32


# -- SQL ------------------------------------------------------------------------------------------


def _verifier(tenant="alpha"):
    return pg_roles.scram_verifier(pg_roles.derive_password(tenant, KEY), salt=b"0123456789abcdef")


def test_the_scram_verifier_matches_a_known_answer():
    """Fixed vector, computed once with the scramp reference library (pg8000's dependency)."""
    expected = "SCRAM-SHA-256$4096:MDEyMzQ1Njc4OWFiY2RlZg==$WXmExmM6ExvDqFQH3pi5Cx8YnrsBJzjpgNSv9wm3nyI=:HIYDJVF/bXzaWlsNVQ+KGzREUuKXTMaFLYdR7N2Z/lk="
    assert pg_roles.scram_verifier("pw123", b"0123456789abcdef") == expected


def test_the_scram_verifier_matches_the_reference_library_when_it_is_installed():
    import base64

    scramp = pytest.importorskip("scramp")
    pw, salt = "pw123", b"0123456789abcdef"
    s, stored, server, it = scramp.ScramMechanism("SCRAM-SHA-256").make_auth_info(
        pw, iteration_count=4096, salt=salt)
    b64 = lambda b: base64.b64encode(b).decode()  # noqa: E731
    assert pg_roles.scram_verifier(pw, salt) == f"SCRAM-SHA-256${it}:{b64(s)}${b64(stored)}:{b64(server)}"


def test_provision_sql_creates_a_login_with_no_privileges_beyond_the_six_tables():
    sql = pg_roles.provision_sql("alpha", _verifier(), exists=False)
    text = "\n".join(sql)
    assert sql[0].startswith('CREATE ROLE "pp_t_alpha" WITH LOGIN PASSWORD \'SCRAM-SHA-256$')
    for must in ("NOSUPERUSER", "NOCREATEROLE", "NOBYPASSRLS", "NOCREATEDB", "NOREPLICATION"):
        assert must in sql[0]
    assert 'ALTER ROLE "pp_t_alpha" RESET ALL' in sql, "a persisted role-level setting is cleared"
    assert "REVOKE ALL ON ALL TABLES" in text
    grant = next(s for s in sql if s.startswith("GRANT SELECT"))
    for t in pg_roles.TENANT_TABLES:
        assert t in grant
    for platform in ("tenants", "api_tokens", "tenant_config", "tenancy_meta"):
        assert platform not in grant
    assert "IN ROLE" not in text and "GRANT paypilot" not in text, "must be a member of nothing"


def test_an_existing_role_is_altered_not_recreated():
    sql = pg_roles.provision_sql("alpha", _verifier(), exists=True)
    assert sql[0].startswith('ALTER ROLE "pp_t_alpha" WITH LOGIN PASSWORD')


def test_only_a_scram_verifier_is_accepted_so_no_cleartext_or_injection_gets_in():
    for bad in ("x' ; DROP ROLE postgres; --", "a" * 40, pg_roles.derive_password("alpha", KEY),
                _verifier() + "'; DROP ROLE x; --", ""):
        with pytest.raises(pg_roles.RoleError):
            pg_roles.provision_sql("alpha", bad, exists=False)


def test_deprovision_logs_out_revokes_and_drops():
    sql = pg_roles.deprovision_sql("alpha")
    assert sql[0] == 'ALTER ROLE "pp_t_alpha" NOLOGIN'
    assert any("pg_terminate_backend" in s for s in sql)
    assert any(s.startswith('DROP OWNED BY "pp_t_alpha"') for s in sql)
    assert sql[-1] == 'DROP ROLE IF EXISTS "pp_t_alpha"'


class Admin(FakeNative):
    """A fake admin connection that answers the provisioning probes."""

    def __init__(self, public_create=False, role_exists=False, member_of=0):
        super().__init__()
        self.public_create, self.role_exists, self.member_of = public_create, role_exists, member_of
        self.statements: list[str] = []

    def run(self, sql, **kw):
        self.calls.append((sql, kw))
        if "aclexplode" in sql:
            return [[1]] if self.public_create else []
        if "FROM pg_roles WHERE rolname" in sql:
            return [[1]] if self.role_exists else []
        if "pg_auth_members" in sql:
            return [[self.member_of]]
        if "has_schema_privilege" in sql:
            return []
        self.statements.append(sql)
        return None


def test_provision_never_puts_the_cleartext_password_in_any_statement():
    admin = Admin()
    pg_roles.provision(admin, "alpha", KEY)
    pw = pg_roles.derive_password("alpha", KEY)
    assert admin.statements and all(pw not in s for s in admin.statements)
    assert any("SCRAM-SHA-256$" in s for s in admin.statements)


def test_provision_refuses_when_public_can_create_in_schema_public():
    admin = Admin(public_create=True)
    with pytest.raises(pg_roles.RoleError, match="PUBLIC can CREATE"):
        pg_roles.provision(admin, "alpha", KEY)
    assert admin.statements == [], "nothing is changed on a cluster that fails the check"


def test_provision_refuses_a_leftover_role_that_is_a_member_of_another_role():
    admin = Admin(role_exists=True, member_of=1)
    with pytest.raises(pg_roles.RoleError, match="member of another role"):
        pg_roles.provision(admin, "alpha", KEY)
    assert admin.statements == []


def test_deprovision_of_a_role_that_is_already_gone_is_a_quiet_success():
    admin = Admin(role_exists=False)
    assert pg_roles.deprovision(admin, "alpha") == "pp_t_alpha"
    assert admin.statements == [], "no statement may run, so a repeat sync cannot abort"


def test_deprovision_binds_the_name_for_the_session_kill():
    admin = Admin(role_exists=True)
    pg_roles.deprovision(admin, "alpha")
    kill = next(c for c in admin.calls if "pg_terminate_backend" in c[0])
    assert kill[1] == {"n": "pp_t_alpha"}


def test_sync_carries_on_past_a_failing_tenant_and_exits_nonzero(monkeypatch, capsys):
    import app.store as store_mod

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    import pg_roles as cli

    from app import decision_audit

    admin = Admin(role_exists=True)
    monkeypatch.setattr(decision_audit, "open_connection", lambda env: admin)
    monkeypatch.setattr(pg_backend, "_role_env", lambda role: {})
    monkeypatch.setenv(pg_roles.KEY_ENV, "k" * 40)

    class FakeDb:
        def __init__(self, **kw):
            pass

        def close(self):
            pass

    class FakePlatform:
        def __init__(self, db):
            pass

        def list_tenants(self):
            too_long = "t" + "x" * 62
            return [{"id": "acme", "status": "offboarded"}, {"id": too_long, "status": "active"},
                    {"id": "zeta", "status": "offboarded"}]

    monkeypatch.setattr(pg_backend, "PgDatabase", FakeDb)
    monkeypatch.setattr(store_mod, "Platform", FakePlatform)
    assert cli.main(["sync"]) == 2
    out = capsys.readouterr().out
    assert "dropped     acme" in out and "dropped     zeta" in out, "tenants after the bad one still drop"
    assert "REFUSED" in out and "1 failed" in out


# -- the schema -----------------------------------------------------------------------------------


def _schema_code():
    text = pg_backend.SCHEMA_PATH.read_text()
    return "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("--"))


def test_the_policy_itself_calls_the_login_function_and_never_the_setting():
    code = _schema_code()
    fmt = re.search(r"'CREATE POLICY %I ON %I TO PUBLIC '(.*?)t \|\| '_tenant_isolation'", code, re.S)
    assert fmt, "the tenant policy format string was not found"
    assert "tenant_id = pp_current_tenant()" in fmt.group(1)
    assert "current_setting" not in fmt.group(1) and "paypilot.tenant_id" not in fmt.group(1)


def test_the_function_pins_its_search_path_and_qualifies_what_it_calls():
    code = _schema_code()
    fn = code.split("CREATE OR REPLACE FUNCTION pp_current_tenant()")[1].split("$f$;")[0]
    assert "SET search_path = pg_catalog, pg_temp" in fn
    for name in ("left(", "substr(", "current_setting("):
        assert f"pg_catalog.{name}" in fn, f"{name} must be schema-qualified"
    assert "OPERATOR(pg_catalog.=)" in fn
    assert "pg_catalog.nullif" not in fn.lower(), "NULLIF is syntax, not a pg_catalog function"
    branch = fn.split("pg_catalog.left(current_user, 5)")[1].split("WHEN current_user")[0]
    assert "current_setting" not in branch, "a pp_t_ login never consults the setting"


# -- the backend switch ---------------------------------------------------------------------------


def test_in_roles_mode_each_tenant_connects_as_itself_and_sets_no_setting():
    calls: list[tuple] = []

    def connect(role, tenant=None):
        calls.append((role, tenant))
        return FakeNative()

    db = pg_backend.PgDatabase(connect=connect, tenant_roles=True)
    c = db.conn_for("alpha")
    db.conn_for("beta")
    assert calls == [("platform", None), ("tenant", "alpha"), ("tenant", "beta")]
    c.execute("UPDATE failures SET state = ?", ("x",))
    assert not any("set_config" in s for s in c._native.sql())
    db.close()


def test_the_default_mode_is_unchanged_and_still_sets_the_setting():
    db = pg_backend.PgDatabase(connect=lambda role: FakeNative(), tenant_roles=False)
    c = db.conn_for("alpha")
    c.execute("UPDATE failures SET state = ?", ("x",))
    assert any("set_config" in s for s in c._native.sql())
    db.close()


def test_the_tenant_login_env_uses_the_role_name_and_a_derived_password(monkeypatch):
    monkeypatch.setenv("PGHOST", "h")
    monkeypatch.setenv("PGDATABASE", "d")
    monkeypatch.setenv(pg_roles.KEY_ENV, "k" * 40)
    env = pg_backend._tenant_login_env("alpha")
    assert env["PGUSER"] == "pp_t_alpha"
    assert env["PGPASSWORD"] == pg_roles.derive_password("alpha", b"k" * 40)


def test_a_missing_login_fails_closed_and_leaks_nothing(monkeypatch):
    monkeypatch.delenv("PGHOST", raising=False)
    monkeypatch.setenv(pg_roles.KEY_ENV, "k" * 40)
    with pytest.raises(RuntimeError) as ei:
        pg_backend._tenant_login_env("alpha")
    text = str(ei.value)
    assert "PGHOST" in text
    assert pg_roles.derive_password("alpha", b"k" * 40) not in text and "k" * 40 not in text


def test_a_failed_tenant_login_is_remembered_briefly_and_connects_outside_the_lock():
    attempts: list[int] = []
    holder: dict = {}

    def connect(role, tenant=None):
        attempts.append(1)
        assert not holder["db"]._guard.locked(), "a login must not hold the database-wide lock"
        raise RuntimeError("password authentication failed")

    db = pg_backend.PgDatabase(connect=lambda role, tenant=None: FakeNative(), tenant_roles=True)
    holder["db"] = db
    db._connect = connect
    with pytest.raises(RuntimeError):
        db.conn_for("alpha")
    with pytest.raises(RuntimeError, match="unavailable"):
        db.conn_for("alpha")  # inside the cooldown: no second network attempt
    assert len(attempts) == 1
    db._failed["alpha"] -= db.FAIL_COOLDOWN + 1
    with pytest.raises(RuntimeError, match="authentication"):
        db.conn_for("alpha")
    assert len(attempts) == 2


def test_the_config_merge_lock_is_bounded_and_keyed_per_connection():
    n1, n2 = FakeNative(), FakeNative()
    pg_backend.PgConnection(n1, tenant_id=None).execute("BEGIN IMMEDIATE")
    pg_backend.PgConnection(n2, tenant_id="alpha").execute("BEGIN IMMEDIATE")
    for n, label in ((n1, "platform"), (n2, "alpha")):
        sqls = n.sql()
        assert sqls.index("SET LOCAL lock_timeout = '10s'") < next(
            i for i, q in enumerate(sqls) if "pg_advisory_xact_lock" in q)
        key = next(c[1]["k"] for c in n.calls if "pg_advisory_xact_lock" in c[0])
        assert key.endswith(f":{label}")


def test_the_cli_refuses_with_no_arguments_and_cannot_run_without_admin_settings(monkeypatch, capsys):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    import pg_roles as cli

    assert cli.main([]) == 1
    for k in ("PAYPILOT_PG_ADMIN_USER", "PAYPILOT_PG_ADMIN_PASSWORD", "PGHOST", "PGDATABASE"):
        monkeypatch.delenv(k, raising=False)
    assert cli.main(["provision", "alpha"]) == 2
    assert "CANNOT RUN" in capsys.readouterr().out


# -- hardening the app host and the shared login -------------------------------------------------------


def test_roles_mode_refuses_to_start_when_admin_or_shared_credentials_are_present(monkeypatch):
    for name in pg_roles.FORBIDDEN_APP_ENV:
        monkeypatch.delenv(name, raising=False)
    pg_roles.assert_app_host_is_clean()  # clean host passes
    for name in pg_roles.FORBIDDEN_APP_ENV:
        monkeypatch.setenv(name, "secret-value-123")
        with pytest.raises(pg_roles.RoleError) as ei:
            pg_roles.assert_app_host_is_clean()
        assert name in str(ei.value) and "secret-value-123" not in str(ei.value)
        monkeypatch.delenv(name)


def test_a_real_roles_mode_database_checks_the_host_but_a_fake_driver_test_does_not(monkeypatch):
    monkeypatch.setenv("PAYPILOT_PG_ADMIN_USER", "admin")
    monkeypatch.setattr(pg_backend, "_default_connect", lambda role, tenant=None: FakeNative())
    with pytest.raises(pg_roles.RoleError, match="PAYPILOT_PG_ADMIN_USER"):
        pg_backend.PgDatabase(tenant_roles=True)
    pg_backend.PgDatabase(connect=lambda role, tenant=None: FakeNative(), tenant_roles=True).close()
    monkeypatch.delenv("PAYPILOT_PG_ADMIN_USER")
    db = pg_backend.PgDatabase(tenant_roles=True)  # clean host starts
    db.close()


def test_the_default_shared_mode_does_not_apply_the_roles_mode_host_check(monkeypatch):
    monkeypatch.setenv("PAYPILOT_PG_TENANT_USER", "paypilot_tenant")
    monkeypatch.setattr(pg_backend, "_default_connect", lambda role, tenant=None: FakeNative())
    db = pg_backend.PgDatabase(tenant_roles=False)
    db.close()


def test_harden_makes_the_shared_login_unusable_and_reports_it():
    class A(Admin):
        def __init__(self, can_login=True, exists=True):
            super().__init__()
            self.can_login, self.exists = can_login, exists

        def run(self, sql, **kw):
            if "SELECT rolcanlogin" in sql:
                self.calls.append((sql, kw))
                return [[self.can_login]]
            if "FROM pg_roles WHERE rolname" in sql:
                self.calls.append((sql, kw))
                return [[1]] if self.exists else []
            if sql.startswith("ALTER ROLE"):
                self.can_login = False
            return super().run(sql, **kw)

    a = A()
    assert pg_roles.harden(a) is True
    assert any(c[0] == 'ALTER ROLE "paypilot_tenant" NOLOGIN' for c in a.calls)
    assert pg_roles.harden(a) is True, "idempotent"
    assert pg_roles.harden(A(exists=False)) is True, "no shared role is as good as an unusable one"

    class Stuck(A):
        def run(self, sql, **kw):
            if sql.startswith("ALTER ROLE"):
                self.calls.append((sql, kw))
                return None  # the ALTER did not take
            return super().run(sql, **kw)

    assert pg_roles.harden(Stuck()) is False, "reports failure instead of claiming success"


def test_the_cli_has_a_harden_command_and_sync_runs_it():
    text = (Path(__file__).resolve().parents[1] / "scripts" / "pg_roles.py").read_text()
    assert '"harden"' in text and "pg_roles.harden(admin)" in text
    assert text.count("pg_roles.harden(admin)") >= 2, "both the command and sync call it"
