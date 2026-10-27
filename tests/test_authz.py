"""The authorisation core: roles, the one permission function, and token handling.

The load-bearing properties are the negative ones: a wrong-tenant caller learns
nothing (404, identical whatever role they hold), nobody mints above themselves,
and no role or action exists that nothing checks.
"""

from __future__ import annotations

import pytest

from app import authz
from app.authz import (
    ACTIONS,
    PLATFORM_ADMIN,
    ROLES,
    TENANT_ADMIN,
    TENANT_APPROVER,
    TENANT_ROLES,
    TENANT_SERVICE,
    TENANT_VIEWER,
    Principal,
    authorize,
    can_grant,
)
from app.tenant_config import platform_config as _platform_config

A, B = "tn_aaaaaaaaaaaaaaaa", "tn_bbbbbbbbbbbbbbbb"


def who(role, tenant=A):
    return Principal(role=role, tenant_id=None if role == PLATFORM_ADMIN else tenant, token_id="t1")


# -- the table is coherent -----------------------------------------------------


def test_every_role_is_granted_at_least_one_action():
    """A role nobody checks is not a role: delete it or check it."""
    assert authz.unchecked_roles() == []


def test_the_table_names_only_real_roles_and_platform_admin_can_do_everything():
    for action, (roles, _) in ACTIONS.items():
        assert roles <= set(ROLES), action
        assert PLATFORM_ADMIN in roles, f"{action}: the platform operator must be able to do it"


def test_actions_a_tenant_must_not_touch_are_platform_only():
    for action in ("tenants.create", "tenants.list", "tenants.suspend", "tenants.resume",
                   "tenants.offboard", "tenants.config.platform_fields", "approvals.sweep_all",
                   "metrics.read"):
        assert ACTIONS[action][0] == {PLATFORM_ADMIN}, action


def test_the_viewer_can_never_decide_or_read_recipients():
    p = authz.permissions(TENANT_VIEWER)
    assert "approvals.decide" not in p and "approvals.read_recipient" not in p
    assert p == {"approvals.list", "report.read"}


def test_the_service_role_is_the_decision_api_and_nothing_else():
    assert authz.permissions(TENANT_SERVICE) == {"decide.write", "decide.read"}


# -- principals are never anonymous or static -----------------------------------


def test_a_tenant_role_needs_exactly_one_tenant():
    for role in TENANT_ROLES:
        with pytest.raises(ValueError):
            Principal(role=role, tenant_id=None, token_id="t")
        with pytest.raises(ValueError):
            Principal(role=role, tenant_id="", token_id="t")


def test_platform_admin_belongs_to_no_tenant():
    with pytest.raises(ValueError):
        Principal(role=PLATFORM_ADMIN, tenant_id=A, token_id="t")


def test_there_is_no_principal_without_a_token_id():
    with pytest.raises(ValueError):
        Principal(role=TENANT_ADMIN, tenant_id=A, token_id="")


def test_an_unknown_role_cannot_be_constructed():
    with pytest.raises(ValueError):
        Principal(role="superuser", tenant_id=A, token_id="t")


# -- authorize: the matrix -------------------------------------------------------


@pytest.mark.parametrize("role", ROLES)
@pytest.mark.parametrize("action", sorted(ACTIONS))
def test_own_tenant_follows_the_table_exactly(role, action):
    roles, tenant_scoped = ACTIONS[action]
    tenant = A if tenant_scoped else None
    v = authorize(who(role), action, tenant)
    if role in roles:
        assert v.allowed and v.status == 200
    else:
        assert not v.allowed and v.status == 403 and v.reason == "role_not_permitted"


def test_unauthenticated_is_401():
    v = authorize(None, "report.read", A)
    assert (v.allowed, v.status, v.reason) == (False, 401, "unauthenticated")


def test_an_unknown_action_is_refused_not_allowed_by_default():
    v = authorize(who(PLATFORM_ADMIN), "tenants.delete_everything", A)
    assert not v.allowed and v.reason == "unknown_action"


@pytest.mark.parametrize("role", TENANT_ROLES)
@pytest.mark.parametrize("action", sorted(a for a, (_, scoped) in ACTIONS.items() if scoped))
def test_another_tenants_resource_is_404_whatever_the_role_and_action(role, action):
    v = authorize(who(role, A), action, B)
    assert (v.allowed, v.status, v.reason) == (False, 404, "not_found")


def test_a_wrong_tenant_caller_cannot_tell_a_permitted_action_from_a_forbidden_one():
    """If 'viewer asks to erase tenant B' answered 403 and 'admin asks to erase
    tenant B' answered 404, the status code would say whether tenant B exists."""
    forbidden = authorize(who(TENANT_VIEWER, A), "gdpr.erase", B)
    permitted = authorize(who(TENANT_ADMIN, A), "gdpr.erase", B)
    assert forbidden == permitted


def test_platform_admin_reaches_any_tenant():
    for action, (_roles, scoped) in ACTIONS.items():
        assert authorize(who(PLATFORM_ADMIN), action, B if scoped else None).allowed, action


def test_tenant_roles_never_reach_platform_wide_actions():
    for action, (_roles, scoped) in ACTIONS.items():
        if scoped:
            continue
        for role in TENANT_ROLES:
            v = authorize(who(role), action, None)
            assert not v.allowed and v.status == 403, (role, action)


def test_a_tenant_scoped_action_with_no_resolved_tenant_fails_closed():
    """A route that forgot to resolve the tenant must not be allowed by accident."""
    v = authorize(who(TENANT_ADMIN), "approvals.list", None)
    assert not v.allowed and v.reason == "tenant_required"


def test_a_platform_wide_action_naming_another_tenant_is_not_found():
    v = authorize(who(TENANT_ADMIN, A), "metrics.read", B)
    assert (v.allowed, v.status) == (False, 404)


# -- who may mint what ----------------------------------------------------------


def test_only_platform_admin_mints_platform_admin():
    assert can_grant(PLATFORM_ADMIN, PLATFORM_ADMIN)
    for r in TENANT_ROLES:
        assert not can_grant(r, PLATFORM_ADMIN)


def test_platform_admin_can_mint_every_role():
    assert all(can_grant(PLATFORM_ADMIN, r) for r in ROLES)


def test_tenant_admin_mints_every_tenant_role_and_never_above_itself():
    assert all(can_grant(TENANT_ADMIN, r) for r in TENANT_ROLES)
    assert not can_grant(TENANT_ADMIN, PLATFORM_ADMIN)


def test_containment_is_computed_not_assumed():
    assert can_grant(TENANT_APPROVER, TENANT_VIEWER)
    assert not can_grant(TENANT_VIEWER, TENANT_APPROVER)
    assert not can_grant(TENANT_SERVICE, TENANT_APPROVER)
    assert not can_grant(TENANT_APPROVER, TENANT_SERVICE)
    assert not can_grant(TENANT_VIEWER, TENANT_SERVICE)


def test_containing_a_role_is_not_the_same_as_being_allowed_to_mint():
    """can_grant is containment only. Whether the caller may mint at all is a
    separate authorize() check, and an approver may not."""
    assert can_grant(TENANT_APPROVER, TENANT_VIEWER)
    assert not authorize(who(TENANT_APPROVER), "tokens.create", A).allowed


def test_unknown_roles_grant_and_receive_nothing():
    assert not can_grant("root", TENANT_VIEWER)
    assert not can_grant(PLATFORM_ADMIN, "root")


# -- tokens ---------------------------------------------------------------------


def test_a_new_token_has_the_exact_shape_and_only_its_hash_is_returned_for_storage():
    token_id, raw, stored = authz.new_token()
    parsed = authz.parse_token(raw)
    assert parsed is not None and parsed[0] == token_id
    assert raw.startswith("pp_") and len(token_id) == 8
    assert stored != parsed[1] and len(stored) == 64
    assert parsed[1] not in stored


def test_tokens_are_unique():
    assert len({authz.new_token()[1] for _ in range(50)}) == 50


def test_a_token_cannot_trip_the_output_guard_secret_patterns():
    from app.safety import message_violations

    for _ in range(200):
        raw = authz.new_token()[1]
        assert message_violations(f"key {raw}", allow_hosts=False, config=_platform_config()) == []


@pytest.mark.parametrize("bad", [
    None, "", " ", "pp_", "pp_12345678_", "pp_1234567_" + "a" * 48,
    "pp_12345678_" + "a" * 47, "pp_12345678_" + "a" * 49, "PP_12345678_" + "a" * 48,
    "pp_1234567g_" + "a" * 48, "pp_12345678_" + "A" * 48, "pp_12345678_" + "a" * 47 + "g",
    "pp_12345678-" + "a" * 48, "xx_12345678_" + "a" * 48, "' OR 1=1 --",
    "pp_12345678_" + "a" * 48 + "\n", "pp_12345678_" + "a" * 24 + " " + "a" * 23,
])
def test_a_malformed_token_is_rejected_before_any_lookup(bad):
    assert authz.parse_token(bad) is None


def test_the_parser_does_not_trim_but_the_header_reader_does():
    _, raw, _ = authz.new_token()
    assert authz.parse_token(f" {raw}") is None and authz.parse_token(f"{raw}\n") is None
    assert authz.parse_token(authz.bearer_token(f"Bearer  {raw} ")) is not None


def test_secret_comparison_accepts_the_right_secret_and_refuses_everything_else():
    _, raw, stored = authz.new_token()
    secret = authz.parse_token(raw)[1]
    assert authz.secret_matches(secret, stored)
    assert not authz.secret_matches(secret[:-1] + ("0" if secret[-1] != "0" else "1"), stored)
    assert not authz.secret_matches("", stored)
    assert not authz.secret_matches(secret, "")
    assert not authz.secret_matches(secret, stored[:-1])


@pytest.mark.parametrize("header,expected", [
    ("Bearer abc", "abc"), ("Bearer  abc ", "abc"), ("bearer abc", None), ("Basic abc", None),
    ("Bearer", None), ("Bearer ", None), ("", None), (None, None), ("abc", None),
])
def test_bearer_extraction(header, expected):
    assert authz.bearer_token(header) == expected


@pytest.mark.parametrize("addr,masked", [
    ("owner@acme.test", "o***@acme.test"), ("a@b.test", "a***@b.test"),
    ("", "***"), ("no-at-sign", "***"), ("@x.test", "***"),
])
def test_recipient_masking_for_roles_that_may_not_see_addresses(addr, masked):
    assert authz.mask_email(addr) == masked
