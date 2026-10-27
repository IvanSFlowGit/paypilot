"""Shared AST helpers for the tenant guards (not a test module).

Callees are resolved through import aliases and simple name bindings, and attribute calls are
matched by their attribute name, so ``from m import f as g; g()``, ``g = f; g()`` and ``mod.f()``
all count as calls of ``f``.
"""

from __future__ import annotations

import ast
from collections import Counter


def aliases(tree: ast.AST) -> dict[str, str]:
    """Local name -> the name it stands for (``import x as y``, ``y = x``), resolved to a fixpoint."""
    mapping: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for item in node.names:
                if item.asname:
                    mapping[item.asname] = item.name
        elif (
            isinstance(node, ast.Assign) and isinstance(node.value, ast.Name)
            and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name)
        ):
            mapping[node.targets[0].id] = node.value.id
    for _ in range(4):  # chains of aliases; four is more than any real module has
        mapping = {k: mapping.get(v, v) for k, v in mapping.items()}
    return mapping


def callee(node: ast.Call, alias_map: dict[str, str] | None = None) -> str | None:
    if isinstance(node.func, ast.Name):
        name = node.func.id
    elif isinstance(node.func, ast.Attribute):
        name = node.func.attr
    else:
        return None
    return (alias_map or {}).get(name, name)


def current_config_sites_in(src: str, rel: str) -> Counter:
    """Calls of current_config() by (file, enclosing function), through aliases and attributes."""
    tree = ast.parse(src)
    alias_map = aliases(tree)
    counts: Counter = Counter()
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for node in ast.walk(fn):
            if isinstance(node, ast.Call) and callee(node, alias_map) == "current_config":
                counts[(rel, fn.name)] += 1
    return counts
