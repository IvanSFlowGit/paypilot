#!/usr/bin/env python3
"""Enumerate every environment variable app/ reads, in BOTH forms.

WHY A SCRIPT AND NOT A GREP. On 2026-10-17 a grep for `getenv("NAME")` over
app/ returned 32 variables and the count was presented as the env surface. It
missed at least one, and the one it missed was the one that mattered:
app/bank/consent.py:77 does

    CONSENT_FERNET_ENV = "PAYPILOT_CONSENT_KEY"

and then reads through the constant, so the literal never appears beside
getenv. That variable is what 503s the bank consent store, and it was absent
from the README, from fly.toml and from the grep, which is three places a
reader could look and not find it.

A pattern is a claim about text and the text is longer than the pattern. This
parses instead: it resolves a module-level `NAME = "LITERAL"` assignment when
that name is handed to getenv, environ.get or environ[...], so both shapes land
in one list.

THE CONTROL IS CARRIED IN EVERY RUN. PAYPILOT_CONSENT_KEY is read only through
a constant, so if this script ever stops finding it the script has acquired the
same blind spot as the grep it replaces. It exits 2 in that case rather than
printing a shorter list, because a probe that cannot find the case it was built
for has been run rather than tested.

Run:   python3 scripts/env_surface.py [--json]
Exit:  0 enumerated, 2 the control did not fire
"""

from __future__ import annotations

import argparse
import ast
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
APP = ROOT / "app"

# Read only through a constant. If this stops being found, the probe is blind.
CONTROL = "PAYPILOT_CONSENT_KEY"

READERS = {("os", "getenv"), ("os", "environ", "get"), ("environ", "get")}


def _dotted(node: ast.AST) -> tuple[str, ...]:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return tuple(reversed(parts))


def _module_consts(tree: ast.Module) -> dict[str, str]:
    """Module-level NAME = "literal" only. A name assigned inside a function
    is not a stable env-var name and guessing at one would invent entries."""
    out: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant):
            if isinstance(node.value.value, str):
                for t in node.targets:
                    if isinstance(t, ast.Name):
                        out[t.id] = node.value.value
    return out


def _from_arg(arg: ast.AST, consts: dict[str, str]) -> str | None:
    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
        return arg.value
    if isinstance(arg, ast.Name):
        return consts.get(arg.id)
    return None


def _param_defaults(fn: ast.AST, consts: dict[str, str]) -> dict[str, str]:
    """A parameter whose DEFAULT is an env var name.

    THE THIRD FORM, and the one that defeated both the grep and the first
    version of this script. app/bank/consent.py:166 is

        def _require_fernet(env: str = CONSENT_FERNET_ENV) -> Fernet:
            raw = os.environ.get(env)

    so the name reaches the reader as a parameter default, two hops from any
    literal beside getenv. Resolved here against the module constants, which
    is why the control below is the one that catches a regression.
    """
    out: dict[str, str] = {}
    a = getattr(fn, "args", None)
    if a is None:
        return out
    positional = list(getattr(a, "posonlyargs", [])) + list(a.args)
    for arg, default in zip(positional[len(positional) - len(a.defaults):], a.defaults, strict=True):
        v = _from_arg(default, consts)
        if v:
            out[arg.arg] = v
    for arg, default in zip(a.kwonlyargs, a.kw_defaults, strict=True):
        if default is not None:
            v = _from_arg(default, consts)
            if v:
                out[arg.arg] = v
    return out


def scan(path: pathlib.Path) -> dict[str, list[int]]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    consts = _module_consts(tree)
    found: dict[str, list[int]] = {}

    def note(name: str | None, lineno: int) -> None:
        # An env var name is upper snake case. Anything else here is a dict key
        # that happens to be read the same way, and including it would pad the
        # count with things nobody sets.
        if name and name.isupper() and name.replace("_", "").isalnum():
            found.setdefault(name, []).append(lineno)

    def visit(node: ast.AST, names: dict[str, str]) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            names = {**names, **_param_defaults(node, consts)}
        if isinstance(node, ast.Call) and node.args:
            dotted = _dotted(node.func)
            if dotted[-3:] in {t[-3:] for t in READERS} or dotted[-2:] in {
                t[-2:] for t in READERS
            }:
                note(_from_arg(node.args[0], names), node.lineno)
        elif isinstance(node, ast.Subscript):
            if _dotted(node.value)[-1:] == ("environ",):
                note(_from_arg(node.slice, names), node.lineno)
        for child in ast.iter_child_nodes(node):
            visit(child, names)

    visit(tree, consts)
    return found


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    surface: dict[str, list[str]] = {}
    for f in sorted(APP.rglob("*.py")):
        if "__pycache__" in f.parts:
            continue
        for name, lines in scan(f).items():
            rel = f.relative_to(ROOT).as_posix()
            surface.setdefault(name, []).extend(
                f"{rel}:{ln}" for ln in sorted(set(lines))
            )

    if CONTROL not in surface:
        print(
            f"CONTROL DID NOT FIRE: {CONTROL} is read only through a module "
            f"constant and was not found, so this probe now has the same blind "
            f"spot as the grep it replaces. Refusing to report a count.",
            file=sys.stderr,
        )
        return 2

    if args.json:
        print(json.dumps(surface, indent=2, sort_keys=True))
    else:
        print(f"{len(surface)} environment variables read in app/")
        print(f"  control fired: {CONTROL} found via a constant\n")
        for name in sorted(surface):
            print(f"  {name:<36} {surface[name][0]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
