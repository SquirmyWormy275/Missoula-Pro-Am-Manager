"""
O5: execute every registered population claim against a fresh clone.

The claims themselves are declared next to the constants they guard (see
population.py for the pattern and the c29 incident that motivates it). This
module does two jobs:

1. Runs each registered claim's measuring query against the real mirror and
   verifies the constant IS the population, with a diagnosis that names the
   missing or invented members.

2. Meta-tests verify() itself with deliberately sampled, invented, and
   vacuous claims. The guard is only worth what its strictness is worth: an
   edit that quietly turns equality into a subset check makes every guard
   above it decorative, and dies here instead.
"""

import pytest

# Importing the claim-bearing modules is what populates CLAIMS. Explicit,
# so this module does not depend on pytest's collection order.
import test_sev2_confirmed  # noqa: F401
import test_sev3_confirmed  # noqa: F401
from population import CLAIMS, Claim, PopulationError, verify


def _claim_ids():
    return [c.name[:70] for c in CLAIMS]


@pytest.mark.sev3
@pytest.mark.parametrize("claim", CLAIMS, ids=_claim_ids())
def test_the_constant_is_the_population(claim, sql):
    rows = sql(claim.sql, **claim.params)
    verify(claim, rows)


@pytest.mark.sev3
def test_at_least_the_founding_claims_are_registered():
    """The registry going quietly empty (an import shuffle, a refactor that
    drops the register calls) would make the parametrized test above vanish
    instead of fail. Four claims founded this module; fewer than four
    registered means the guard rail fell off the cliff it guards."""
    assert len(CLAIMS) >= 4, [c.name for c in CLAIMS]


# ---------------------------------------------------------------------------
# Meta-tests: the guard's own strictness.
# ---------------------------------------------------------------------------

_META_ROWS = [(29, "Dwight Severson"), (33, "Jack Love"), (37, "Karson Wilson")]


def _meta_claim(claimed, mode="equals"):
    return Claim(
        name="meta",
        claimed=claimed,
        sql="unused",
        shape=lambda rows: {int(r[0]): r[1] for r in rows},
        mode=mode,
    )


@pytest.mark.sev3
def test_a_sampled_constant_is_rejected():
    """The exact c29 shape: two names claimed, three measured. The error
    must say 'sample' and name the missing man."""
    sampled = {29: "Dwight Severson", 37: "Karson Wilson"}
    with pytest.raises(PopulationError, match="SAMPLE") as err:
        verify(_meta_claim(sampled), _META_ROWS)
    assert "Jack Love" in str(err.value)


@pytest.mark.sev3
def test_an_invented_member_is_rejected():
    invented = dict(_META_ROWS) | {99: "Nobody Realman"}
    with pytest.raises(PopulationError, match="INVENTS"):
        verify(_meta_claim(invented), _META_ROWS)


@pytest.mark.sev3
def test_a_wrong_name_is_rejected_both_ways():
    wrong = dict(_META_ROWS) | {33: "Jack Glove"}
    with pytest.raises(PopulationError, match="both ways"):
        verify(_meta_claim(wrong), _META_ROWS)


@pytest.mark.sev3
def test_an_exact_constant_passes():
    verify(_meta_claim(dict(_META_ROWS)), _META_ROWS)


@pytest.mark.sev3
def test_a_membership_claim_over_an_empty_population_is_vacuous_and_fails():
    claim = Claim(
        name="meta",
        claimed=(9, "Gillian Shannon"),
        sql="unused",
        shape=lambda rows: [(int(r[0]), r[1]) for r in rows],
        mode="member",
    )
    with pytest.raises(PopulationError, match="EMPTY"):
        verify(claim, [])
    with pytest.raises(PopulationError, match="not in the measured"):
        verify(claim, [(10, "Somebody Else")])
    verify(claim, [(9, "Gillian Shannon"), (10, "Somebody Else")])
