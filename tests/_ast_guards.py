"""Shared AST helpers for the tenant guards (not a test module).

Callees are resolved through import aliases and simple name bindings, and attribute calls are
matched by their attribute name, so ``from m import f as g; g()``, ``g = f; g()``, ``g = mod.f;
g()``, ``getattr(mod, "f")()`` and ``mod.f()`` all count as calls of ``f``. Calls outside any
function (module and class bodies) are attributed to ``<module>`` rather than skipped. A literal
``None`` in the config position counts as no config.
"""

from __future__ import annotations

import ast
from collections import Counter


def _getattr_name(node: ast.AST) -> str | None:
    """``getattr(mod, "f")`` -> ``f``."""
    if (
        isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "getattr"
        and len(node.args) >= 2 and isinstance(node.args[1], ast.Constant)
        and isinstance(node.args[1].value, str)
    ):
        return node.args[1].value
    return None


def _bound_name(value: ast.AST) -> str | None:
    """The name a right-hand side stands for: ``f``, ``mod.f`` or ``getattr(mod, "f")``."""
    if isinstance(value, ast.Name):
        return value.id
    if isinstance(value, ast.Attribute):
        return value.attr
    return _getattr_name(value)


def aliases(tree: ast.AST) -> dict[str, str]:
    """Local name -> the name it stands for (``import x as y``, ``y = x``), resolved to a fixpoint."""
    mapping: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for item in node.names:
                if item.asname:
                    mapping[item.asname] = item.name
        elif (
            isinstance(node, ast.Assign) and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
        ):
            bound = _bound_name(node.value)
            if bound:
                mapping[node.targets[0].id] = bound
    for _ in range(4):  # chains of aliases; four is more than any real module has
        mapping = {k: mapping.get(v, v) for k, v in mapping.items()}
    return mapping


def callee(node: ast.Call, alias_map: dict[str, str] | None = None) -> str | None:
    if isinstance(node.func, ast.Name):
        name = node.func.id
    elif isinstance(node.func, ast.Attribute):
        name = node.func.attr
    else:
        name = _getattr_name(node.func)  # getattr(mod, "f")(...)
        if name is None:
            return None
    return (alias_map or {}).get(name, name)


def scoped_calls(tree: ast.AST):
    """Every call with the name of its innermost enclosing function, or ``<module>``."""
    stack: list[str] = []

    def visit(node: ast.AST):
        is_fn = isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        if is_fn:
            stack.append(node.name)
        if isinstance(node, ast.Call):
            yield (stack[-1] if stack else "<module>"), node
        for child in ast.iter_child_nodes(node):
            yield from visit(child)
        if is_fn:
            stack.pop()

    yield from visit(tree)


def passes_config(node: ast.Call, position: int | None) -> bool:
    """True if the call supplies a config. A literal ``None`` is not a config."""
    value = next((k.value for k in node.keywords if k.arg == "config"), None)
    if value is None and position is not None and len(node.args) > position:
        value = node.args[position]
    return value is not None and not (isinstance(value, ast.Constant) and value.value is None)


def current_config_sites_in(src: str, rel: str) -> Counter:
    """Calls of current_config() by (file, enclosing scope), through aliases and attributes."""
    tree = ast.parse(src)
    alias_map = aliases(tree)
    counts: Counter = Counter()
    for scope, node in scoped_calls(tree):
        if callee(node, alias_map) == "current_config":
            counts[(rel, scope)] += 1
    return counts
