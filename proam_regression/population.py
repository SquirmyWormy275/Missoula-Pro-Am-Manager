"""
O5: the real-data expectation guard.

A module-level constant that names ids or rows from the production mirror is
a CLAIM about a population, and the suite's history shows what happens when
such a claim is quietly a sample: the original SMS defect test named two of
the three masked pros, and a first-write-wins mutant walked through the gap
(c29). Six occurrences of the same shape are catalogued in the audit backlog
section E.

This module gives every such constant a place to declare the query that
measures it. test_population_guards.py executes every registered claim
against a fresh production clone and fails, loudly and diagnostically, when
the constant is a sample, a superset, or simply wrong.

Usage, next to the constant it guards:

    from population import Claim, register

    SMS_MASKED_PROS = {29: "...", 33: "...", 37: "..."}
    register(Claim(
        name="SMS_MASKED_PROS is the whole collision population of flight 7",
        claimed=SMS_MASKED_PROS,
        sql=...,                # measures the population
        params={"t": TID},
        shape=lambda rows: {int(r[0]): r[1] for r in rows},
    ))

Modes:
    equals  (default) - shape(rows) must equal `claimed` exactly.
    member            - `claimed` must be an element of shape(rows), and the
                        measured population must not be empty.

The check itself lives in verify() so the guard's own strictness is testable:
test_population_guards.py carries meta-tests that feed verify() deliberately
sampled and invented claims and require it to throw. A future edit that
quietly weakens equality to a subset check dies there, not in production.
"""

from dataclasses import dataclass, field
from typing import Any, Callable

CLAIMS: list = []


class PopulationError(AssertionError):
    """A registered constant disagrees with the measured population."""


@dataclass(frozen=True)
class Claim:
    name: str
    claimed: Any
    sql: str
    params: dict = field(default_factory=dict)
    shape: Callable = staticmethod(lambda rows: rows)
    mode: str = "equals"


def register(claim: Claim) -> Claim:
    CLAIMS.append(claim)
    return claim


def _diagnose(claimed, measured) -> str:
    """Human-readable statement of HOW the claim is wrong, because 'assert
    a == b' on two populations hides the one fact that matters: whether the
    constant is a convenience sample."""
    try:
        c_items = set(claimed.items()) if isinstance(claimed, dict) else set(claimed)
        m_items = set(measured.items()) if isinstance(measured, dict) else set(measured)
    except TypeError:
        return f"claimed {claimed!r} != measured {measured!r}"
    missing = m_items - c_items
    invented = c_items - m_items
    parts = []
    if missing and not invented:
        parts.append(
            f"the constant is a SAMPLE of the population; it is missing "
            f"{sorted(missing)}"
        )
    if invented and not missing:
        parts.append(
            f"the constant INVENTS members the population does not have: "
            f"{sorted(invented)}"
        )
    if invented and missing:
        parts.append(
            f"the constant disagrees with the population both ways: "
            f"missing {sorted(missing)}, invented {sorted(invented)}"
        )
    if not parts:
        parts.append(f"claimed {claimed!r} != measured {measured!r}")
    return "; ".join(parts)


def verify(claim: Claim, rows) -> None:
    """Raise PopulationError unless the claim holds against measured rows."""
    measured = claim.shape(rows)
    if claim.mode == "equals":
        if measured != claim.claimed:
            raise PopulationError(f"{claim.name}: {_diagnose(claim.claimed, measured)}")
    elif claim.mode == "member":
        seq = list(measured)
        if not seq:
            raise PopulationError(
                f"{claim.name}: the measured population is EMPTY, so the "
                f"membership claim is vacuous"
            )
        if claim.claimed not in seq:
            raise PopulationError(
                f"{claim.name}: {claim.claimed!r} is not in the measured "
                f"population {seq!r}"
            )
    else:
        raise ValueError(f"unknown claim mode {claim.mode!r}")
