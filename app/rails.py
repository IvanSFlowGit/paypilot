"""One registry for the return-code rails, so a caller asks rather than imports.

WHY THIS EXISTS, AND WHAT IT REFUSES TO DO.

There are three rails and they do not have the same shape. ACH takes a code and
nothing else and has no abstain row at all. SEPA takes a code and nothing else
and does abstain. Bacs takes a code AND a report type, and that second argument
has no default on purpose: ARUDD, ADDACS and AUDDIS share a one-character
symbol space, so an ADDACS ``0`` answered from the ARUDD table returns
"goes again" for an instruction the payer cancelled.

A registry is the obvious place for that property to die. Giving every rail a
uniform ``lookup(rail, code)`` signature means either defaulting Bacs's report,
which reintroduces exactly the defect the Bacs module refuses, or forcing a
dead argument on the two rails that have no use for one. So this registry does
neither. It declares, per rail, which extra context is REQUIRED, and it refuses
by name when a caller omits it.

THE CONTROL IS THE INTERESTING PART AND IT RUNS ON EVERY IMPORT.

``_validate_registry`` compares each declared ``required_context`` against the
non-defaulted parameters of that rail's real ``lookup_return_code``, read with
``inspect``. So if somebody later writes ``report: str = "ARUDD"`` in
``bacs_return_map``, this module stops importing. That is a second assertion at
a different layer from the Bacs test that pins the same property: the test
fails in the Bacs suite, this fails in anything that touches the registry.

It also checks the reverse direction, which is the one that rots quietly: a new
required parameter added to a rail and not declared here would otherwise make
every registry call fail at runtime with a TypeError from somebody else's
module. Declared-but-absent and absent-but-declared both refuse.

WHAT THIS MODULE DOES NOT DO.

It does not invent a common Decision shape. The three dataclasses carry
genuinely different fields - Bacs has ``report``, ``meaning`` and
``collides_in``, SEPA has ``scheme_scope`` and ``suggested_creditor_action``,
ACH has neither set - and flattening them would drop the fields that make each
rail's answer correct. ``lookup`` returns the rail's own object and the rail is
named on the ``Rail`` record so a caller can tell which shape it holds.

It does not guess a rail from a code. Codes collide ACROSS rails as well as
across Bacs reports: ``R01`` is ACH, ``0`` is Bacs, ``AC04`` is SEPA, and a
bare ``1`` is Bacs while a bare ``2`` is also Bacs. Any function that took a
code and returned a rail would be guessing, so there is not one.
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass
from types import ModuleType
from typing import Any

from app import ach_return_map, bacs_return_map, sepa_return_map


class RailError(RuntimeError):
    """A rail was asked for that does not exist, or asked wrongly."""


class RegistryError(RuntimeError):
    """The registry's own description of a rail no longer matches the rail."""


@dataclass(frozen=True)
class Rail:
    """What the registry knows about one rail, declared rather than inferred."""

    name: str
    module: ModuleType
    required_context: tuple[str, ...]
    abstains: bool
    scheme: str


# Declared, never derived from the module. The import-time control below is
# what keeps the declaration honest; deriving it would make the control
# vacuous, because a derived value agrees with its source by construction.
_RAILS: tuple[Rail, ...] = (
    Rail(
        name="ach",
        module=ach_return_map,
        required_context=(),
        abstains=False,
        scheme="Nacha ACH",
    ),
    Rail(
        name="bacs",
        module=bacs_return_map,
        required_context=("report",),
        abstains=True,
        scheme="Bacs Direct Debit",
    ),
    Rail(
        name="sepa",
        module=sepa_return_map,
        required_context=(),
        abstains=True,
        scheme="SEPA Direct Debit",
    ),
)

_REQUIRED_NAMES = (
    "lookup_return_code",
    "explain",
    "list_decisions",
    "normalise",
    "TABLE_PATH",
    "DEFAULT_KEY",
    "Decision",
    "TableError",
)


def _extra_required_params(fn: Any) -> tuple[str, ...]:
    """The parameters after ``code`` that the caller MUST supply.

    A parameter with a default is not required, which is the whole point: this
    is how a default silently appearing on Bacs's ``report`` gets caught.
    """
    params = list(inspect.signature(fn).parameters.values())
    if not params or params[0].name != "code":
        raise RegistryError(
            f"{fn.__module__}.{fn.__name__} does not take code as its first "
            "parameter, so the registry cannot call it positionally."
        )
    return tuple(
        p.name
        for p in params[1:]
        if p.default is inspect.Parameter.empty
        and p.kind
        in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
    )


def _validate_registry(rails: tuple[Rail, ...]) -> None:
    """Refuse at import when a declaration no longer describes its rail."""
    if not rails:
        raise RegistryError("the registry is empty")

    names = [r.name for r in rails]
    if len(set(names)) != len(names):
        raise RegistryError(f"duplicate rail name in {names}")
    if names != sorted(names):
        raise RegistryError(f"rails are not in sorted order: {names}")

    for rail in rails:
        for attr in _REQUIRED_NAMES:
            if not hasattr(rail.module, attr):
                raise RegistryError(
                    f"rail {rail.name} ({rail.module.__name__}) has no {attr}, "
                    "so the registry cannot treat it as a rail."
                )

        if not rail.module.TABLE_PATH.is_file():
            raise RegistryError(
                f"rail {rail.name} names a table that is not on disk: "
                f"{rail.module.TABLE_PATH}"
            )

        for fn_name in ("lookup_return_code", "explain"):
            found = _extra_required_params(getattr(rail.module, fn_name))
            if found != rail.required_context:
                raise RegistryError(
                    f"rail {rail.name}.{fn_name} requires {found or '()'} after "
                    f"code, and the registry declares "
                    f"{rail.required_context or '()'}. One of the two has "
                    "moved. A required argument that quietly gains a default "
                    "is the Bacs report defect, and a new required argument "
                    "nobody declared makes every registry call raise a "
                    "TypeError from inside the rail."
                )

        declared_abstains = bool(getattr(rail.module, "ABSTAIN_ROWS", frozenset()))
        if declared_abstains != rail.abstains:
            raise RegistryError(
                f"rail {rail.name} has ABSTAIN_ROWS "
                f"{'non-empty' if declared_abstains else 'empty or absent'} "
                f"and the registry declares abstains={rail.abstains}."
            )

    # Positive control, carried on every import rather than fired once. A
    # registry that cannot answer for a code known to be in the table is
    # broken, and an empty registry would otherwise pass every check above.
    controls = {"ach": ("R01", {}), "bacs": ("0", {"report": "ARUDD"}),
                "sepa": ("AC04", {})}
    for rail in rails:
        if rail.name not in controls:
            raise RegistryError(
                f"rail {rail.name} has no positive control, so nothing proves "
                "the registry can reach its table."
            )
        code, context = controls[rail.name]
        decision = rail.module.lookup_return_code(code, **context)
        if not decision.matched:
            raise RegistryError(
                f"control {code} on rail {rail.name} fell through to the "
                "default row, so the table is not the one this control was "
                "written against."
            )


_validate_registry(_RAILS)

_BY_NAME = {r.name: r for r in _RAILS}


def rails() -> tuple[str, ...]:
    """Every rail name, sorted, so a caller can enumerate rather than guess."""
    return tuple(r.name for r in _RAILS)


def get(rail: object) -> Rail:
    """The Rail record, or a refusal that names what does exist."""
    if not isinstance(rail, str):
        raise RailError(
            f"rail must be a string, got {type(rail).__name__}. Known rails: "
            f"{', '.join(rails())}."
        )
    key = rail.strip().lower()
    if key not in _BY_NAME:
        raise RailError(
            f"no rail named {rail!r}. Known rails: {', '.join(rails())}."
        )
    return _BY_NAME[key]


def _call(rail: object, fn_name: str, code: object, context: dict[str, Any]) -> Any:
    record = get(rail)
    missing = tuple(k for k in record.required_context if k not in context)
    if missing:
        raise RailError(
            f"rail {record.name} requires {', '.join(missing)} and none was "
            "given. The registry does not pick one: on Bacs the same symbol "
            "means different things in different reports, so a guessed report "
            "returns a confident wrong answer."
        )
    unexpected = tuple(k for k in context if k not in record.required_context)
    if unexpected:
        raise RailError(
            f"rail {record.name} takes no {', '.join(unexpected)}. It would be "
            "ignored, and an ignored argument reads as an applied one."
        )
    return getattr(record.module, fn_name)(code, **context)


def lookup(rail: object, code: object, **context: Any) -> Any:
    """One decision from one rail, in that rail's own Decision shape."""
    return _call(rail, "lookup_return_code", code, context)


def explain(rail: object, code: object, **context: Any) -> Any:
    """The same decision rendered for a person, in that rail's own shape."""
    return _call(rail, "explain", code, context)


def list_decisions(rail: object) -> Any:
    """Every decision row on one rail, with the codes that land on it."""
    return get(rail).module.list_decisions()
