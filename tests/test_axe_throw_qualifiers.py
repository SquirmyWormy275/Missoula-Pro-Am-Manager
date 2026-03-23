"""
Tests for the axe throw multi-round scoring workflow.

Covers:
  - Partnered Axe Throw prelim scoring (from PRO_SCORES)
  - Axe throw tie detection via _detect_axe_ties()
  - Throwoff resolution via record_throwoff_result()
  - College Axe Throw qualifier flow (prelims → finals override)

Uses the conftest.py app/db_session fixtures for integration tests that
require the scoring engine to interact with real model objects.

Run:  pytest tests/test_axe_throw_qualifiers.py -v
"""
import json
import pytest
from unittest.mock import MagicMock, patch

from tests.conftest import (
    make_tournament, make_team, make_pro_competitor, make_college_competitor,
    make_event, make_event_result,
)
from tests.fixtures.synthetic_data import PRO_SCORES, PRO_COMPETITORS


# ---------------------------------------------------------------------------
# Partnered Axe Throw prelim data from synthetic_data.py:
#   ('Cosmo Cramer', 23.0, 'completed', 'Finn McCool'),
#   ('Juicy Crust', 19.0, 'completed', 'Garfield Heathcliff'),
#   ('Larry Occidentalis', 18.0, 'completed', 'Steptoe Edwall'),
#   ('Dee John', 17.0, 'completed', 'Carson Mitsubishi'),
#   ('Cherry Strawberry', 14.0, 'completed', 'Epinephrine Needel'),
# ---------------------------------------------------------------------------


class TestAxeThrowPrelimScoring:
    """Test Partnered Axe Throw prelim scoring with 5 pro pairs.

    Uses the PartneredAxeThrow state machine rather than the scoring engine
    since Partnered Axe Throw has its own prelims/finals flow.
    """

    def test_register_and_score_prelims(self, app, db_session):
        from services.partnered_axe import PartneredAxeThrow

        tournament = make_tournament(db_session)
        event = make_event(
            db_session, tournament,
            name='Partnered Axe Throw',
            event_type='pro',
            scoring_type='hits',
            scoring_order='highest_wins',
            is_partnered=True,
            has_prelims=True,
        )

        # Create pro competitors
        pro_lookup = {}
        axe_data = PRO_SCORES['Partnered Axe Throw']
        all_names = set()
        for row in axe_data:
            all_names.add(row[0])
            if len(row) > 3:
                all_names.add(row[3])

        for name in all_names:
            p_info = next((p for p in PRO_COMPETITORS if p['name'] == name), None)
            gender = p_info['gender'] if p_info else 'M'
            comp = make_pro_competitor(db_session, tournament, name=name, gender=gender)
            pro_lookup[name] = comp

        pat = PartneredAxeThrow(event)
        assert pat.get_stage() == 'prelims'

        # Register all 5 pairs
        for row in axe_data:
            comp1_name = row[0]
            partner_name = row[3] if len(row) > 3 else None
            if partner_name:
                pat.register_pair(
                    competitor1_id=pro_lookup[comp1_name].id,
                    competitor2_id=pro_lookup[partner_name].id,
                )

        pairs = pat.get_pairs()
        assert len(pairs) == 5

        # Record prelim scores
        for i, row in enumerate(axe_data):
            hits = int(row[1])
            pat.record_prelim_result(pair_id=i + 1, hits=hits)

        standings = pat.get_prelim_standings()
        assert len(standings) == 5
        # Highest score first
        assert standings[0]['prelim_score'] == 23
        assert standings[-1]['prelim_score'] == 14

    def test_top4_advance_to_finals(self, app, db_session):
        from services.partnered_axe import PartneredAxeThrow

        tournament = make_tournament(db_session)
        event = make_event(
            db_session, tournament,
            name='Partnered Axe Throw',
            event_type='pro',
            scoring_type='hits',
            scoring_order='highest_wins',
            is_partnered=True,
            has_prelims=True,
        )

        # Register and score 5 pairs
        axe_data = PRO_SCORES['Partnered Axe Throw']
        pro_lookup = {}
        all_names = set()
        for row in axe_data:
            all_names.add(row[0])
            if len(row) > 3:
                all_names.add(row[3])
        for name in all_names:
            p_info = next((p for p in PRO_COMPETITORS if p['name'] == name), None)
            gender = p_info['gender'] if p_info else 'M'
            comp = make_pro_competitor(db_session, tournament, name=name, gender=gender)
            pro_lookup[name] = comp

        pat = PartneredAxeThrow(event)
        for i, row in enumerate(axe_data):
            comp1 = pro_lookup[row[0]]
            partner = pro_lookup[row[3]] if len(row) > 3 else None
            if partner:
                pat.register_pair(comp1.id, partner.id)

        for i, row in enumerate(axe_data):
            pat.record_prelim_result(pair_id=i + 1, hits=int(row[1]))

        assert pat.can_advance_to_finals()
        finalists = pat.advance_to_finals()

        assert len(finalists) == 4
        assert pat.get_stage() == 'finals'

        # Cherry Strawberry / Epinephrine Needel (14 hits) should NOT be a finalist
        finalist_scores = [f['prelim_score'] for f in finalists]
        assert 14 not in finalist_scores, "Lowest prelim pair should not advance"

    def test_finals_determine_positions_1_through_4(self, app, db_session):
        from services.partnered_axe import PartneredAxeThrow

        tournament = make_tournament(db_session)
        event = make_event(
            db_session, tournament,
            name='Partnered Axe Throw',
            event_type='pro',
            scoring_type='hits',
            scoring_order='highest_wins',
            is_partnered=True,
            has_prelims=True,
        )

        axe_data = PRO_SCORES['Partnered Axe Throw']
        pro_lookup = {}
        all_names = set()
        for row in axe_data:
            all_names.add(row[0])
            if len(row) > 3:
                all_names.add(row[3])
        for name in all_names:
            p_info = next((p for p in PRO_COMPETITORS if p['name'] == name), None)
            gender = p_info['gender'] if p_info else 'M'
            comp = make_pro_competitor(db_session, tournament, name=name, gender=gender)
            pro_lookup[name] = comp

        pat = PartneredAxeThrow(event)
        for i, row in enumerate(axe_data):
            partner = row[3] if len(row) > 3 else None
            if partner:
                pat.register_pair(pro_lookup[row[0]].id, pro_lookup[partner].id)
        for i, row in enumerate(axe_data):
            pat.record_prelim_result(pair_id=i + 1, hits=int(row[1]))

        pat.advance_to_finals()

        # Record finals results (different scores than prelims)
        finalists = pat.get_finalists()
        final_scores = [25, 22, 20, 18]  # pair 1 wins finals too
        for i, pair in enumerate(finalists):
            pat.record_final_result(pair_id=pair['pair_id'], hits=final_scores[i])

        assert pat.get_stage() == 'completed'

        final_standings = pat.get_final_standings()
        assert len(final_standings) == 4
        assert final_standings[0]['final_position'] == 1
        assert final_standings[0]['final_score'] == 25
        assert final_standings[3]['final_position'] == 4

    def test_full_standings_positions_5_plus_from_prelims(self, app, db_session):
        from services.partnered_axe import PartneredAxeThrow

        tournament = make_tournament(db_session)
        event = make_event(
            db_session, tournament,
            name='Partnered Axe Throw',
            event_type='pro',
            scoring_type='hits',
            scoring_order='highest_wins',
            is_partnered=True,
            has_prelims=True,
        )

        axe_data = PRO_SCORES['Partnered Axe Throw']
        pro_lookup = {}
        all_names = set()
        for row in axe_data:
            all_names.add(row[0])
            if len(row) > 3:
                all_names.add(row[3])
        for name in all_names:
            p_info = next((p for p in PRO_COMPETITORS if p['name'] == name), None)
            gender = p_info['gender'] if p_info else 'M'
            comp = make_pro_competitor(db_session, tournament, name=name, gender=gender)
            pro_lookup[name] = comp

        pat = PartneredAxeThrow(event)
        for i, row in enumerate(axe_data):
            partner = row[3] if len(row) > 3 else None
            if partner:
                pat.register_pair(pro_lookup[row[0]].id, pro_lookup[partner].id)
        for i, row in enumerate(axe_data):
            pat.record_prelim_result(pair_id=i + 1, hits=int(row[1]))

        pat.advance_to_finals()
        finalists = pat.get_finalists()
        for i, pair in enumerate(finalists):
            pat.record_final_result(pair_id=pair['pair_id'], hits=25 - i)

        full = pat.get_full_standings()
        assert len(full) == 5, "All 5 pairs should appear in full standings"

        # 5th place pair is the one that didn't make finals
        fifth_place = next(p for p in full if p['final_position'] == 5)
        assert fifth_place['prelim_score'] == 14, "5th place should be Cherry/Epinephrine (14 hits)"


class TestAxeThrowTieDetection:
    """Test _detect_axe_ties() from the scoring engine."""

    def test_detects_two_competitors_with_same_score(self, app, db_session):
        from services.scoring_engine import _detect_axe_ties

        tournament = make_tournament(db_session)
        event = make_event(
            db_session, tournament,
            name='Axe Throw',
            event_type='college',
            scoring_type='score',
            scoring_order='highest_wins',
            requires_triple_runs=True,
        )

        team = make_team(db_session, tournament)
        c1 = make_college_competitor(db_session, tournament, team, 'Comp A', gender='M')
        c2 = make_college_competitor(db_session, tournament, team, 'Comp B', gender='M')
        c3 = make_college_competitor(db_session, tournament, team, 'Comp C', gender='M')

        r1 = make_event_result(db_session, event, c1, competitor_type='college',
                               result_value=15.0, run1_value=5.0, run2_value=5.0, run3_value=5.0,
                               status='completed')
        r2 = make_event_result(db_session, event, c2, competitor_type='college',
                               result_value=15.0, run1_value=6.0, run2_value=4.0, run3_value=5.0,
                               status='completed')
        r3 = make_event_result(db_session, event, c3, competitor_type='college',
                               result_value=12.0, run1_value=4.0, run2_value=4.0, run3_value=4.0,
                               status='completed')

        tie_groups = _detect_axe_ties([r1, r2, r3])
        assert len(tie_groups) == 1, "Should detect one tie group"
        assert len(tie_groups[0]) == 2, "Tie group should have 2 competitors"

        tied_ids = {r.competitor_id for r in tie_groups[0]}
        assert tied_ids == {c1.id, c2.id}

    def test_no_ties_returns_empty(self, app, db_session):
        from services.scoring_engine import _detect_axe_ties

        tournament = make_tournament(db_session)
        event = make_event(
            db_session, tournament,
            name='Axe Throw',
            event_type='college',
            scoring_type='score',
            scoring_order='highest_wins',
        )

        team = make_team(db_session, tournament)
        c1 = make_college_competitor(db_session, tournament, team, 'Comp A', gender='M')
        c2 = make_college_competitor(db_session, tournament, team, 'Comp B', gender='M')

        r1 = make_event_result(db_session, event, c1, competitor_type='college',
                               result_value=15.0, status='completed')
        r2 = make_event_result(db_session, event, c2, competitor_type='college',
                               result_value=12.0, status='completed')

        tie_groups = _detect_axe_ties([r1, r2])
        assert len(tie_groups) == 0

    def test_calculate_positions_sets_throwoff_pending(self, app, db_session):
        from services.scoring_engine import calculate_positions

        tournament = make_tournament(db_session)
        # The event name must be in AXE_THROW_CUMULATIVE_EVENTS for is_axe_throw_cumulative
        event = make_event(
            db_session, tournament,
            name='Axe Throw',
            event_type='college',
            scoring_type='score',
            scoring_order='highest_wins',
            requires_triple_runs=True,
        )

        team = make_team(db_session, tournament)
        c1 = make_college_competitor(db_session, tournament, team, 'Thrower A', gender='M')
        c2 = make_college_competitor(db_session, tournament, team, 'Thrower B', gender='M')

        r1 = make_event_result(db_session, event, c1, competitor_type='college',
                               result_value=18.0, run1_value=6.0, run2_value=6.0, run3_value=6.0,
                               status='completed')
        r2 = make_event_result(db_session, event, c2, competitor_type='college',
                               result_value=18.0, run1_value=7.0, run2_value=5.0, run3_value=6.0,
                               status='completed')

        calculate_positions(event)

        assert r1.throwoff_pending is True, "Tied axe thrower should have throwoff_pending"
        assert r2.throwoff_pending is True, "Tied axe thrower should have throwoff_pending"


class TestAxeThrowThrowoffResolution:
    """Test record_throwoff_result() to resolve ties."""

    def test_throwoff_resolves_positions(self, app, db_session):
        from services.scoring_engine import calculate_positions, record_throwoff_result

        tournament = make_tournament(db_session)
        event = make_event(
            db_session, tournament,
            name='Axe Throw',
            event_type='college',
            scoring_type='score',
            scoring_order='highest_wins',
            requires_triple_runs=True,
        )

        team = make_team(db_session, tournament)
        c1 = make_college_competitor(db_session, tournament, team, 'Thrower X', gender='M')
        c2 = make_college_competitor(db_session, tournament, team, 'Thrower Y', gender='M')
        c3 = make_college_competitor(db_session, tournament, team, 'Thrower Z', gender='M')

        r1 = make_event_result(db_session, event, c1, competitor_type='college',
                               result_value=20.0, run1_value=7.0, run2_value=7.0, run3_value=6.0,
                               status='completed')
        r2 = make_event_result(db_session, event, c2, competitor_type='college',
                               result_value=20.0, run1_value=6.0, run2_value=8.0, run3_value=6.0,
                               status='completed')
        r3 = make_event_result(db_session, event, c3, competitor_type='college',
                               result_value=15.0, run1_value=5.0, run2_value=5.0, run3_value=5.0,
                               status='completed')

        # First pass sets throwoff_pending
        calculate_positions(event)
        assert r1.throwoff_pending is True
        assert r2.throwoff_pending is True
        assert r3.throwoff_pending is False

        # Judge resolves: Thrower X wins the throwoff → position 1, Y → position 2
        position_map = {
            r1.id: 1,
            r2.id: 2,
        }
        record_throwoff_result(event, position_map)

        assert r1.final_position == 1
        assert r2.final_position == 2
        assert r1.throwoff_pending is False
        assert r2.throwoff_pending is False
        # r3 was 3rd from calculate_positions and unchanged
        assert r3.final_position == 3

    def test_throwoff_updates_points_correctly(self, app, db_session):
        from services.scoring_engine import calculate_positions, record_throwoff_result
        import config

        tournament = make_tournament(db_session)
        event = make_event(
            db_session, tournament,
            name='Axe Throw',
            event_type='college',
            scoring_type='score',
            scoring_order='highest_wins',
            requires_triple_runs=True,
        )

        team = make_team(db_session, tournament)
        c1 = make_college_competitor(db_session, tournament, team, 'Thr A', gender='M')
        c2 = make_college_competitor(db_session, tournament, team, 'Thr B', gender='M')

        r1 = make_event_result(db_session, event, c1, competitor_type='college',
                               result_value=21.0, status='completed')
        r2 = make_event_result(db_session, event, c2, competitor_type='college',
                               result_value=21.0, status='completed')

        calculate_positions(event)

        # After calculate_positions, both tied at position 1
        # Now resolve: r2 gets 1st, r1 gets 2nd
        record_throwoff_result(event, {r2.id: 1, r1.id: 2})

        assert r2.points_awarded == config.PLACEMENT_POINTS.get(1, 0)
        assert r1.points_awarded == config.PLACEMENT_POINTS.get(2, 0)


class TestAxeThrowCollegeQualifiers:
    """Test college Axe Throw where finals override prelim positions for top placements."""

    def test_prelim_scoring_positions_correct(self, app, db_session):
        """Score 6 college competitors in Axe Throw; verify highest_wins ordering."""
        from services.scoring_engine import calculate_positions

        tournament = make_tournament(db_session)
        event = make_event(
            db_session, tournament,
            name='Axe Throw',
            event_type='college',
            gender='M',
            scoring_type='score',
            scoring_order='highest_wins',
            requires_triple_runs=True,
        )

        team = make_team(db_session, tournament)
        competitors = []
        scores = [18, 16, 15, 14, 12, 10]
        for i, score in enumerate(scores):
            c = make_college_competitor(
                db_session, tournament, team,
                name=f'Axe Thrower {i+1}', gender='M',
            )
            competitors.append(c)
            make_event_result(
                db_session, event, c, competitor_type='college',
                result_value=float(score),
                run1_value=float(score // 3),
                run2_value=float(score // 3),
                run3_value=float(score - 2 * (score // 3)),
                status='completed',
            )

        calculate_positions(event)

        results = event.results.order_by(None).all()
        results_by_id = {r.competitor_id: r for r in results}

        # Highest score should be position 1
        assert results_by_id[competitors[0].id].final_position == 1
        assert results_by_id[competitors[5].id].final_position == 6

    def test_finals_override_prelim_positions(self, app, db_session):
        """Simulate a finals round overriding prelim positions for top 4.

        In the Partnered Axe Throw flow, finals scores replace positions 1-4.
        For college Axe Throw, we simulate this by re-scoring with
        record_throwoff_result or by direct position override.
        """
        from services.scoring_engine import calculate_positions, record_throwoff_result

        tournament = make_tournament(db_session)
        event = make_event(
            db_session, tournament,
            name='Axe Throw',
            event_type='college',
            gender='M',
            scoring_type='score',
            scoring_order='highest_wins',
            requires_triple_runs=True,
        )

        team = make_team(db_session, tournament)

        # 6 competitors with distinct prelim scores
        comp_scores = [
            ('Alpha', 20.0),
            ('Bravo', 18.0),
            ('Charlie', 16.0),
            ('Delta', 14.0),
            ('Echo', 12.0),
            ('Foxtrot', 10.0),
        ]

        result_objs = {}
        for name, score in comp_scores:
            c = make_college_competitor(db_session, tournament, team, name=name, gender='M')
            r = make_event_result(
                db_session, event, c, competitor_type='college',
                result_value=score, status='completed',
            )
            result_objs[name] = r

        calculate_positions(event)

        # Prelim positions: Alpha=1, Bravo=2, Charlie=3, Delta=4, Echo=5, Foxtrot=6
        assert result_objs['Alpha'].final_position == 1
        assert result_objs['Bravo'].final_position == 2

        # Simulate finals: Charlie wins, Alpha drops to 3rd among top 4
        # Use record_throwoff_result to override positions for the top 4
        finals_map = {
            result_objs['Charlie'].id: 1,
            result_objs['Bravo'].id: 2,
            result_objs['Alpha'].id: 3,
            result_objs['Delta'].id: 4,
        }
        record_throwoff_result(event, finals_map)

        assert result_objs['Charlie'].final_position == 1, "Finals winner should be 1st"
        assert result_objs['Alpha'].final_position == 3, "Prelim leader dropped to 3rd in finals"
        # Echo and Foxtrot retain prelim positions 5 and 6
        assert result_objs['Echo'].final_position == 5
        assert result_objs['Foxtrot'].final_position == 6
