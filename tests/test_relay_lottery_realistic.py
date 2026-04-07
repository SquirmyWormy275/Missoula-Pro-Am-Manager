"""
Tests for the Pro-Am Relay lottery with realistic pool sizes from synthetic data.

Uses the conftest.py app/db_session fixtures to run against an in-memory SQLite
database. Seeds the 25 pro competitors and college competitors with correct
lottery opt-in flags and genders, then exercises lottery capacity, draw,
insufficient-pool validation, competitor replacement, and result recording.

Run:  pytest tests/test_relay_lottery_realistic.py -v
"""
import json

import pytest

from tests.conftest import (
    make_college_competitor,
    make_pro_competitor,
    make_team,
    make_tournament,
)
from tests.fixtures.synthetic_data import COLLEGE_TEAMS, PRO_COMPETITORS

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _seed_pro_competitors(session, tournament):
    """Seed all 25 pro competitors with correct lottery opt-in and gender."""
    pros = {}
    for p in PRO_COMPETITORS:
        comp = make_pro_competitor(
            session, tournament,
            name=p['name'],
            gender=p['gender'],
            events=p.get('events', []),
        )
        # pro_am_lottery_opt_in is a DB column on ProCompetitor
        comp.pro_am_lottery_opt_in = p.get('lottery', False)
        session.flush()
        pros[p['name']] = comp
    return pros


def _seed_college_competitors(session, tournament):
    """Seed all college teams and competitors. All college competitors opt in."""
    teams = {}
    comps = {}
    for code, tdata in COLLEGE_TEAMS.items():
        team = make_team(
            session, tournament,
            code=code,
            school=tdata['school'],
            abbrev=tdata['abbrev'],
        )
        teams[code] = team
        for m in tdata['members']:
            c = make_college_competitor(
                session, tournament, team,
                name=m['name'],
                gender=m['gender'],
            )
            # College opt-in stored via property → partners JSON
            c.pro_am_lottery_opt_in = True
            session.flush()
            comps[m['name']] = c
    return teams, comps


def _seed_all(session):
    """Create tournament, pro competitors, and college competitors."""
    tournament = make_tournament(session, name='Relay Test 2026')
    pros = _seed_pro_competitors(session, tournament)
    teams, college = _seed_college_competitors(session, tournament)
    return tournament, pros, teams, college


# ---------------------------------------------------------------------------
# Expected opt-in counts from synthetic data
# Lottery=True pros:
#   F: Cherry Strawberry, Ada Byrd, Caligraphy Jones, Olive Oyle, Wanda Fuca  (5)
#   M: Steptoe Edwall, Finn McCool, Imortal Joe, Cosmo Cramer,
#      Jonathon Wept, Ben Cambium                                              (6)
#
# College: all members opt in via _seed_college_competitors.
# College M: CMC-A(4)+CMC-B(4)+CMC-C(4)+CCU-A(4)+CCU-B(5)+TCU-A(4)+JT-A(2) = 27
# College F: CMC-A(4)+CMC-B(4)+CMC-C(4)+CCU-A(4)+CCU-B(2)+TCU-A(4)+JT-A(3) = 25
# (verified from COLLEGE_TEAMS member counts)
# ---------------------------------------------------------------------------


class TestRelayLotteryCapacity:
    """Verify get_lottery_capacity() returns correct pool sizes and max teams."""

    def test_pool_counts_match_synthetic_data(self, app, db_session):
        from services.proam_relay import ProAmRelay
        tournament, pros, teams, college = _seed_all(db_session)

        relay = ProAmRelay(tournament)
        cap = relay.get_lottery_capacity()

        assert cap['pro_male'] == 6, f"Expected 6 pro males opted in, got {cap['pro_male']}"
        assert cap['pro_female'] == 5, f"Expected 5 pro females opted in, got {cap['pro_female']}"
        # College: all opted in
        assert cap['college_male'] > 0
        assert cap['college_female'] > 0

    def test_max_teams_limited_by_smallest_pool(self, app, db_session):
        from services.proam_relay import ProAmRelay
        tournament, pros, teams, college = _seed_all(db_session)

        relay = ProAmRelay(tournament)
        cap = relay.get_lottery_capacity()

        # min(6//2, 5//2, college_m//2, college_f//2) = min(3, 2, ...) = 2
        assert cap['max_teams'] == 2, (
            f"Expected max_teams=2 (limited by 5 pro women // 2), got {cap['max_teams']}"
        )


class TestRelayLotteryDraw:
    """Verify run_lottery() produces correctly balanced teams."""

    def test_draw_two_teams_gender_balanced(self, app, db_session):
        from services.proam_relay import ProAmRelay
        tournament, pros, teams, college = _seed_all(db_session)

        relay = ProAmRelay(tournament)
        result = relay.run_lottery(num_teams=2)

        assert result['success'] is True
        drawn_teams = result['teams']
        assert len(drawn_teams) == 2

        for team in drawn_teams:
            pro_members = team['pro_members']
            college_members = team['college_members']

            assert len(pro_members) == 4, "Each team needs 4 pro members"
            assert len(college_members) == 4, "Each team needs 4 college members"

            pro_m = [m for m in pro_members if m['gender'] == 'M']
            pro_f = [m for m in pro_members if m['gender'] == 'F']
            col_m = [m for m in college_members if m['gender'] == 'M']
            col_f = [m for m in college_members if m['gender'] == 'F']

            assert len(pro_m) == 2, "Need exactly 2 pro men per team"
            assert len(pro_f) == 2, "Need exactly 2 pro women per team"
            assert len(col_m) == 2, "Need exactly 2 college men per team"
            assert len(col_f) == 2, "Need exactly 2 college women per team"

    def test_no_duplicate_competitors_across_teams(self, app, db_session):
        from services.proam_relay import ProAmRelay
        tournament, pros, teams, college = _seed_all(db_session)

        relay = ProAmRelay(tournament)
        result = relay.run_lottery(num_teams=2)
        drawn_teams = result['teams']

        all_pro_ids = []
        all_college_ids = []
        for team in drawn_teams:
            all_pro_ids.extend(m['id'] for m in team['pro_members'])
            all_college_ids.extend(m['id'] for m in team['college_members'])

        assert len(all_pro_ids) == len(set(all_pro_ids)), "Duplicate pro competitor across teams"
        assert len(all_college_ids) == len(set(all_college_ids)), "Duplicate college competitor across teams"

    def test_draw_sets_status_to_drawn(self, app, db_session):
        from services.proam_relay import ProAmRelay
        tournament, pros, teams, college = _seed_all(db_session)

        relay = ProAmRelay(tournament)
        relay.run_lottery(num_teams=2)

        assert relay.relay_data['status'] == 'drawn'

    def test_all_drawn_competitors_were_opted_in(self, app, db_session):
        from models.competitor import ProCompetitor
        from services.proam_relay import ProAmRelay

        tournament, pros, teams, college = _seed_all(db_session)

        relay = ProAmRelay(tournament)
        result = relay.run_lottery(num_teams=2)

        drawn_pro_ids = set()
        for team in result['teams']:
            for m in team['pro_members']:
                drawn_pro_ids.add(m['id'])

        for pid in drawn_pro_ids:
            comp = ProCompetitor.query.get(pid)
            assert comp.pro_am_lottery_opt_in is True, (
                f"Pro competitor {comp.name} was drawn but not opted in"
            )


class TestRelayLotteryInsufficientPool:
    """Verify ValueError when requesting more teams than the pool supports."""

    def test_three_teams_raises_valueerror(self, app, db_session):
        from services.proam_relay import ProAmRelay
        tournament, pros, teams, college = _seed_all(db_session)

        relay = ProAmRelay(tournament)
        with pytest.raises(ValueError, match="Not enough"):
            relay.run_lottery(num_teams=3)

    def test_one_team_succeeds(self, app, db_session):
        from services.proam_relay import ProAmRelay
        tournament, pros, teams, college = _seed_all(db_session)

        relay = ProAmRelay(tournament)
        result = relay.run_lottery(num_teams=1)
        assert result['success'] is True
        assert len(result['teams']) == 1


class TestRelayCompetitorReplacement:
    """Verify replace_competitor() swaps correctly and enforces gender match."""

    def test_replace_pro_competitor_same_gender(self, app, db_session):
        from services.proam_relay import ProAmRelay
        tournament, pros, teams, college = _seed_all(db_session)

        relay = ProAmRelay(tournament)
        relay.run_lottery(num_teams=1)

        team = relay.relay_data['teams'][0]
        # Find a male pro member on the team
        male_member = next(m for m in team['pro_members'] if m['gender'] == 'M')

        # Find a male pro who was NOT drawn (lottery=False males exist)
        drawn_pro_ids = {m['id'] for m in team['pro_members']}
        from models.competitor import ProCompetitor
        replacement = ProCompetitor.query.filter_by(
            tournament_id=tournament.id,
            gender='M',
            status='active',
            pro_am_lottery_opt_in=True,
        ).filter(~ProCompetitor.id.in_(drawn_pro_ids)).first()

        if replacement:
            relay.replace_competitor(
                team_number=1,
                old_competitor_id=male_member['id'],
                new_competitor_id=replacement.id,
                competitor_type='pro',
            )

            updated_team = relay.relay_data['teams'][0]
            updated_ids = [m['id'] for m in updated_team['pro_members']]
            assert replacement.id in updated_ids
            assert male_member['id'] not in updated_ids

            # Gender balance still correct
            pro_m = [m for m in updated_team['pro_members'] if m['gender'] == 'M']
            pro_f = [m for m in updated_team['pro_members'] if m['gender'] == 'F']
            assert len(pro_m) == 2
            assert len(pro_f) == 2

    def test_replace_with_wrong_gender_raises(self, app, db_session):
        from services.proam_relay import ProAmRelay
        tournament, pros, teams, college = _seed_all(db_session)

        relay = ProAmRelay(tournament)
        relay.run_lottery(num_teams=1)

        team = relay.relay_data['teams'][0]
        male_member = next(m for m in team['pro_members'] if m['gender'] == 'M')

        # Find a female pro who is opted in
        from models.competitor import ProCompetitor
        female_pro = ProCompetitor.query.filter_by(
            tournament_id=tournament.id,
            gender='F',
            status='active',
            pro_am_lottery_opt_in=True,
        ).first()

        if female_pro:
            with pytest.raises(ValueError, match="same gender"):
                relay.replace_competitor(
                    team_number=1,
                    old_competitor_id=male_member['id'],
                    new_competitor_id=female_pro.id,
                    competitor_type='pro',
                )


class TestRelayResultRecording:
    """Verify event result recording and total_time calculation."""

    def test_record_all_events_calculates_total_time(self, app, db_session):
        from services.proam_relay import ProAmRelay
        tournament, pros, teams, college = _seed_all(db_session)

        relay = ProAmRelay(tournament)
        relay.run_lottery(num_teams=2)

        # Record all 4 event results for team 1
        event_times = {
            'partnered_sawing': 45.3,
            'standing_butcher_block': 38.7,
            'underhand_butcher_block': 42.1,
            'team_axe_throw': 15.0,
        }

        for event_name, time_val in event_times.items():
            relay.record_event_result(team_number=1, event_name=event_name, time_seconds=time_val)

        team1 = next(t for t in relay.relay_data['teams'] if t['team_number'] == 1)
        expected_total = sum(event_times.values())
        assert team1['total_time'] == pytest.approx(expected_total, abs=0.01)

    def test_partial_results_no_total_time(self, app, db_session):
        from services.proam_relay import ProAmRelay
        tournament, pros, teams, college = _seed_all(db_session)

        relay = ProAmRelay(tournament)
        relay.run_lottery(num_teams=2)

        # Record only 2 of 4 events
        relay.record_event_result(team_number=1, event_name='partnered_sawing', time_seconds=45.0)
        relay.record_event_result(team_number=1, event_name='standing_butcher_block', time_seconds=38.0)

        team1 = next(t for t in relay.relay_data['teams'] if t['team_number'] == 1)
        assert team1['total_time'] is None, "Total time should be None until all events complete"

    def test_all_teams_complete_sets_status_completed(self, app, db_session):
        from services.proam_relay import ProAmRelay
        tournament, pros, teams, college = _seed_all(db_session)

        relay = ProAmRelay(tournament)
        relay.run_lottery(num_teams=2)

        events = ['partnered_sawing', 'standing_butcher_block',
                  'underhand_butcher_block', 'team_axe_throw']

        for team_num in [1, 2]:
            for evt in events:
                relay.record_event_result(team_number=team_num, event_name=evt,
                                          time_seconds=30.0 + team_num)

        assert relay.relay_data['status'] == 'completed'

    def test_get_results_sorted_by_total_time(self, app, db_session):
        from services.proam_relay import ProAmRelay
        tournament, pros, teams, college = _seed_all(db_session)

        relay = ProAmRelay(tournament)
        relay.run_lottery(num_teams=2)

        events = ['partnered_sawing', 'standing_butcher_block',
                  'underhand_butcher_block', 'team_axe_throw']

        # Team 2 is faster
        for evt in events:
            relay.record_event_result(team_number=1, event_name=evt, time_seconds=50.0)
            relay.record_event_result(team_number=2, event_name=evt, time_seconds=30.0)

        results = relay.get_results()
        assert len(results) == 2
        assert results[0]['team_number'] == 2, "Team with lower total time should be first"
        assert results[1]['team_number'] == 1
