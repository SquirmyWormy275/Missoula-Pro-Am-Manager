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
from models import EventResult
from models.competitor import ProCompetitor
from tests.conftest import make_event, make_pro_competitor, make_tournament


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


def _make_pair(db_session, tournament, name1, name2, event_id=None):
    # Tests must enroll pairs in the axe_event before register_pair() will
    # accept them — the service now requires that event.id appears in both
    # competitors' events_entered list (tenancy hardening, April 2026).
    events = [event_id] if event_id is not None else []
    c1 = make_pro_competitor(db_session, tournament, name1, 'M', events=events)
    c2 = make_pro_competitor(db_session, tournament, name2, 'F', events=events)
    return c1, c2


def _complete_event(db_session, tournament, axe_event):
    """Run the real state machine through a four-pair final."""
    from services.partnered_axe import PartneredAxeThrow

    pat = PartneredAxeThrow(axe_event)
    pairs = []
    for i in range(5):
        c1, c2 = _make_pair(
            db_session, tournament, f'Complete{i}A', f'Complete{i}B',
            event_id=axe_event.id,
        )
        db_session.flush()
        pairs.append(pat.register_pair(c1.id, c2.id))

    for i, pair in enumerate(pairs):
        pat.record_prelim_result(pair['pair_id'], hits=10 + i)

    for i, finalist in enumerate(pat.advance_to_finals()):
        pat.record_final_result(finalist['pair_id'], hits=20 + i)

    return pat


def _add_final_heat(db_session, axe_event, pair):
    """Create the show card a flight build must preserve after final scoring."""
    from models import Heat

    heat = Heat(event_id=axe_event.id, heat_number=1, run_number=1)
    db_session.add(heat)
    db_session.flush()
    competitor_ids = [pair['competitor1']['id'], pair['competitor2']['id']]
    heat.set_roster('pro', competitor_ids, {competitor_id: 1 for competitor_id in competitor_ids})
    db_session.flush()
    return heat


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
        c1, c2 = _make_pair(db_session, tournament, 'PA1', 'PA2', event_id=axe_event.id)
        db_session.flush()

        pair = pat.register_pair(c1.id, c2.id)
        assert 'pair_id' in pair
        assert len(pat.get_pairs()) == 1

    def test_record_prelim_result(self, db_session, tournament, axe_event):
        from services.partnered_axe import PartneredAxeThrow
        pat = PartneredAxeThrow(axe_event)

        pairs = []
        for i in range(5):
            c1, c2 = _make_pair(db_session, tournament, f'Pre{i}A', f'Pre{i}B', event_id=axe_event.id)
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
            c1, c2 = _make_pair(db_session, tournament, f'Adv{i}A', f'Adv{i}B', event_id=axe_event.id)
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
            c1, c2 = _make_pair(db_session, tournament, f'Fin{i}A', f'Fin{i}B', event_id=axe_event.id)
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
            c1, c2 = _make_pair(db_session, tournament, f'FC{i}A', f'FC{i}B', event_id=axe_event.id)
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

    def test_completion_finalizes_payout_ledger(self, db_session, tournament, axe_event):
        """The state-machine finish must publish official pro payouts."""
        axe_event.set_payouts({'1': 500, '2': 300, '3': 200, '4': 100})
        db_session.commit()

        _complete_event(db_session, tournament, axe_event)

        results = sorted(
            axe_event.results.all(),
            key=lambda result: result.final_position or 999,
        )
        assert axe_event.status == 'completed'
        assert axe_event.is_finalized is True
        assert len(results) == 10
        assert [r.final_position for r in results] == [
            1, 1, 2, 2, 3, 3, 4, 4, 5, 5,
        ]
        assert [r.payout_amount for r in results] == [
            500, 500, 300, 300, 200, 200, 100, 100, 0, 0,
        ]

        competitors = ProCompetitor.query.filter_by(
            tournament_id=tournament.id
        ).all()
        assert sorted(comp.total_earnings for comp in competitors) == [
            0, 0, 100, 100, 200, 200, 300, 300, 500, 500,
        ]

    def test_show_rebuild_preserves_completed_final_card(
            self, db_session, tournament, axe_event):
        """A later flight build cannot replace a completed Partnered Axe final."""
        from models import Heat
        from services.flight_builder import _prepare_partnered_axe_show_heats

        pat = _complete_event(db_session, tournament, axe_event)
        heat = _add_final_heat(db_session, axe_event, pat.get_finalists()[0])
        heat.status = 'completed'
        heat_id = heat.id
        roster = heat.get_competitors()
        db_session.flush()

        prepared = _prepare_partnered_axe_show_heats(axe_event)

        assert [prepared_heat.id for prepared_heat in prepared] == [heat_id]
        preserved = db_session.get(Heat, heat_id)
        assert preserved.status == 'completed'
        assert preserved.get_competitors() == roster

    def test_show_rebuild_preserves_partially_scored_final_card(
            self, db_session, tournament, axe_event):
        """A live finals score is enough to make the show card immutable."""
        from models import Heat
        from services.flight_builder import _prepare_partnered_axe_show_heats
        from services.partnered_axe import PartneredAxeThrow

        pat = PartneredAxeThrow(axe_event)
        pairs = []
        for i in range(4):
            first, second = _make_pair(
                db_session, tournament, f'Live{i}A', f'Live{i}B', event_id=axe_event.id,
            )
            db_session.flush()
            pairs.append(pat.register_pair(first.id, second.id))
        for i, pair in enumerate(pairs):
            pat.record_prelim_result(pair['pair_id'], hits=10 + i)
        finalists = pat.advance_to_finals()
        heat = _add_final_heat(db_session, axe_event, finalists[0])
        pat.record_final_result(finalists[0]['pair_id'], hits=20)
        heat_id = heat.id
        db_session.flush()

        prepared = _prepare_partnered_axe_show_heats(axe_event)

        assert [prepared_heat.id for prepared_heat in prepared] == [heat_id]
        assert db_session.get(Heat, heat_id).get_competitors() == heat.get_competitors()

    def test_final_correction_reopens_only_changed_payouts(
            self, db_session, tournament, axe_event):
        axe_event.set_payouts({'1': 500, '2': 300, '3': 200, '4': 100})
        db_session.commit()
        pat = _complete_event(db_session, tournament, axe_event)

        original_winner = axe_event.results.filter_by(final_position=1).first()
        unaffected_fifth = axe_event.results.filter_by(final_position=5).first()
        original_winner.payout_settled = True
        unaffected_fifth.payout_settled = True
        db_session.commit()

        corrected_pair = pat.get_finalists()[-1]
        pat.record_final_result(corrected_pair['pair_id'], hits=100)

        db_session.expire_all()
        corrected = axe_event.results.filter_by(
            competitor_id=corrected_pair['competitor1']['id']
        ).one()
        original_winner = db_session.get(EventResult, original_winner.id)
        unaffected_fifth = db_session.get(EventResult, unaffected_fifth.id)
        assert corrected.final_position == 1
        assert corrected.payout_amount == 500
        assert original_winner.final_position == 2
        assert original_winner.payout_settled is False
        assert unaffected_fifth.payout_settled is True

    def test_full_standings_merge(self, db_session, tournament, axe_event):
        from services.partnered_axe import PartneredAxeThrow
        pat = PartneredAxeThrow(axe_event)

        pairs = []
        for i in range(6):
            c1, c2 = _make_pair(db_session, tournament, f'FS{i}A', f'FS{i}B', event_id=axe_event.id)
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

    def test_payout_configuration_preserves_pair_state(
            self, auth_client, db_session, tournament, axe_event):
        """Purses and Partnered Axe state occupy separate columns."""
        from services.partnered_axe import PartneredAxeThrow

        pat = PartneredAxeThrow(axe_event)
        c1, c2 = _make_pair(
            db_session, tournament, 'Payout State A', 'Payout State B',
            event_id=axe_event.id,
        )
        pat.register_pair(c1.id, c2.id)
        state_before = axe_event.event_state

        response = auth_client.post(
            f'/scoring/{tournament.id}/event/{axe_event.id}/payouts',
            data={'payout_1': '500', 'payout_2': '300'},
            follow_redirects=False,
        )

        assert response.status_code == 302
        assert axe_event.get_payouts() == {'1': 500.0, '2': 300.0}
        assert axe_event.event_state == state_before


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
            c1, c2 = _make_pair(db_session, tournament, f'Few{i}A', f'Few{i}B', event_id=axe_event.id)
            db_session.flush()
            pair = pat.register_pair(c1.id, c2.id)
            pairs.append(pair)
            pat.record_prelim_result(pair['pair_id'], hits=10)

        assert pat.can_advance_to_finals() is False

    @pytest.mark.parametrize('invalid_hits', [-1, float('inf'), float('nan'), 3.5, True])
    def test_invalid_prelim_hits_do_not_change_state(
            self, db_session, tournament, axe_event, invalid_hits):
        from services.partnered_axe import PartneredAxeThrow

        pat = PartneredAxeThrow(axe_event)
        c1, c2 = _make_pair(
            db_session, tournament, 'Invalid Hits A', 'Invalid Hits B',
            event_id=axe_event.id,
        )
        db_session.flush()
        pair = pat.register_pair(c1.id, c2.id)

        with pytest.raises(ValueError, match='finite, non-negative whole number'):
            pat.record_prelim_result(pair['pair_id'], invalid_hits)

        assert pat.get_prelim_standings() == []
        assert axe_event.results.count() == 0

    def test_reset_clears_state(self, db_session, tournament, axe_event):
        from services.partnered_axe import PartneredAxeThrow
        pat = PartneredAxeThrow(axe_event)

        c1, c2 = _make_pair(db_session, tournament, 'Reset1', 'Reset2', event_id=axe_event.id)
        db_session.flush()
        pat.register_pair(c1.id, c2.id)
        assert len(pat.get_pairs()) == 1

        pat.reset()
        assert pat.get_stage() == 'prelims'
        assert len(pat.get_pairs()) == 0

    def test_reset_removes_unsettled_results_and_earnings(
            self, db_session, tournament, axe_event):
        axe_event.set_payouts({'1': 500, '2': 300, '3': 200, '4': 100})
        db_session.commit()
        pat = _complete_event(db_session, tournament, axe_event)

        pat.reset()

        assert axe_event.results.count() == 0
        assert axe_event.status == 'pending'
        assert axe_event.is_finalized is False
        assert pat.get_stage() == 'prelims'
        assert all(
            comp.total_earnings == 0
            for comp in ProCompetitor.query.filter_by(
                tournament_id=tournament.id
            ).all()
        )

    def test_reset_refuses_to_erase_settled_payouts(
            self, db_session, tournament, axe_event):
        axe_event.set_payouts({'1': 500})
        db_session.commit()
        pat = _complete_event(db_session, tournament, axe_event)
        winning_result = axe_event.results.filter_by(final_position=1).first()
        winning_result.payout_settled = True
        db_session.commit()

        with pytest.raises(ValueError, match='payouts are settled'):
            pat.reset()

        assert axe_event.results.count() == 10
        assert axe_event.is_finalized is True

    def test_get_final_standings_empty_before_completion(self, db_session, tournament, axe_event):
        from services.partnered_axe import PartneredAxeThrow
        pat = PartneredAxeThrow(axe_event)
        assert pat.get_final_standings() == []

    def test_exactly_4_pairs_minimum_for_finals(self, db_session, tournament, axe_event):
        from services.partnered_axe import PartneredAxeThrow
        pat = PartneredAxeThrow(axe_event)

        pairs = []
        for i in range(4):
            c1, c2 = _make_pair(db_session, tournament, f'Min{i}A', f'Min{i}B', event_id=axe_event.id)
            db_session.flush()
            pair = pat.register_pair(c1.id, c2.id)
            pairs.append(pair)
            pat.record_prelim_result(pair['pair_id'], hits=10 + i)

        assert pat.can_advance_to_finals() is True
        finalists = pat.advance_to_finals()
        assert len(finalists) == 4
