"""The config-less-call guards see aliases, attribute calls and the wrapper functions.

Reviewer finding 2 on M3. The AST guards matched a callee by its last identifier, so
``from app.safety import message_violations as mv; mv(x)`` and ``tenant_config.current_config()``
passed them, the wrapper functions that fall back to the ambient config (use_mock, ai_disclosure,
model_name, safe_log_fields) were not watched at all, and recovery_link still defaulted its
config to None. Each case below is a form the earlier guard did not see.
"""

from __future__ import annotations

import ast
import inspect
from collections import Counter
from pathlib import Path

from app import stripe_client
from tests._ast_guards import aliases as _aliases
from tests._ast_guards import callee as _callee
from tests._ast_guards import current_config_sites_in as _current_config_sites_in
from tests.test_tenant_rework2 import KNOWN_CURRENT_CONFIG_FALLBACKS
from tests.test_tenant_rework3 import _config_violations

ROOT = Path(__file__).resolve().parents[1]

# -- aliases and attribute calls -------------------------------------------------------------


def test_an_aliased_import_of_a_guard_function_is_still_checked():
    tree = ast.parse("from app.safety import message_violations as mv\nmv(body)\n")
    assert len(_config_violations(tree, "planted.py")) == 1


def test_a_name_bound_to_a_guard_function_is_still_checked():
    tree = ast.parse("mv = message_violations\nmv(body)\n")
    assert len(_config_violations(tree, "planted.py")) == 1


def test_an_aliased_guard_call_that_names_its_config_is_not_flagged():
    tree = ast.parse("from app.safety import message_violations as mv\nmv(body, config=cfg)\n")
    assert _config_violations(tree, "planted.py") == []


def test_current_config_called_through_the_module_is_counted():
    src = "import app.tenant_config as tc\ndef f():\n    return tc.current_config()\n"
    assert _current_config_sites_in(src, "planted.py") == Counter({("planted.py", "f"): 1})


def test_current_config_imported_under_another_name_is_counted():
    src = "from app.tenant_config import current_config as cc\ndef f():\n    return cc()\n"
    assert _current_config_sites_in(src, "planted.py") == Counter({("planted.py", "f"): 1})


def test_the_real_app_count_is_unchanged_by_the_wider_scan():
    """Widening the scan must not silently grow the pinned set: today's code has no hidden ones."""
    total: Counter = Counter()
    for path in sorted((ROOT / "app").rglob("*.py")):
        total += _current_config_sites_in(
            path.read_text(encoding="utf-8"), path.relative_to(ROOT).as_posix()
        )
    assert total == KNOWN_CURRENT_CONFIG_FALLBACKS


# -- the wrapper functions that fall back to the ambient config -------------------------------

#: callee -> position of its config argument. A call without it falls back to current_config().
CONFIGLESS_WRAPPERS = {"use_mock": 0, "ai_disclosure": 0, "model_name": 0, "safe_log_fields": 1}

#: Every call of a wrapper that passes no config, by (module, function, wrapper). Each is a route
#: or a logger that is single-mode by design and fails closed in multi mode. Pinned, so a new one
#: fails until somebody decides it.
KNOWN_CONFIGLESS_WRAPPER_CALLS = Counter({
    ("app/api.py", "chargebee_webhook", "ai_disclosure"): 1,
    ("app/api.py", "config", "model_name"): 1,
    ("app/api.py", "config", "use_mock"): 1,
    ("app/api.py", "metrics", "use_mock"): 1,
    ("app/api.py", "payment_failed", "ai_disclosure"): 1,
    ("app/api.py", "payment_failed_batch", "ai_disclosure"): 1,
    ("app/api.py", "recharge_webhook", "ai_disclosure"): 1,
    ("app/audit.py", "record", "safe_log_fields"): 1,
})


def _wrapper_sites(src: str, rel: str) -> Counter:
    tree = ast.parse(src)
    aliases = _aliases(tree)
    counts: Counter = Counter()
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for node in ast.walk(fn):
            if not isinstance(node, ast.Call):
                continue
            name = _callee(node, aliases)
            if name not in CONFIGLESS_WRAPPERS:
                continue
            has_config = any(k.arg == "config" for k in node.keywords) or (
                len(node.args) > CONFIGLESS_WRAPPERS[name]
            )
            if not has_config:
                counts[(rel, fn.name, name)] += 1
    return counts


def test_a_new_config_less_wrapper_call_is_flagged_and_a_configured_one_is_not():
    planted = (
        "from app.nodes import use_mock as um\n"
        "def a():\n    return um()\n"
        "def b(c):\n    return um(c)\n"
        "def d():\n    return ai_disclosure()\n"
        "def e():\n    return safe_log_fields({})\n"
        "def f(cfg):\n    return safe_log_fields({}, cfg)\n"
    )
    assert _wrapper_sites(planted, "p.py") == Counter({
        ("p.py", "a", "use_mock"): 1,
        ("p.py", "d", "ai_disclosure"): 1,
        ("p.py", "e", "safe_log_fields"): 1,
    })


def test_the_config_less_wrapper_calls_are_exactly_the_pinned_set():
    observed: Counter = Counter()
    for path in sorted((ROOT / "app").rglob("*.py")):
        observed += _wrapper_sites(path.read_text(encoding="utf-8"), path.relative_to(ROOT).as_posix())
    added = {k: v for k, v in (observed - KNOWN_CONFIGLESS_WRAPPER_CALLS).items()}
    gone = {k: v for k, v in (KNOWN_CONFIGLESS_WRAPPER_CALLS - observed).items()}
    assert added == {}, f"a new config-less wrapper call: pass the config or pin it: {added}"
    assert gone == {}, f"a pinned config-less wrapper call is gone: shrink the set: {gone}"


# -- the link helpers cannot be called without a tenant ---------------------------------------


def test_recovery_link_and_its_validator_require_a_config():
    for fn in (stripe_client.recovery_link, stripe_client._is_valid_recovery_link):
        param = inspect.signature(fn).parameters["config"]
        assert param.default is inspect.Parameter.empty, f"{fn.__name__} defaults its config"
