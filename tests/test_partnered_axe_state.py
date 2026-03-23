"""
Partnered Axe Throw state machine tests — prelims, finals, standings.

The PartneredAxeThrow class manages a 3-stage workflow:
  prelims → finals → completed

This file tests the full lifecycle with a real DB.

Run:
    pytest tests/test_partnered_axe_state.py -v
"""
import json
import pytest
from database import db as _db
from tests.conftest import make_tournament, make_pro_competitor, make_event


@pytest.fixture(autouse=True)
def _db_session(db_session):
    """Activate conftest's db_session for every test in this module."""
    yield db_session


@pytest.fixture()
def tournament(db_session):
    return make_tournament(db_session, status='pro_active')


@pytest.fixture()
def axe_event(db_session, tournament):
    return make_event(
        db_session, tournament, 'Partnered Axe Throw',
        event_type='pro', scoring_type='hits',
        scoring_order='highest_wins', stand_type='axe_throw',
        has_prelims=True, requires_triple_runs=True,
    )


def _make_pair(db_session, tournament, name1, name2):
    c1 = make_pro_competitor(db_session, tournament, name1, 'M')
    c2 = make_pro_competitor(db_session, tournament, name2, 'F')
    return c1, c2


# ---------------------------------------------------------------------------
# Basic lifecycle
# ---------------------------------------------------------------------------

class TestPartneredAxeLifecycle:
    """Full prelims → finals → completed workflow."""

    def test_initial_stage_is_prelims(self, db_session, tournament, axe_event):
        from services.partnered_axe import PartneredAxeThrow
        pat = PartneredAxeThrow(axe_event)
        assert pat.get_stage() == 'prelims'

    def test_register_pair(self, db_session, tournament, axe_event):
        from services.partnered_axe import PartneredAxeThrow
        pat = PartneredAxeThrow(axe_event)
        c1, c2 = _make_pair(db_session, tournament, 'PA1', 'PA2')
        db_session.flush()

        pair = pat.register_pair(c1.id, c2.id)
        assert 'pair_id' in pair
        assert len(pat.get_pairs()) == 1

    def test_record_prelim_result(self, db_session, tournament, axe_event):
        from services.partnered_axe import PartneredAxeThrow
        pat = PartneredAxeThrow(axe_event)

        pairs = []
        for i in range(5):
            c1, c2 = _make_pair(db_session, tournament, f'Pre{i}A', f'Pre{i}B')
            db_session.flush()
            pair = pat.register_pair(c1.id, c2.id)
            pairs.append(pair)

        # Record results
        for i, pair in enumerate(pairs):
            pat.record_prelim_result(pair['pair_id'], hits=10 + i)

        standings = pat.get_prelim_standings()
        assert len(standings) == 5
        # Highest score first
        assert standings[0]['prelim_score'] >= standings[-1]['prelim_score']

    def test_can_advance_requires_all_results(self, db_session, tournament, axe_event):
        from services.partnered_axe import PartneredAxeThrow
        pat = PartneredAxeThrow(axe_event)

        pairs = []
        for i in range(4):
            c1, c2 = _make_pair(db_session, tournament, f'Adv{i}A', f'Adv{i}B')
            db_session.flush()
            pair = pat.register_pair(c1.id, c2.id)
            pairs.append(pair)

        # Only record 3 of 4
        for pair in pairs[:3]:
            pat.record_prelim_result(pair['pair_id'], hits=10)

        assert pat.can_advance_to_finals() is False

        # Record the last one
        pat.record_prelim_result(pairs[3]['pair_id'], hits=8)
        assert pat.can_advance_to_finals() is True

    def test_advance_to_finals(self, db_session, tournament, axe_event):
        from services.partnered_axe import PartneredAxeThrow
        pat = PartneredAxeThrow(axe_event)

        pairs = []
        for i in range(6):
            c1, c2 = _make_pair(db_session, tournament, f'Fin{i}A', f'Fin{i}B')
            db_session.flush()
            pair = pat.register_pair(c1.id, c2.id)
            pairs.append(pair)

        for i, pair in enumerate(pairs):
            pat.record_prelim_result(pair['pair_id'], hits=5 + i)

        finalists = pat.advance_to_finals()
        assert len(finalists) == 4
        assert pat.get_stage() == 'finals'

    def test_record_final_results_completes(self, db_session, tournament, axe_event):
        from services.partnered_axe import PartneredAxeThrow
        pat = PartneredAxeThrow(axe_event)

        pairs = []
        for i in range(5):
            c1, c2 = _make_pair(db_session, tournament, f'FC{i}A', f'FC{i}B')
            db_session.flush()
            pair = pat.register_pair(c1.id, c2.id)
            pairs.append(pair)

        for i, pair in enumerate(pairs):
            pat.record_prelim_result(pair['pair_id'], hits=5 + i)

        finalists = pat.advance_to_finals()

        # Record final results
        for i, finalist in enumerate(finalists):
            pat.record_final_result(finalist['pair_id'], hits=10 + i)

        assert pat.get_stage() == 'completed'

    def test_full_standings_merge(self, db_session, tournament, axe_event):
        from services.partnered_axe import PartneredAxeThrow
        pat = PartneredAxeThrow(axe_event)

        pairs = []
        for i in range(6):
            c1, c2 = _make_pair(db_session, tournament, f'FS{i}A', f'FS{i}B')
            db_session.flush()
            pair = pat.register_pair(c1.id, c2.id)
            pairs.append(pair)

        for i, pair in enumerate(pairs):
            pat.record_prelim_result(pair['pair_id'], hits=5 + i)

        finalists = pat.advance_to_finals()
        for i, finalist in enumerate(finalists):
            pat.record_final_result(finalist['pair_id'], hits=20 + i)

        standings = pat.get_full_standings()
        # Top 4 from finals + 2 non-finalists from prelims
        assert len(standings) == 6


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

class TestPartneredAxeEdgeCases:
    """Edge cases and error paths."""

    def test_cannot_advance_with_fewer_than_4_pairs(self, db_session, tournament, axe_event):
        from services.partnered_axe import PartneredAxeThrow
        pat = PartneredAxeThrow(axe_event)

        pairs = []
        for i in range(3):
            c1, c2 = _make_pair(db_session, tournament, f'Few{i}A', f'Few{i}B')
            db_session.flush()
            pair = pat.register_pair(c1.id, c2.id)
            pairs.append(pair)
            pat.record_prelim_result(pair['pair_id'], hits=10)

        assert pat.can_advance_to_finals() is False

    def test_reset_clears_state(self, db_session, tournament, axe_event):
        from services.partnered_axe import PartneredAxeThrow
        pat = PartneredAxeThrow(axe_event)

        c1, c2 = _make_pair(db_session, tournament, 'Reset1', 'Reset2')
        db_session.flush()
        pat.register_pair(c1.id, c2.id)
        assert len(pat.get_pairs()) == 1

        pat.reset()
        assert pat.get_stage() == 'prelims'
        assert len(pat.get_pairs()) == 0

    def test_get_final_standings_empty_before_completion(self, db_session, tournament, axe_event):
        from services.partnered_axe import PartneredAxeThrow
        pat = PartneredAxeThrow(axe_event)
        assert pat.get_final_standings() == []

    def test_exactly_4_pairs_minimum_for_finals(self, db_session, tournament, axe_event):
        from services.partnered_axe import PartneredAxeThrow
        pat = PartneredAxeThrow(axe_event)

        pairs = []
        for i in range(4):
            c1, c2 = _make_pair(db_session, tournament, f'Min{i}A', f'Min{i}B')
            db_session.flush()
            pair = pat.register_pair(c1.id, c2.id)
            pairs.append(pair)
            pat.record_prelim_result(pair['pair_id'], hits=10 + i)

        assert pat.can_advance_to_finals() is True
        finalists = pat.advance_to_finals()
        assert len(finalists) == 4
