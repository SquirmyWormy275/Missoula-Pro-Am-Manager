"""
Preflight service tests — heat/table sync, odd partner pools,
Saturday overflow detection, and fully-valid tournament pass-through.

Covers ``services.preflight.build_preflight_report()`` against an
in-memory SQLite database using the same fixture pattern as test_woodboss.py.

Run:
    pytest tests/test_preflight.py -v

Requirements:
    pytest (pip install pytest)
    All app dependencies installed.
"""
import json
import os

import pytest

from database import db as _db

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope='module')
def app():
    """Test Flask app with temp-file SQLite built via flask db upgrade."""
    from tests.db_test_utils import create_test_app
    _app, db_path = create_test_app()

    with _app.app_context():
        yield _app
        _db.session.remove()
    try:
        os.unlink(db_path)
    except OSError:
        pass


@pytest.fixture(autouse=True)
def db_session(app):
    """Wrap each test in a transaction and roll back afterward."""
    with app.app_context():
        _db.session.begin_nested()
        yield _db.session
        _db.session.rollback()


@pytest.fixture()
def tournament(db_session):
    """Create a fresh tournament."""
    from models import Tournament
    t = Tournament(name='Preflight Test 2026', year=2026, status='setup')
    db_session.add(t)
    db_session.flush()
    return t


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_event(db_session, tournament, name, event_type='pro', gender=None,
                scoring_type='time', stand_type=None, is_partnered=False):
    """Create and return an Event."""
    from models import Event
    e = Event(
        tournament_id=tournament.id,
        name=name,
        event_type=event_type,
        gender=gender,
        scoring_type=scoring_type,
        stand_type=stand_type,
        is_partnered=is_partnered,
    )
    db_session.add(e)
    db_session.flush()
    return e


def _make_heat(db_session, event, heat_number=1, run_number=1,
               competitor_ids=None, flight_id=None):
    """Create and return a Heat with optional competitor JSON."""
    from models.heat import Heat
    h = Heat(
        event_id=event.id,
        heat_number=heat_number,
        run_number=run_number,
        flight_id=flight_id,
    )
    if competitor_ids is not None:
        h.set_competitors(competitor_ids)
    db_session.add(h)
    db_session.flush()
    return h


def _make_heat_assignment(db_session, heat_id, competitor_id,
                          competitor_type='pro', stand_number=None):
    """Create a HeatAssignment row."""
    from models.heat import HeatAssignment
    ha = HeatAssignment(
        heat_id=heat_id,
        competitor_id=competitor_id,
        competitor_type=competitor_type,
        stand_number=stand_number,
    )
    db_session.add(ha)
    db_session.flush()
    return ha


def _make_pro(db_session, tournament, name, gender='M', event_ids=None,
              status='active'):
    """Create an active ProCompetitor."""
    from models.competitor import ProCompetitor
    c = ProCompetitor(
        tournament_id=tournament.id,
        name=name,
        gender=gender,
        status=status,
    )
    if event_ids:
        c.set_events_entered(event_ids)
    db_session.add(c)
    db_session.flush()
    return c


def _make_flight(db_session, tournament, flight_number=1):
    """Create and return a Flight."""
    from models.heat import Flight
    f = Flight(
        tournament_id=tournament.id,
        flight_number=flight_number,
    )
    db_session.add(f)
    db_session.flush()
    return f


# ---------------------------------------------------------------------------
# Empty tournament — no events, no heats
# ---------------------------------------------------------------------------

class TestEmptyTournament:
    """A tournament with no events or heats should return zero issues."""

    def test_no_events_returns_clean_report(self, db_session, tournament):
        from services.preflight import build_preflight_report
        report = build_preflight_report(tournament)

        assert report['issue_count'] == 0
        assert report['issues'] == []
        assert report['has_autofixable'] is False

    def test_severity_counts_all_zero(self, db_session, tournament):
        from services.preflight import build_preflight_report
        report = build_preflight_report(tournament)

        assert report['severity']['high'] == 0
        assert report['severity']['medium'] == 0
        assert report['severity']['low'] == 0


# ---------------------------------------------------------------------------
# Heat/table sync mismatch detection
# ---------------------------------------------------------------------------

class TestHeatSyncMismatch:
    """Detect divergence between Heat.competitors JSON and HeatAssignment rows."""

    def test_matching_json_and_table_no_issue(self, db_session, tournament):
        """When JSON and table agree, no heat_sync_mismatch issue."""
        event = _make_event(db_session, tournament, 'Underhand', stand_type='underhand')
        comp = _make_pro(db_session, tournament, 'John Doe', event_ids=[event.id])
        heat = _make_heat(db_session, event, competitor_ids=[comp.id])
        _make_heat_assignment(db_session, heat.id, comp.id)

        from services.preflight import build_preflight_report
        report = build_preflight_report(tournament)

        codes = [i['code'] for i in report['issues']]
        assert 'heat_sync_mismatch' not in codes

    def test_extra_in_json_triggers_mismatch(self, db_session, tournament):
        """JSON has a competitor ID that HeatAssignment does not."""
        event = _make_event(db_session, tournament, 'Underhand', stand_type='underhand')
        comp1 = _make_pro(db_session, tournament, 'John Doe', event_ids=[event.id])
        comp2 = _make_pro(db_session, tournament, 'Jane Doe', 'F', event_ids=[event.id])
        # JSON lists both, but table only has comp1
        heat = _make_heat(db_session, event, competitor_ids=[comp1.id, comp2.id])
        _make_heat_assignment(db_session, heat.id, comp1.id)

        from services.preflight import build_preflight_report
        report = build_preflight_report(tournament)

        codes = [i['code'] for i in report['issues']]
        assert 'heat_sync_mismatch' in codes
        mismatch = [i for i in report['issues'] if i['code'] == 'heat_sync_mismatch'][0]
        assert mismatch['severity'] == 'high'
        assert mismatch['autofix'] is True

    def test_extra_in_table_triggers_mismatch(self, db_session, tournament):
        """HeatAssignment has a row that JSON does not list."""
        event = _make_event(db_session, tournament, 'Underhand', stand_type='underhand')
        comp1 = _make_pro(db_session, tournament, 'John Doe', event_ids=[event.id])
        comp2 = _make_pro(db_session, tournament, 'Jane Doe', 'F', event_ids=[event.id])
        # JSON lists only comp1, but table has both
        heat = _make_heat(db_session, event, competitor_ids=[comp1.id])
        _make_heat_assignment(db_session, heat.id, comp1.id)
        _make_heat_assignment(db_session, heat.id, comp2.id)

        from services.preflight import build_preflight_report
        report = build_preflight_report(tournament)

        codes = [i['code'] for i in report['issues']]
        assert 'heat_sync_mismatch' in codes

    def test_multiple_heats_counts_correctly(self, db_session, tournament):
        """Multiple mismatched heats under one event are reported as one issue."""
        event = _make_event(db_session, tournament, 'Standing Block', stand_type='standing_block')
        comp = _make_pro(db_session, tournament, 'Bob', event_ids=[event.id])

        # Heat 1: mismatch (JSON has comp, table empty)
        _make_heat(db_session, event, heat_number=1, competitor_ids=[comp.id])
        # Heat 2: mismatch (JSON has comp, table empty)
        _make_heat(db_session, event, heat_number=2, competitor_ids=[comp.id])

        from services.preflight import build_preflight_report
        report = build_preflight_report(tournament)

        mismatches = [i for i in report['issues'] if i['code'] == 'heat_sync_mismatch']
        assert len(mismatches) == 1
        assert '2 heat(s)' in mismatches[0]['detail']


# ---------------------------------------------------------------------------
# Odd partner pool detection
# ---------------------------------------------------------------------------

class TestOddPartnerPool:
    """Partnered pro events with an odd number of entrants should warn."""

    def test_even_pool_no_issue(self, db_session, tournament):
        """4 entrants in a partnered event — no odd_partner_pool issue."""
        event = _make_event(db_session, tournament, 'Double Buck', gender='M',
                            stand_type='saw_hand', is_partnered=True)
        for i in range(4):
            _make_pro(db_session, tournament, f'Pro {i}', event_ids=[event.id])

        from services.preflight import build_preflight_report
        report = build_preflight_report(tournament)

        codes = [i['code'] for i in report['issues']]
        assert 'odd_partner_pool' not in codes

    def test_odd_pool_triggers_warning(self, db_session, tournament):
        """3 entrants in a partnered event — odd_partner_pool issue raised."""
        event = _make_event(db_session, tournament, 'Double Buck', gender='M',
                            stand_type='saw_hand', is_partnered=True)
        for i in range(3):
            _make_pro(db_session, tournament, f'Pro {i}', event_ids=[event.id])

        from services.preflight import build_preflight_report
        report = build_preflight_report(tournament)

        codes = [i['code'] for i in report['issues']]
        assert 'odd_partner_pool' in codes
        issue = [i for i in report['issues'] if i['code'] == 'odd_partner_pool'][0]
        assert issue['severity'] == 'medium'
        assert '3 entrants' in issue['detail']

    def test_single_entrant_no_issue(self, db_session, tournament):
        """Only 1 entrant — not enough for pairing, so no warning."""
        event = _make_event(db_session, tournament, 'Double Buck', gender='M',
                            stand_type='saw_hand', is_partnered=True)
        _make_pro(db_session, tournament, 'Lonely Pro', event_ids=[event.id])

        from services.preflight import build_preflight_report
        report = build_preflight_report(tournament)

        codes = [i['code'] for i in report['issues']]
        assert 'odd_partner_pool' not in codes

    def test_non_partnered_event_ignored(self, db_session, tournament):
        """Odd entrants in a non-partnered event do not trigger the warning."""
        event = _make_event(db_session, tournament, 'Underhand', gender='M',
                            stand_type='underhand', is_partnered=False)
        for i in range(3):
            _make_pro(db_session, tournament, f'Pro {i}', event_ids=[event.id])

        from services.preflight import build_preflight_report
        report = build_preflight_report(tournament)

        codes = [i['code'] for i in report['issues']]
        assert 'odd_partner_pool' not in codes


# ---------------------------------------------------------------------------
# Saturday overflow detection
# ---------------------------------------------------------------------------

class TestSaturdayOverflow:
    """Spillover college events should be flagged when not integrated into flights."""

    def test_spillover_not_in_flights_triggers_issue(self, db_session, tournament):
        """College event marked as Saturday overflow with heats but no flight assignment."""
        event = _make_event(db_session, tournament, 'Standing Block Speed',
                            event_type='college', gender='M',
                            stand_type='standing_block')
        _make_heat(db_session, event, competitor_ids=[1, 2])
        # Need at least one flight to enter the spillover check branch
        _make_flight(db_session, tournament)

        from services.preflight import build_preflight_report
        report = build_preflight_report(tournament,
                                        saturday_college_event_ids=[event.id])

        codes = [i['code'] for i in report['issues']]
        assert 'spillover_not_in_flights' in codes
        issue = [i for i in report['issues'] if i['code'] == 'spillover_not_in_flights'][0]
        assert issue['severity'] == 'high'
        assert issue['autofix'] is True

    def test_spillover_with_flight_assignment_clean(self, db_session, tournament):
        """College overflow heats already assigned to a flight — no issue."""
        event = _make_event(db_session, tournament, 'Standing Block Speed',
                            event_type='college', gender='M',
                            stand_type='standing_block')
        flight = _make_flight(db_session, tournament)
        _make_heat(db_session, event, competitor_ids=[1, 2], flight_id=flight.id)

        from services.preflight import build_preflight_report
        report = build_preflight_report(tournament,
                                        saturday_college_event_ids=[event.id])

        codes = [i['code'] for i in report['issues']]
        assert 'spillover_not_in_flights' not in codes
        assert 'spillover_missing_heats' not in codes

    def test_spillover_missing_heats(self, db_session, tournament):
        """College overflow event with zero heats — missing heats warning."""
        event = _make_event(db_session, tournament, 'Obstacle Pole',
                            event_type='college', gender='M',
                            stand_type='obstacle_pole')
        _make_flight(db_session, tournament)

        from services.preflight import build_preflight_report
        report = build_preflight_report(tournament,
                                        saturday_college_event_ids=[event.id])

        codes = [i['code'] for i in report['issues']]
        assert 'spillover_missing_heats' in codes

    def test_no_saturday_ids_skips_check(self, db_session, tournament):
        """When saturday_college_event_ids is empty/None, no spillover issues."""
        event = _make_event(db_session, tournament, 'Standing Block Speed',
                            event_type='college', gender='M',
                            stand_type='standing_block')
        _make_heat(db_session, event, competitor_ids=[1, 2])
        _make_flight(db_session, tournament)

        from services.preflight import build_preflight_report
        report = build_preflight_report(tournament, saturday_college_event_ids=None)

        spillover_codes = [i['code'] for i in report['issues']
                           if i['code'].startswith('spillover')]
        assert spillover_codes == []

    def test_no_flights_skips_spillover_check(self, db_session, tournament):
        """When no flights exist at all, the spillover block is skipped."""
        event = _make_event(db_session, tournament, 'Standing Block Speed',
                            event_type='college', gender='M',
                            stand_type='standing_block')
        _make_heat(db_session, event, competitor_ids=[1, 2])

        from services.preflight import build_preflight_report
        report = build_preflight_report(tournament,
                                        saturday_college_event_ids=[event.id])

        spillover_codes = [i['code'] for i in report['issues']
                           if i['code'].startswith('spillover')]
        assert spillover_codes == []


# ---------------------------------------------------------------------------
# Cookie Stack / Standing Block stand conflict
# ---------------------------------------------------------------------------

class TestStandConflict:
    """Cookie Stack and Standing Block share stands — warn when both have heats but no flights."""

    def test_both_have_heats_no_flights_warns(self, db_session, tournament):
        cs_event = _make_event(db_session, tournament, 'Cookie Stack',
                               stand_type='cookie_stack')
        sb_event = _make_event(db_session, tournament, 'Standing Block',
                               stand_type='standing_block')
        _make_heat(db_session, cs_event, competitor_ids=[1])
        _make_heat(db_session, sb_event, competitor_ids=[2])

        from services.preflight import build_preflight_report
        report = build_preflight_report(tournament)

        codes = [i['code'] for i in report['issues']]
        assert 'stand_conflict_no_flights' in codes

    def test_both_have_heats_with_flights_no_warning(self, db_session, tournament):
        cs_event = _make_event(db_session, tournament, 'Cookie Stack',
                               stand_type='cookie_stack')
        sb_event = _make_event(db_session, tournament, 'Standing Block',
                               stand_type='standing_block')
        flight = _make_flight(db_session, tournament)
        _make_heat(db_session, cs_event, competitor_ids=[1], flight_id=flight.id)
        _make_heat(db_session, sb_event, competitor_ids=[2], flight_id=flight.id)

        from services.preflight import build_preflight_report
        report = build_preflight_report(tournament)

        codes = [i['code'] for i in report['issues']]
        assert 'stand_conflict_no_flights' not in codes

    def test_only_one_has_heats_no_warning(self, db_session, tournament):
        _make_event(db_session, tournament, 'Cookie Stack', stand_type='cookie_stack')
        sb_event = _make_event(db_session, tournament, 'Standing Block',
                               stand_type='standing_block')
        _make_heat(db_session, sb_event, competitor_ids=[1])

        from services.preflight import build_preflight_report
        report = build_preflight_report(tournament)

        codes = [i['code'] for i in report['issues']]
        assert 'stand_conflict_no_flights' not in codes


# ---------------------------------------------------------------------------
# Fully valid tournament — no issues
# ---------------------------------------------------------------------------

class TestFullyValidTournament:
    """A well-formed tournament with synced heats, even partner pools, and
    integrated flights should produce a clean preflight report."""

    def test_clean_tournament_passes(self, db_session, tournament):
        # Create a non-partnered event with synced heat
        event = _make_event(db_session, tournament, 'Underhand', gender='M',
                            stand_type='underhand')
        comp1 = _make_pro(db_session, tournament, 'Alice', event_ids=[event.id])
        comp2 = _make_pro(db_session, tournament, 'Bob', event_ids=[event.id])
        heat = _make_heat(db_session, event, competitor_ids=[comp1.id, comp2.id])
        _make_heat_assignment(db_session, heat.id, comp1.id)
        _make_heat_assignment(db_session, heat.id, comp2.id)

        # Create a partnered event with even entrant count
        partnered = _make_event(db_session, tournament, 'Double Buck', gender='M',
                                stand_type='saw_hand', is_partnered=True)
        p1 = _make_pro(db_session, tournament, 'Pro A', event_ids=[partnered.id])
        p2 = _make_pro(db_session, tournament, 'Pro B', event_ids=[partnered.id])
        heat2 = _make_heat(db_session, partnered, competitor_ids=[p1.id, p2.id])
        _make_heat_assignment(db_session, heat2.id, p1.id)
        _make_heat_assignment(db_session, heat2.id, p2.id)

        from services.preflight import build_preflight_report
        report = build_preflight_report(tournament)

        assert report['issue_count'] == 0
        assert report['issues'] == []
        assert report['has_autofixable'] is False
        assert report['severity'] == {'high': 0, 'medium': 0, 'low': 0}

    def test_report_structure(self, db_session, tournament):
        """Verify the returned dict has the expected top-level keys."""
        from services.preflight import build_preflight_report
        report = build_preflight_report(tournament)

        assert 'issue_count' in report
        assert 'issues' in report
        assert 'severity' in report
        assert 'has_autofixable' in report
        assert isinstance(report['issues'], list)
        assert isinstance(report['severity'], dict)
