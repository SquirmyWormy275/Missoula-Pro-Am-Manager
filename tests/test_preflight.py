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

# D12-C commit F2: `seat_roster` was imported here for commit E. Every heat
# this module builds is now rosterless on purpose, so the import went with
# the roster argument. See `_make_heat`.

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
                scoring_type='time', stand_type=None, is_partnered=False,
                is_handicap=False):
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
        is_handicap=is_handicap,
    )
    db_session.add(e)
    db_session.flush()
    return e


def _make_event_result(db_session, event, competitor, status='pending', reviewed=False):
    """Create an active event result for mark-review preflight coverage."""
    from models import EventResult
    from services.time_utils import utc_now_naive

    result = EventResult(
        event_id=event.id,
        competitor_id=competitor.id,
        competitor_type=event.event_type,
        competitor_name=competitor.name,
        status=status,
        mark_assigned_at=utc_now_naive() if reviewed else None,
    )
    db_session.add(result)
    db_session.flush()
    return result


def _make_heat(db_session, event, heat_number=1, run_number=1, flight_id=None,
               flight_position=None):
    """Create and return an empty Heat.

    It used to take a `competitor_ids` list and write it straight into the
    `heats.competitors` column, bypassing the rows, because the thing this
    module tested was `heat_sync_mismatch`: a helper that wrote both stores
    could never build the disagreement the check looked for. D12-C commit F2
    deleted that check, and with it the only reason this helper had to write
    a roster nothing would read.

    Nothing left in `build_preflight_report` reads a heat roster. The
    spillover and stand-conflict checks count heats per event; the pool
    checks read entries off the competitors themselves. So the heats built
    here are empty, which is what they were already worth. The tests that
    genuinely need seated heats add rows through `_make_heat_assignment`.
    """
    from models.heat import Heat
    h = Heat(
        event_id=event.id,
        heat_number=heat_number,
        run_number=run_number,
        flight_id=flight_id,
        flight_position=flight_position,
    )
    db_session.add(h)
    db_session.flush()
    return h


def _make_heat_assignment(db_session, heat_id, competitor_id,
                          competitor_type='pro', stand_number=None, uid=None):
    """Create a HeatAssignment row.

    As of s8a0b2c3d4e5 the row carries a NOT NULL uid with a foreign key onto
    the identity spine, so this cannot be built from the legacy pair alone.
    When `uid` is not given it is resolved from the pair, which is what every
    caller in this file wants: they all pass a competitor they just created.
    Pass `uid` explicitly only to build a row whose uid disagrees with its
    legacy pair, which is drift rather than a normal assignment.
    """
    from models.heat import HeatAssignment
    from services.entity_key import EntityKey, resolve_uid

    if uid is None:
        key = EntityKey.from_legacy(competitor_id, competitor_type)
        uid = resolve_uid(db_session, key)
        assert uid is not None, (
            f'no {competitor_type} competitor with id {competitor_id}; this '
            f'helper builds valid rows, see the uid argument for drift'
        )
    ha = HeatAssignment(
        heat_id=heat_id,
        uid=uid,
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

class TestProPayoutLedgerIntegrity:
    def test_earnings_cache_mismatch_is_reported(self, db_session, tournament):
        from models.event import EventResult
        from services.preflight import build_preflight_report

        event = _make_event(db_session, tournament, 'Paid Underhand')
        competitor = _make_pro(db_session, tournament, 'Ledger Drift')
        competitor.total_earnings = 500.0
        db_session.add(EventResult(
            event_id=event.id,
            competitor_id=competitor.id,
            competitor_type='pro',
            competitor_name=competitor.name,
            payout_amount=300.0,
            status='completed',
        ))
        db_session.flush()

        report = build_preflight_report(tournament)
        mismatch = next(
            issue for issue in report['issues']
            if issue['code'] == 'pro_earnings_cache_mismatch'
        )

        assert mismatch['severity'] == 'high'
        assert mismatch['mismatches'] == [{
            'competitor_name': 'Ledger Drift',
            'ledger_total': 300.0,
            'cached_total': 500.0,
        }]

    def test_matching_earnings_cache_is_not_reported(self, db_session, tournament):
        from models.event import EventResult
        from services.preflight import build_preflight_report

        event = _make_event(db_session, tournament, 'Matching Underhand')
        competitor = _make_pro(db_session, tournament, 'Ledger Match')
        competitor.total_earnings = 300.0
        db_session.add(EventResult(
            event_id=event.id,
            competitor_id=competitor.id,
            competitor_type='pro',
            competitor_name=competitor.name,
            payout_amount=300.0,
            status='completed',
        ))
        db_session.flush()

        report = build_preflight_report(tournament)
        assert 'pro_earnings_cache_mismatch' not in {
            issue['code'] for issue in report['issues']
        }


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
# Heat/table sync mismatch detection stood here.
#
# D12-C commit F2. Four tests, and they were the reason `_make_heat` wrote a
# column-only roster. They drove `heat_sync_mismatch`, which compared
# `heats.competitors` against `heat_assignments` and reported the two stores
# disagreeing as a high-severity autofixable issue.
#
# Commit E made `heat_assignments` the only store a roster is written to or
# read from, so the JSON column cannot disagree with the rows any more than a
# printed copy can disagree with the file it was printed from. A check for
# divergence between one store and a rendering of it has nothing left to find,
# and a preflight blocker that can never fire is a blocker the operator learns
# to ignore. The check, its `heat_sync_mismatch` code, its place in
# `BLOCKING_CODES`, the sync-check and sync-fix routes it fed, and these four
# tests all went in the same commit.
#
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
        _make_heat(db_session, event)
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
        _make_heat(db_session, event, flight_id=flight.id)

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
        _make_heat(db_session, event)
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
        _make_heat(db_session, event)

        from services.preflight import build_preflight_report
        report = build_preflight_report(tournament,
                                        saturday_college_event_ids=[event.id])

        spillover_codes = [i['code'] for i in report['issues']
                           if i['code'].startswith('spillover')]
        assert spillover_codes == []


class TestMandatoryDaySplitPreflight:
    """Mandatory Saturday Run 2 participation is checked without UI selection."""

    @pytest.mark.parametrize('empty_selection', (None, []))
    def test_missing_chokerman_run2_heats_are_blocking_and_actionable(
            self, db_session, tournament, empty_selection):
        event = _make_event(
            db_session, tournament, "Chokerman's Race",
            event_type='college', gender='M', stand_type='chokerman',
        )
        _make_heat(db_session, event, run_number=1)

        from services.preflight import build_preflight_report
        report = build_preflight_report(
            tournament, saturday_college_event_ids=empty_selection,
        )

        issue = next(
            item for item in report['issues']
            if item['code'] == 'chokerman_run2_missing_heats'
        )
        assert issue['severity'] == 'high'
        assert issue in report['blocking']
        assert issue['event_id'] == event.id
        assert issue['expected_run_number'] == 2
        assert 'Generate All Heats' in issue['detail']
        assert 'Run 2' in issue['detail']
        assert report['has_blockers'] is True

    @pytest.mark.parametrize('empty_selection', (None, []))
    def test_wholly_unassigned_chokerman_run2_is_a_participation_blocker(
            self, db_session, tournament, empty_selection):
        event = _make_event(
            db_session, tournament, "Chokerman's Race",
            event_type='college', gender='M', stand_type='chokerman',
        )
        first = _make_heat(db_session, event, heat_number=1, run_number=2)
        second = _make_heat(db_session, event, heat_number=2, run_number=2)
        _make_flight(db_session, tournament)

        from services.preflight import build_preflight_report
        report = build_preflight_report(
            tournament, saturday_college_event_ids=empty_selection,
        )

        issue = next(
            item for item in report['issues']
            if item['code'] == 'chokerman_run2_not_in_flights'
        )
        assert issue in report['blocking']
        assert issue['heat_ids'] == [first.id, second.id]
        assert issue['assigned_heat_ids'] == []
        assert issue['unassigned_heat_ids'] == [first.id, second.id]
        assert 'Integrate College Spillover' in issue['detail']
        assert not any(
            item['code'] == 'chokerman_run2_invalid_closer'
            for item in report['issues']
        )

    @pytest.mark.parametrize('empty_selection', (None, []))
    def test_partially_assigned_chokerman_run2_has_distinct_blocker(
            self, db_session, tournament, empty_selection):
        event = _make_event(
            db_session, tournament, "Chokerman's Race",
            event_type='college', gender='M', stand_type='chokerman',
        )
        flight = _make_flight(db_session, tournament)
        assigned = _make_heat(
            db_session, event, heat_number=1, run_number=2,
            flight_id=flight.id, flight_position=1,
        )
        unassigned = _make_heat(
            db_session, event, heat_number=2, run_number=2,
        )

        from services.preflight import build_preflight_report
        report = build_preflight_report(
            tournament, saturday_college_event_ids=empty_selection,
        )

        issue = next(
            item for item in report['issues']
            if item['code'] == 'chokerman_run2_partially_in_flights'
        )
        assert issue in report['blocking']
        assert issue['heat_ids'] == [assigned.id, unassigned.id]
        assert issue['assigned_heat_ids'] == [assigned.id]
        assert issue['unassigned_heat_ids'] == [unassigned.id]
        assert '1 of 2' in issue['detail']
        assert 'Integrate College Spillover' in issue['detail']
        assert not any(
            item['code'] == 'chokerman_run2_invalid_closer'
            for item in report['issues']
        )

    @pytest.mark.parametrize('empty_selection', (None, []))
    def test_complete_chokerman_run2_must_be_final_suffix_of_last_flight(
            self, db_session, tournament, empty_selection):
        pro_event = _make_event(db_session, tournament, 'Underhand')
        chokerman = _make_event(
            db_session, tournament, "Chokerman's Race",
            event_type='college', gender='M', stand_type='chokerman',
        )
        first_flight = _make_flight(db_session, tournament, flight_number=1)
        last_flight = _make_flight(db_session, tournament, flight_number=2)
        _make_heat(
            db_session, pro_event, heat_number=1,
            flight_id=first_flight.id, flight_position=1,
        )
        closer = _make_heat(
            db_session, chokerman, heat_number=1, run_number=2,
            flight_id=last_flight.id, flight_position=1,
        )
        trailing = _make_heat(
            db_session, pro_event, heat_number=2,
            flight_id=last_flight.id, flight_position=2,
        )

        from services.preflight import build_preflight_report
        report = build_preflight_report(
            tournament, saturday_college_event_ids=empty_selection,
        )

        issue = next(
            item for item in report['issues']
            if item['code'] == 'chokerman_run2_invalid_closer'
        )
        assert issue in report['blocking']
        assert issue['heat_ids'] == [closer.id]
        assert issue['last_flight_id'] == last_flight.id
        assert 'final suffix of the last flight' in issue['detail']
        assert 'Rebuild Flights' in issue['detail']
        assert str(trailing.id) in issue['detail']
        assert not any(
            item['code'] in {
                'chokerman_run2_not_in_flights',
                'chokerman_run2_partially_in_flights',
            }
            for item in report['issues']
        )

    def test_complete_chokerman_suffix_reports_reversed_heat_order(
            self, db_session, tournament):
        chokerman = _make_event(
            db_session, tournament, "Chokerman's Race",
            event_type='college', gender='M', stand_type='chokerman',
        )
        flight = _make_flight(db_session, tournament, flight_number=1)
        heat_two = _make_heat(
            db_session, chokerman, heat_number=2, run_number=2,
            flight_id=flight.id, flight_position=1,
        )
        heat_one = _make_heat(
            db_session, chokerman, heat_number=1, run_number=2,
            flight_id=flight.id, flight_position=2,
        )

        from services.preflight import build_preflight_report
        report = build_preflight_report(
            tournament, saturday_college_event_ids=[],
        )

        issue = next(
            item for item in report['issues']
            if item['code'] == 'chokerman_run2_invalid_closer'
        )
        assert issue in report['blocking']
        assert issue['title'] == "Chokerman's Race Run 2 heats are out of order"
        assert 'heat-number order' in issue['detail']
        assert str(heat_two.id) in issue['detail']
        assert str(heat_one.id) in issue['detail']
        assert issue['wrong_flight_heat_ids'] == []
        assert issue['trailing_heat_ids'] == []

    @pytest.mark.parametrize('empty_selection', (None, []))
    def test_complete_chokerman_final_suffix_is_clean(
            self, db_session, tournament, empty_selection):
        pro_event = _make_event(db_session, tournament, 'Underhand')
        chokerman = _make_event(
            db_session, tournament, "Chokerman's Race",
            event_type='college', gender='M', stand_type='chokerman',
        )
        first_flight = _make_flight(db_session, tournament, flight_number=1)
        last_flight = _make_flight(db_session, tournament, flight_number=2)
        _make_heat(
            db_session, pro_event, heat_number=1,
            flight_id=first_flight.id, flight_position=1,
        )
        _make_heat(
            db_session, pro_event, heat_number=2,
            flight_id=last_flight.id, flight_position=1,
        )
        _make_heat(
            db_session, chokerman, heat_number=1, run_number=2,
            flight_id=last_flight.id, flight_position=2,
        )
        _make_heat(
            db_session, chokerman, heat_number=2, run_number=2,
            flight_id=last_flight.id, flight_position=3,
        )

        from services.preflight import build_preflight_report
        report = build_preflight_report(
            tournament, saturday_college_event_ids=empty_selection,
        )

        assert not any(
            item['code'].startswith('chokerman_run2_')
            for item in report['issues']
        )

    @pytest.mark.parametrize('empty_selection', (None, []))
    def test_speed_climb_run2_is_mandatory_without_explicit_selection(
            self, db_session, tournament, empty_selection):
        speed_climb = _make_event(
            db_session, tournament, 'Speed Climb', event_type='college',
            gender='F', stand_type='speed_climb',
        )
        _make_heat(db_session, speed_climb, run_number=1)
        run_two = _make_heat(
            db_session, speed_climb, heat_number=1, run_number=2,
        )
        _make_flight(db_session, tournament)

        from services.preflight import build_preflight_report
        report = build_preflight_report(
            tournament, saturday_college_event_ids=empty_selection,
        )

        issue = next(
            item for item in report['issues']
            if item['code'] == 'spillover_not_in_flights'
        )
        assert issue['event_id'] == speed_climb.id
        assert issue['unassigned_heat_ids'] == [run_two.id]
        assert '1 heat(s)' in issue['detail']

    @pytest.mark.parametrize('empty_selection', (None, []))
    def test_no_chokerman_event_does_not_create_false_blocker(
            self, db_session, tournament, empty_selection):
        _make_event(db_session, tournament, 'Underhand')

        from services.preflight import build_preflight_report
        report = build_preflight_report(
            tournament, saturday_college_event_ids=empty_selection,
        )

        assert not any(
            item['code'].startswith('chokerman_run2_')
            for item in report['issues']
        )
        assert report['has_blockers'] is False


# ---------------------------------------------------------------------------
# Built flight structure
# ---------------------------------------------------------------------------


class TestBuiltFlightStructure:
    """Malformed persisted flight order blocks spillover integration."""

    def test_missing_and_non_positive_positions_are_blocking_and_actionable(
            self, db_session, tournament):
        event = _make_event(db_session, tournament, 'Underhand')
        flight = _make_flight(db_session, tournament, flight_number=3)
        missing = _make_heat(
            db_session, event, heat_number=1, flight_id=flight.id,
            flight_position=None,
        )
        non_positive = _make_heat(
            db_session, event, heat_number=2, flight_id=flight.id,
            flight_position=0,
        )

        from services.preflight import build_preflight_report
        report = build_preflight_report(tournament)

        issue = next(
            item for item in report['issues']
            if item['code'] == 'invalid_flight_position'
        )
        assert issue['severity'] == 'high'
        assert issue['autofix'] is False
        assert issue in report['blocking']
        assert issue['heats'] == [
            {
                'flight_id': flight.id,
                'flight_number': 3,
                'heat_id': missing.id,
                'event_id': event.id,
                'event_name': 'Underhand',
                'heat_number': 1,
                'run_number': 1,
                'flight_position': None,
                'problem': 'missing',
            },
            {
                'flight_id': flight.id,
                'flight_number': 3,
                'heat_id': non_positive.id,
                'event_id': event.id,
                'event_name': 'Underhand',
                'heat_number': 2,
                'run_number': 1,
                'flight_position': 0,
                'problem': 'non_positive',
            },
        ]
        assert f'Flight 3 heat {missing.id}' in issue['detail']
        assert f'Flight 3 heat {non_positive.id}' in issue['detail']
        assert 'positive unique position' in issue['detail']
        assert report['has_blockers'] is True

    def test_duplicate_positions_are_blocking_with_both_heat_details(
            self, db_session, tournament):
        from sqlalchemy import inspect

        migrated_uniques = {
            constraint['name']
            for constraint in inspect(_db.engine).get_unique_constraints('heats')
        }
        if 'uq_heats_flight_position' in migrated_uniques:
            pytest.skip(
                'The database now rejects duplicate flight positions before '
                'preflight can inspect legacy corruption.'
            )

        event = _make_event(db_session, tournament, 'Single Buck')
        flight = _make_flight(db_session, tournament, flight_number=2)
        first = _make_heat(
            db_session, event, heat_number=4, run_number=1,
            flight_id=flight.id, flight_position=5,
        )
        second = _make_heat(
            db_session, event, heat_number=2, run_number=2,
            flight_id=flight.id, flight_position=5,
        )

        from services.preflight import build_preflight_report
        report = build_preflight_report(tournament)

        issue = next(
            item for item in report['issues']
            if item['code'] == 'duplicate_flight_position'
        )
        assert issue['severity'] == 'high'
        assert issue in report['blocking']
        assert issue['duplicates'] == [{
            'flight_id': flight.id,
            'flight_number': 2,
            'flight_position': 5,
            'heats': (
                {
                    'heat_id': first.id,
                    'event_id': event.id,
                    'event_name': 'Single Buck',
                    'heat_number': 4,
                    'run_number': 1,
                },
                {
                    'heat_id': second.id,
                    'event_id': event.id,
                    'event_name': 'Single Buck',
                    'heat_number': 2,
                    'run_number': 2,
                },
            ),
        }]
        assert f'heats {first.id} and {second.id}' in issue['detail']
        assert 'Flight 2 position 5' in issue['detail']
        assert 'assign each heat a positive unique position' in issue['detail']

# ---------------------------------------------------------------------------
# Physical shared-stand conflicts
# ---------------------------------------------------------------------------

PHYSICAL_STAND_PAIRS = (
    ('cookie_stack', 'standing_block'),
    ('hot_saw', 'stock_saw'),
    ('saw_hand', 'stock_saw'),
    ('obstacle_pole', 'speed_climb'),
)


class TestStandConflict:
    """Preflight distinguishes unbuilt schedules from conflicts already built."""

    @pytest.mark.parametrize(('first_type', 'second_type'), PHYSICAL_STAND_PAIRS)
    def test_configured_pair_with_heats_no_flights_warns(
            self, db_session, tournament, first_type, second_type):
        first = _make_event(
            db_session, tournament, first_type.replace('_', ' ').title(),
            stand_type=first_type,
        )
        second = _make_event(
            db_session, tournament, second_type.replace('_', ' ').title(),
            stand_type=second_type,
        )
        _make_heat(db_session, first)
        _make_heat(db_session, second)

        from services.preflight import build_preflight_report
        report = build_preflight_report(tournament)

        issue = next(
            item for item in report['issues']
            if item['code'] == 'stand_conflict_no_flights'
        )
        assert issue['severity'] == 'medium'
        assert issue['pairs'] == [{
            'stand_types': (first_type, second_type),
            'event_names': ((first.display_name,), (second.display_name,)),
            'required_gap': 8,
        }]
        assert first.display_name in issue['detail']
        assert second.display_name in issue['detail']
        assert 'Rebuild Flights' in issue['detail']
        assert 'required 8-heat gap' in issue['detail']

    def test_unbuilt_reciprocal_pairs_are_reported_once(
            self, db_session, tournament):
        for stand_type in sorted({
                stand_type
                for pair in PHYSICAL_STAND_PAIRS
                for stand_type in pair
        }):
            event = _make_event(
                db_session, tournament, stand_type.replace('_', ' ').title(),
                stand_type=stand_type,
            )
            _make_heat(db_session, event)

        from services.preflight import build_preflight_report
        report = build_preflight_report(tournament)

        issues = [
            item for item in report['issues']
            if item['code'] == 'stand_conflict_no_flights'
        ]
        assert len(issues) == 1
        reported_pairs = [
            frozenset(pair['stand_types']) for pair in issues[0]['pairs']
        ]
        assert len(reported_pairs) == len(set(reported_pairs))
        assert set(reported_pairs) == {
            frozenset(pair) for pair in PHYSICAL_STAND_PAIRS
        }

    def test_built_conflicting_flight_warns_with_actionable_details(
            self, db_session, tournament):
        cs_event = _make_event(db_session, tournament, 'Cookie Stack',
                               stand_type='cookie_stack')
        sb_event = _make_event(db_session, tournament, 'Standing Block',
                               stand_type='standing_block')
        flight = _make_flight(db_session, tournament)
        cs_heat = _make_heat(
            db_session, cs_event, flight_id=flight.id, flight_position=1,
        )
        sb_heat = _make_heat(
            db_session, sb_event, flight_id=flight.id, flight_position=2,
        )

        from services.preflight import build_preflight_report
        report = build_preflight_report(tournament)

        issue = next(
            item for item in report['issues']
            if item['code'] == 'stand_conflict_built_flights'
        )
        assert issue['severity'] == 'medium'
        assert issue['autofix'] is False
        assert issue not in report['blocking']
        assert issue['conflicts'] == [{
            'heat_ids': (cs_heat.id, sb_heat.id),
            'stand_types': ('cookie_stack', 'standing_block'),
            'events': ('Cookie Stack', 'Standing Block'),
            'heat_numbers': (1, 1),
            'run_numbers': (1, 1),
            'flight_numbers': (1, 1),
            'flight_positions': (1, 2),
            'gap': 1,
            'required_gap': 8,
        }]
        assert 'Cookie Stack Heat 1' in issue['detail']
        assert 'Standing Block Heat 1' in issue['detail']
        assert 'current gap 1' in issue['detail']
        assert 'required gap 8' in issue['detail']

    @pytest.mark.parametrize(('first_type', 'second_type'), PHYSICAL_STAND_PAIRS)
    def test_built_flights_detect_every_configured_physical_stand_pair(
            self, db_session, tournament, first_type, second_type):
        first = _make_event(
            db_session, tournament, first_type.replace('_', ' ').title(),
            stand_type=first_type,
        )
        second = _make_event(
            db_session, tournament, second_type.replace('_', ' ').title(),
            stand_type=second_type,
        )
        flight = _make_flight(db_session, tournament)
        _make_heat(
            db_session, first, flight_id=flight.id, flight_position=1,
        )
        _make_heat(
            db_session, second, flight_id=flight.id, flight_position=2,
        )

        from services.preflight import build_preflight_report
        report = build_preflight_report(tournament)

        issue = next(
            item for item in report['issues']
            if item['code'] == 'stand_conflict_built_flights'
        )
        detected_pairs = {
            frozenset(conflict['stand_types'])
            for conflict in issue['conflicts']
        }
        assert frozenset((first_type, second_type)) in detected_pairs

    def test_pair_coverage_matches_shared_flight_builder_configuration(self):
        from services.flight_builder import _CONFLICTING_STANDS

        configured_pairs = {
            frozenset((stand_type, conflict_type))
            for stand_type, conflict_types in _CONFLICTING_STANDS.items()
            for conflict_type in conflict_types
        }
        assert configured_pairs == {
            frozenset(pair) for pair in PHYSICAL_STAND_PAIRS
        }

    def test_conflict_free_built_flight_has_no_built_conflict_warning(
            self, db_session, tournament):
        cs_event = _make_event(
            db_session, tournament, 'Cookie Stack', stand_type='cookie_stack',
        )
        neutral_event = _make_event(
            db_session, tournament, 'Underhand', stand_type='underhand',
        )
        sb_event = _make_event(
            db_session, tournament, 'Standing Block',
            stand_type='standing_block',
        )
        flight = _make_flight(db_session, tournament)
        _make_heat(
            db_session, cs_event, flight_id=flight.id, flight_position=1,
        )
        for position in range(2, 9):
            _make_heat(
                db_session, neutral_event, heat_number=position - 1,
                flight_id=flight.id, flight_position=position,
            )
        _make_heat(
            db_session, sb_event, flight_id=flight.id, flight_position=9,
        )

        from services.preflight import build_preflight_report
        report = build_preflight_report(tournament)

        codes = {item['code'] for item in report['issues']}
        assert 'stand_conflict_built_flights' not in codes

    def test_built_diagnostics_fetch_flight_heats_in_one_query(
            self, db_session, tournament):
        from sqlalchemy import event as sqlalchemy_event

        event = _make_event(
            db_session, tournament, 'Underhand', stand_type='underhand',
        )
        for flight_number in range(1, 4):
            flight = _make_flight(
                db_session, tournament, flight_number=flight_number,
            )
            _make_heat(
                db_session, event, heat_number=flight_number,
                flight_id=flight.id, flight_position=1,
            )

        heat_selects = []

        def capture_heat_select(_connection, _cursor, statement, *args):
            normalized = ' '.join(statement.lower().split())
            if (
                    normalized.startswith('select')
                    and ' from heats ' in f' {normalized} '
            ):
                heat_selects.append(normalized)

        sqlalchemy_event.listen(
            _db.engine, 'before_cursor_execute', capture_heat_select,
        )
        try:
            from services.preflight import build_preflight_report
            build_preflight_report(tournament)
        finally:
            sqlalchemy_event.remove(
                _db.engine, 'before_cursor_execute', capture_heat_select,
            )

        assert len(heat_selects) == 1

    def test_batched_built_heat_order_is_global_and_deterministic(
            self, db_session, tournament, monkeypatch):
        event = _make_event(
            db_session, tournament, 'Underhand', stand_type='underhand',
        )
        flight_two = _make_flight(db_session, tournament, flight_number=2)
        flight_one = _make_flight(db_session, tournament, flight_number=1)

        position_two = _make_heat(
            db_session, event, heat_number=1, flight_id=flight_one.id,
            flight_position=2,
        )
        null_position = _make_heat(
            db_session, event, heat_number=2, flight_id=flight_one.id,
            flight_position=None,
        )
        position_one_first = _make_heat(
            db_session, event, heat_number=3, flight_id=flight_one.id,
            flight_position=1,
        )
        position_three = _make_heat(
            db_session, event, heat_number=4, flight_id=flight_one.id,
            flight_position=3,
        )
        second_flight_heat = _make_heat(
            db_session, event, heat_number=5, flight_id=flight_two.id,
            flight_position=1,
        )

        captured_order = []

        def capture_order(ordered_heats):
            captured_order.extend(heat.id for heat in ordered_heats)
            return []

        from services import flight_builder
        monkeypatch.setattr(
            flight_builder, 'find_stand_conflicts', capture_order,
        )

        from services.preflight import build_preflight_report
        build_preflight_report(tournament)

        assert captured_order == [
            position_one_first.id,
            position_two.id,
            position_three.id,
            null_position.id,
            second_flight_heat.id,
        ]

    def test_only_one_has_heats_no_warning(self, db_session, tournament):
        _make_event(db_session, tournament, 'Cookie Stack', stand_type='cookie_stack')
        sb_event = _make_event(db_session, tournament, 'Standing Block',
                               stand_type='standing_block')
        _make_heat(db_session, sb_event)

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
        heat = _make_heat(db_session, event)
        _make_heat_assignment(db_session, heat.id, comp1.id)
        _make_heat_assignment(db_session, heat.id, comp2.id)

        # Create a partnered event with even entrant count
        partnered = _make_event(db_session, tournament, 'Double Buck', gender='M',
                                stand_type='saw_hand', is_partnered=True)
        p1 = _make_pro(db_session, tournament, 'Pro A', event_ids=[partnered.id])
        p2 = _make_pro(db_session, tournament, 'Pro B', event_ids=[partnered.id])
        p1.partners = json.dumps({str(partnered.id): p2.name})
        p2.partners = json.dumps({str(partnered.id): p1.name})
        heat2 = _make_heat(db_session, partnered)
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


class TestHandicapMarkReview:
    """Late entrants must not be indistinguishable from scratch marks."""

    def test_unreviewed_handicap_entry_is_high_priority_issue(self, db_session, tournament):
        event = _make_event(
            db_session, tournament, 'Handicap Underhand', gender='M',
            stand_type='underhand', is_handicap=True,
        )
        competitor = _make_pro(db_session, tournament, 'Late Entry', event_ids=[event.id])
        _make_event_result(db_session, event, competitor, reviewed=False)

        from services.preflight import build_preflight_report
        report = build_preflight_report(tournament)

        issue = next(i for i in report['issues'] if i['code'] == 'handicap_marks_unreviewed')
        assert issue['severity'] == 'high'
        assert issue['unreviewed_marks'][0]['competitor_name'] == 'Late Entry'

    def test_reviewed_scratch_mark_is_not_reported_as_missing(self, db_session, tournament):
        event = _make_event(
            db_session, tournament, 'Handicap Underhand', gender='M',
            stand_type='underhand', is_handicap=True,
        )
        competitor = _make_pro(db_session, tournament, 'Intentional Scratch', event_ids=[event.id])
        result = _make_event_result(db_session, event, competitor, reviewed=True)
        result.handicap_factor = 0.0

        from services.preflight import build_preflight_report
        report = build_preflight_report(tournament)

        assert 'handicap_marks_unreviewed' not in {i['code'] for i in report['issues']}


# ---------------------------------------------------------------------------
# Gear-sharing preflight — USING vs SHARING semantics (V2.9.1 + follow-up fix)
# ---------------------------------------------------------------------------

class TestGearSharingUsingPrefix:
    """Regression tests for the gear-sharing preflight after V2.9.1 introduced
    the ``using:`` value prefix. Prior to the follow-up fix, every legitimate
    USING entry was reported as an unknown partner because the preflight
    normalized the whole value (``using:Alice`` -> ``usingalice``) instead of
    the underlying name."""

    def _codes(self, report):
        return {i['code'] for i in report['issues']}

    def test_using_prefix_resolves_to_known_partner(self, db_session, tournament):
        """A USING entry whose underlying name matches a roster competitor
        must NOT be flagged as unknown."""
        from services.preflight import build_preflight_report

        jj = _make_event(db_session, tournament, 'Jack & Jill Sawing',
                         stand_type='saw_hand', is_partnered=True)
        alice = _make_pro(db_session, tournament, 'Alice Jones',
                          gender='F', event_ids=[jj.id])
        bob = _make_pro(db_session, tournament, 'Bob Smith',
                        gender='M', event_ids=[jj.id])

        alice.gear_sharing = json.dumps({str(jj.id): 'using:Bob Smith'})
        alice.partners = json.dumps({str(jj.id): 'Bob Smith'})
        bob.gear_sharing = json.dumps({str(jj.id): 'using:Alice Jones'})
        bob.partners = json.dumps({str(jj.id): 'Alice Jones'})
        db_session.flush()

        report = build_preflight_report(tournament)
        codes = self._codes(report)
        assert 'gear_unknown_partner_names' not in codes
        assert 'gear_partner_mismatch' not in codes

    def test_sharing_entry_different_from_partner_is_not_flagged(
            self, db_session, tournament):
        """A SHARING entry intentionally names a DIFFERENT person than the
        event partner — that is the entire point of cross-competitor gear
        dependency outside a partnered pair. The preflight must not flag it
        as a 'gear vs partner disagreement'."""
        from services.preflight import build_preflight_report

        db = _make_event(db_session, tournament, 'Double Buck', gender='M',
                         stand_type='saw_hand', is_partnered=True)
        ripley = _make_pro(db_session, tournament, 'Ripley Orr',
                           gender='M', event_ids=[db.id])
        cody = _make_pro(db_session, tournament, 'Cody Labahn',
                         gender='M', event_ids=[db.id])
        may = _make_pro(db_session, tournament, 'May Brown',
                        gender='M', event_ids=[db.id])

        # Ripley is partnered with Cody for Double Buck, but shares his saw
        # with May (a cross-competitor SHARING dependency, no using: prefix).
        ripley.gear_sharing = json.dumps({str(db.id): 'May Brown'})
        ripley.partners = json.dumps({str(db.id): 'Cody Labahn'})
        db_session.flush()

        report = build_preflight_report(tournament)
        codes = self._codes(report)
        assert 'gear_partner_mismatch' not in codes

    def test_using_entry_still_flagged_when_name_unknown(
            self, db_session, tournament):
        """The prefix fix must not mask a GENUINE unresolved partner — a
        USING entry whose underlying name is not on the roster should still
        trigger the unknown-partner warning."""
        from services.preflight import build_preflight_report

        jj = _make_event(db_session, tournament, 'Jack & Jill Sawing',
                         stand_type='saw_hand', is_partnered=True)
        alice = _make_pro(db_session, tournament, 'Alice Jones',
                          gender='F', event_ids=[jj.id])

        alice.gear_sharing = json.dumps({str(jj.id): 'using:Ghost Competitor'})
        db_session.flush()

        report = build_preflight_report(tournament)
        codes = self._codes(report)
        assert 'gear_unknown_partner_names' in codes

    def test_using_mismatch_with_partners_still_flagged(
            self, db_session, tournament):
        """When a USING entry names a different person than the partners
        dict, that IS a real inconsistency (confirmation drifted away from
        the registered partner) and must still be flagged."""
        from services.preflight import build_preflight_report

        jj = _make_event(db_session, tournament, 'Jack & Jill Sawing',
                         stand_type='saw_hand', is_partnered=True)
        alice = _make_pro(db_session, tournament, 'Alice Jones',
                          gender='F', event_ids=[jj.id])
        _bob = _make_pro(db_session, tournament, 'Bob Smith',
                         gender='M', event_ids=[jj.id])
        _carol = _make_pro(db_session, tournament, 'Carol Vance',
                           gender='F', event_ids=[jj.id])

        alice.gear_sharing = json.dumps({str(jj.id): 'using:Bob Smith'})
        alice.partners = json.dumps({str(jj.id): 'Carol Vance'})
        db_session.flush()

        report = build_preflight_report(tournament)
        codes = self._codes(report)
        assert 'gear_partner_mismatch' in codes


class TestCleanupNonEnrolledGearEntries:
    """Regression tests for services.gear_sharing.cleanup_non_enrolled_gear_entries."""

    def test_removes_entries_for_non_enrolled_events(
            self, db_session, tournament):
        """Gear entries pointing at events the competitor is not enrolled in
        should be removed; entries for events they ARE in stay."""
        from services.gear_sharing import cleanup_non_enrolled_gear_entries

        enrolled = _make_event(db_session, tournament, 'Single Buck',
                               stand_type='saw_hand')
        orphan = _make_event(db_session, tournament, 'Double Buck',
                             stand_type='saw_hand', is_partnered=True)
        chrissy = _make_pro(db_session, tournament, 'Chrissy Marcellus',
                            gender='F', event_ids=[enrolled.id])

        chrissy.gear_sharing = json.dumps({
            str(enrolled.id): 'Alice Jones',
            str(orphan.id): 'Cody Labahn',
        })
        db_session.flush()

        result = cleanup_non_enrolled_gear_entries(tournament)

        assert result['cleaned'] == 1
        assert 'Chrissy Marcellus' in result['affected']
        remaining = json.loads(chrissy.gear_sharing)
        assert str(enrolled.id) in remaining
        assert str(orphan.id) not in remaining

    def test_keeps_category_entries_when_enrolled_in_matching_event(
            self, db_session, tournament):
        """A category key should be kept when the competitor is enrolled in
        any event of that category, even if not all of them."""
        from services.gear_sharing import cleanup_non_enrolled_gear_entries

        sb = _make_event(db_session, tournament, 'Single Buck',
                         stand_type='saw_hand')
        alice = _make_pro(db_session, tournament, 'Alice Jones',
                          gender='F', event_ids=[sb.id])

        alice.gear_sharing = json.dumps({'category:crosscut': 'Bob Smith'})
        db_session.flush()

        result = cleanup_non_enrolled_gear_entries(tournament)

        assert result['cleaned'] == 0
        remaining = json.loads(alice.gear_sharing)
        assert 'category:crosscut' in remaining


# ---------------------------------------------------------------------------
# BLOCKING_CODES helper
# ---------------------------------------------------------------------------


class TestBlockingCodes:
    """V2.14.16: hard-blocker codes are surfaced in report['blocking'].

    DOMAIN_CONTRACT (2026-04-27): unresolved partners, self-references, and
    non-reciprocal pairs are not allowed to slip into the schedule.
    Generation enforces this; the preflight dashboard surfaces the same set
    as a red banner with click-path fixes.

    The contract also named heat sync mismatches. D12-C commit F2 removed
    that member: with one roster store there is nothing for it to be out of
    sync with. The other three are untouched.
    """

    def test_blocking_codes_constant_includes_partner_invariants(self):
        from services.preflight import BLOCKING_CODES

        assert 'unresolved_partner_name' in BLOCKING_CODES
        assert 'self_reference_partner' in BLOCKING_CODES
        assert 'non_reciprocal_partnership' in BLOCKING_CODES
        assert 'invalid_partner_gender' in BLOCKING_CODES
        assert 'invalid_flight_position' in BLOCKING_CODES
        assert 'duplicate_flight_position' in BLOCKING_CODES
        assert 'duplicate_flight_number' in BLOCKING_CODES
        assert 'chokerman_run2_missing_heats' in BLOCKING_CODES
        assert 'chokerman_run2_not_in_flights' in BLOCKING_CODES
        assert 'chokerman_run2_partially_in_flights' in BLOCKING_CODES
        assert 'chokerman_run2_invalid_closer' in BLOCKING_CODES
        assert 'heat_sync_mismatch' not in BLOCKING_CODES

    def test_get_blocking_issues_filters_advisory(self):
        from services.preflight import get_blocking_issues

        report = {
            'issues': [
                {'code': 'odd_partner_pool', 'severity': 'medium'},
                {'code': 'unresolved_partner_name', 'severity': 'high'},
                {'code': 'non_reciprocal_partnership', 'severity': 'high'},
                {'code': 'gear_partner_mismatch', 'severity': 'low'},
            ],
        }
        blocking = get_blocking_issues(report)

        assert len(blocking) == 3
        codes = {b['code'] for b in blocking}
        assert codes == {
            'unresolved_partner_name',
            'non_reciprocal_partnership',
            'gear_partner_mismatch',
        }

    def test_clean_tournament_has_no_blockers(self, db_session, tournament):
        from services.preflight import build_preflight_report

        report = build_preflight_report(tournament)

        assert report['has_blockers'] is False
        assert report['blocking'] == []
