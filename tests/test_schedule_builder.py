"""
Schedule builder tests — Friday college day, Saturday pro show, Friday Feature,
empty tournament, and data structure validation.

Covers:
    - build_day_schedule() return structure and key presence
    - Friday day block: college events ordered correctly
    - Saturday show block: pro events from flights and event-order fallback
    - Empty tournament (no events at all)
    - Friday Feature extraction (collegiate 1-board, pro 1-board / 3-board jigger)
    - Chokerman's Race mandatory Saturday run 2

Run:
    pytest tests/test_schedule_builder.py -v

Requirements:
    pytest (pip install pytest)
    All app dependencies installed.
"""
import pytest
from database import db as _db


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope='module')
def app():
    """Create a test Flask app with in-memory SQLite."""
    import os
    os.environ.setdefault('SECRET_KEY', 'test-secret-schedule')
    os.environ.setdefault('WTF_CSRF_ENABLED', 'False')

    from app import create_app
    _app = create_app()
    _app.config.update({
        'TESTING': True,
        'SQLALCHEMY_DATABASE_URI': 'sqlite:///:memory:',
        'WTF_CSRF_ENABLED': False,
        'WTF_CSRF_CHECK_DEFAULT': False,
        'SERVER_NAME': None,
    })

    with _app.app_context():
        _db.create_all()
        yield _app
        _db.session.remove()
        # _db.drop_all() � skipped; in-memory SQLite is discarded on exit


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
    t = Tournament(name='Schedule Test 2026', year=2026, status='setup')
    db_session.add(t)
    db_session.flush()
    return t


def _make_event(db_session, tournament, name, event_type, gender=None,
                scoring_type='time', stand_type=None, requires_dual_runs=False,
                is_open=False):
    """Helper: create an Event and flush to get an id."""
    from models import Event
    e = Event(
        tournament_id=tournament.id,
        name=name,
        event_type=event_type,
        gender=gender,
        scoring_type=scoring_type,
        stand_type=stand_type or 'general',
        requires_dual_runs=requires_dual_runs,
        is_open=is_open,
    )
    db_session.add(e)
    db_session.flush()
    return e


def _make_flight(db_session, tournament, flight_number):
    """Helper: create a Flight."""
    from models.heat import Flight
    f = Flight(
        tournament_id=tournament.id,
        flight_number=flight_number,
    )
    db_session.add(f)
    db_session.flush()
    return f


def _make_heat(db_session, event, heat_number, run_number=1, flight=None, flight_position=None):
    """Helper: create a Heat, optionally assigned to a flight."""
    from models import Heat
    h = Heat(
        event_id=event.id,
        heat_number=heat_number,
        run_number=run_number,
    )
    if flight:
        h.flight_id = flight.id
        h.flight_position = flight_position
    db_session.add(h)
    db_session.flush()
    return h


# ---------------------------------------------------------------------------
# Seed data fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def college_events(db_session, tournament):
    """Create a set of college events for Friday scheduling."""
    events = {}
    for name, gender, st, scoring, dual, is_open in [
        ('Axe Throw',              'M', 'axe_throw',      'score', False, True),
        ('Axe Throw',              'F', 'axe_throw',      'score', False, True),
        ('Underhand Hard Hit',     'M', 'underhand',      'hits',  False, False),
        ('Underhand Speed',        'M', 'underhand',      'time',  False, False),
        ('Standing Block Speed',   'M', 'standing_block',  'time',  False, False),
        ('Standing Block Speed',   'F', 'standing_block',  'time',  False, False),
        ('Single Buck',            'M', 'saw_hand',       'time',  False, False),
        ("Chokerman's Race",       'M', 'chokerman',      'time',  True,  False),
        ('1-Board Springboard',    'M', 'springboard',     'time',  False, False),
        ('Birling',                'M', 'birling',         'bracket', False, False),
    ]:
        e = _make_event(db_session, tournament, name, 'college', gender,
                        scoring_type=scoring, stand_type=st,
                        requires_dual_runs=dual, is_open=is_open)
        key = f"{name}_{gender}"
        events[key] = e
    return events


@pytest.fixture()
def pro_events(db_session, tournament):
    """Create a set of pro events for Saturday scheduling."""
    events = {}
    for name, gender, st in [
        ('Springboard',    None, 'springboard'),
        ('Underhand',      'M', 'underhand'),
        ('Standing Block', 'M', 'standing_block'),
        ('Stock Saw',      'M', 'stock_saw'),
        ('Hot Saw',        None, 'hot_saw'),
        ('Single Buck',    'M', 'saw_hand'),
        ('Pro 1-Board',    None, 'springboard'),
        ('3-Board Jigger', None, 'springboard'),
    ]:
        e = _make_event(db_session, tournament, name, 'pro', gender, stand_type=st)
        key = f"{name}_{gender}"
        events[key] = e
    return events


# ---------------------------------------------------------------------------
# TestBuildDayScheduleStructure — validate return dict shape
# ---------------------------------------------------------------------------

class TestBuildDayScheduleStructure:
    """Verify build_day_schedule returns the expected top-level keys and types."""

    def test_top_level_keys(self, db_session, tournament, college_events, pro_events):
        from services.schedule_builder import build_day_schedule
        result = build_day_schedule(tournament)

        assert isinstance(result, dict)
        expected_keys = {'friday_day', 'friday_feature', 'saturday_show', 'saturday_source'}
        assert set(result.keys()) == expected_keys

    def test_friday_day_is_list_of_dicts(self, db_session, tournament, college_events, pro_events):
        from services.schedule_builder import build_day_schedule
        result = build_day_schedule(tournament)

        assert isinstance(result['friday_day'], list)
        for entry in result['friday_day']:
            assert isinstance(entry, dict)

    def test_saturday_source_is_string(self, db_session, tournament, college_events, pro_events):
        from services.schedule_builder import build_day_schedule
        result = build_day_schedule(tournament)

        assert isinstance(result['saturday_source'], str)
        assert result['saturday_source'] in ('flights', 'events')

    def test_schedule_entry_keys(self, db_session, tournament, college_events, pro_events):
        """Each schedule entry dict must have slot, event_id, label, event_type, stand_type."""
        from services.schedule_builder import build_day_schedule
        result = build_day_schedule(tournament)

        required_keys = {'slot', 'event_id', 'label', 'event_type', 'stand_type'}
        for block_name in ('friday_day', 'friday_feature', 'saturday_show'):
            for entry in result[block_name]:
                assert required_keys.issubset(entry.keys()), (
                    f"Missing keys in {block_name}: {required_keys - set(entry.keys())}"
                )

    def test_slot_numbers_are_sequential(self, db_session, tournament, college_events, pro_events):
        from services.schedule_builder import build_day_schedule
        result = build_day_schedule(tournament)

        for block_name in ('friday_day', 'friday_feature', 'saturday_show'):
            entries = result[block_name]
            if entries:
                slots = [e['slot'] for e in entries]
                assert slots == list(range(1, len(entries) + 1)), (
                    f"Slots in {block_name} not sequential: {slots}"
                )

    def test_event_ids_are_integers(self, db_session, tournament, college_events, pro_events):
        from services.schedule_builder import build_day_schedule
        result = build_day_schedule(tournament)

        for block_name in ('friday_day', 'friday_feature', 'saturday_show'):
            for entry in result[block_name]:
                assert isinstance(entry['event_id'], int)


# ---------------------------------------------------------------------------
# TestFridayCollegeSchedule — Friday day block
# ---------------------------------------------------------------------------

class TestFridayCollegeSchedule:
    """Verify Friday day block contains college events in expected order."""

    def test_friday_day_contains_college_events(self, db_session, tournament, college_events, pro_events):
        from services.schedule_builder import build_day_schedule
        result = build_day_schedule(tournament)

        friday_types = {e['event_type'] for e in result['friday_day']}
        assert friday_types == {'college'}, "Friday day should only contain college events"

    def test_open_events_run_first(self, db_session, tournament, college_events, pro_events):
        """OPEN events (Axe Throw) should appear before CLOSED events."""
        from services.schedule_builder import build_day_schedule
        result = build_day_schedule(tournament)

        friday = result['friday_day']
        if not friday:
            pytest.skip("No Friday day events generated")

        # Axe Throw events are OPEN and should be at the start
        axe_labels = [e for e in friday if 'Axe Throw' in e['label']]
        non_axe_labels = [e for e in friday if 'Axe Throw' not in e['label'] and 'Birling' not in e['label']]
        if axe_labels and non_axe_labels:
            assert axe_labels[0]['slot'] < non_axe_labels[0]['slot']

    def test_birling_runs_last(self, db_session, tournament, college_events, pro_events):
        """Birling should be the last event in the Friday day block."""
        from services.schedule_builder import build_day_schedule
        result = build_day_schedule(tournament)

        friday = result['friday_day']
        birling_entries = [e for e in friday if 'Birling' in e['label']]
        if birling_entries:
            max_slot = max(e['slot'] for e in friday)
            birling_slot = birling_entries[0]['slot']
            assert birling_slot == max_slot, "Birling should have the highest slot number"

    def test_1board_springboard_extracted_to_feature(self, db_session, tournament, college_events, pro_events):
        """Collegiate 1-Board Springboard should be in friday_feature, not friday_day."""
        from services.schedule_builder import build_day_schedule
        result = build_day_schedule(tournament)

        friday_day_names = {e['label'] for e in result['friday_day']}
        friday_feature_names = {e['label'] for e in result['friday_feature']}

        assert not any('1-Board Springboard' in n for n in friday_day_names), \
            "1-Board Springboard should not be in friday_day"
        assert any('1-Board Springboard' in n for n in friday_feature_names), \
            "1-Board Springboard should be in friday_feature"

    def test_chokerman_in_friday_day(self, db_session, tournament, college_events, pro_events):
        """Chokerman's Race should appear in the Friday day block."""
        from services.schedule_builder import build_day_schedule
        result = build_day_schedule(tournament)

        chokerman_entries = [e for e in result['friday_day'] if "Chokerman" in e['label']]
        assert len(chokerman_entries) >= 1


# ---------------------------------------------------------------------------
# TestSaturdayProSchedule — Saturday show block (event-order fallback)
# ---------------------------------------------------------------------------

class TestSaturdayProSchedule:
    """Verify Saturday show block with no flights (event-order fallback)."""

    def test_saturday_source_events_when_no_flights(self, db_session, tournament, college_events, pro_events):
        from services.schedule_builder import build_day_schedule
        result = build_day_schedule(tournament)

        assert result['saturday_source'] == 'events'

    def test_saturday_contains_pro_events(self, db_session, tournament, college_events, pro_events):
        from services.schedule_builder import build_day_schedule
        result = build_day_schedule(tournament)

        sat_types = {e['event_type'] for e in result['saturday_show']}
        # Saturday show is built from pro events (and maybe college spillover)
        assert 'pro' in sat_types

    def test_chokerman_run2_appended(self, db_session, tournament, college_events, pro_events):
        """Chokerman's Race Run 2 should be appended to the Saturday show."""
        from services.schedule_builder import build_day_schedule
        result = build_day_schedule(tournament)

        saturday = result['saturday_show']
        chokerman_entries = [e for e in saturday if "Chokerman" in e['label'] and "Run 2" in e['label']]
        assert len(chokerman_entries) == 1, "Saturday should have Chokerman's Race Run 2"

    def test_chokerman_run2_is_last(self, db_session, tournament, college_events, pro_events):
        """Chokerman's Race Run 2 should be at the end of Saturday."""
        from services.schedule_builder import build_day_schedule
        result = build_day_schedule(tournament)

        saturday = result['saturday_show']
        if not saturday:
            pytest.skip("No Saturday events")
        last_entry = saturday[-1]
        assert "Chokerman" in last_entry['label'] and "Run 2" in last_entry['label']

    def test_friday_feature_pro_excluded_from_saturday(self, db_session, tournament, college_events, pro_events):
        """Pro events auto-extracted for Friday Feature (Pro 1-Board, 3-Board Jigger)
        should not appear in the Saturday show block."""
        from services.schedule_builder import build_day_schedule
        result = build_day_schedule(tournament)

        feature_ids = {e['event_id'] for e in result['friday_feature'] if e['event_type'] == 'pro'}
        saturday_ids = {e['event_id'] for e in result['saturday_show']}
        overlap = feature_ids & saturday_ids
        assert not overlap, f"Feature pro events leaked into Saturday: {overlap}"


# ---------------------------------------------------------------------------
# TestSaturdayFlightSchedule — Saturday show built from flights
# ---------------------------------------------------------------------------

class TestSaturdayFlightSchedule:
    """Verify Saturday show block when flights and heats exist."""

    def test_saturday_source_flights(self, db_session, tournament, college_events, pro_events):
        """When flights with heats exist, saturday_source should be 'flights'."""
        # Create a flight with heats for pro events
        spring = pro_events['Springboard_None']
        uh = pro_events['Underhand_M']

        flight = _make_flight(db_session, tournament, 1)
        _make_heat(db_session, spring, 1, flight=flight, flight_position=1)
        _make_heat(db_session, uh, 1, flight=flight, flight_position=2)

        from services.schedule_builder import build_day_schedule
        result = build_day_schedule(tournament)

        assert result['saturday_source'] == 'flights'

    def test_flight_entries_have_flight_keys(self, db_session, tournament, college_events, pro_events):
        """Flight-sourced entries should have flight_number and heat_id keys."""
        spring = pro_events['Springboard_None']
        flight = _make_flight(db_session, tournament, 1)
        _make_heat(db_session, spring, 1, flight=flight, flight_position=1)

        from services.schedule_builder import build_day_schedule
        result = build_day_schedule(tournament)

        for entry in result['saturday_show']:
            if 'flight_number' in entry:
                assert 'heat_id' in entry
                assert isinstance(entry['flight_number'], int)
                assert isinstance(entry['heat_id'], int)

    def test_flight_label_format(self, db_session, tournament, college_events, pro_events):
        """Labels should follow 'Flight N: EventName - Heat M' format."""
        spring = pro_events['Springboard_None']
        flight = _make_flight(db_session, tournament, 1)
        _make_heat(db_session, spring, 1, flight=flight, flight_position=1)

        from services.schedule_builder import build_day_schedule
        result = build_day_schedule(tournament)

        flight_entries = [e for e in result['saturday_show'] if 'flight_number' in e]
        assert len(flight_entries) >= 1
        label = flight_entries[0]['label']
        assert 'Flight 1:' in label
        assert 'Heat 1' in label

    def test_multiple_flights_ordered(self, db_session, tournament, college_events, pro_events):
        """Heats from flight 1 should appear before heats from flight 2."""
        spring = pro_events['Springboard_None']
        uh = pro_events['Underhand_M']

        f1 = _make_flight(db_session, tournament, 1)
        _make_heat(db_session, spring, 1, flight=f1, flight_position=1)

        f2 = _make_flight(db_session, tournament, 2)
        _make_heat(db_session, uh, 1, flight=f2, flight_position=1)

        from services.schedule_builder import build_day_schedule
        result = build_day_schedule(tournament)

        saturday = result['saturday_show']
        flight_entries = [e for e in saturday if 'flight_number' in e]
        flight_nums = [e['flight_number'] for e in flight_entries]
        assert flight_nums == sorted(flight_nums), "Flights should be in ascending order"


# ---------------------------------------------------------------------------
# TestEmptyTournament — no events at all
# ---------------------------------------------------------------------------

class TestEmptyTournament:
    """Verify build_day_schedule handles a tournament with zero events."""

    def test_empty_returns_valid_structure(self, db_session, tournament):
        from services.schedule_builder import build_day_schedule
        result = build_day_schedule(tournament)

        assert isinstance(result, dict)
        assert 'friday_day' in result
        assert 'friday_feature' in result
        assert 'saturday_show' in result
        assert 'saturday_source' in result

    def test_empty_all_blocks_empty(self, db_session, tournament):
        from services.schedule_builder import build_day_schedule
        result = build_day_schedule(tournament)

        assert result['friday_day'] == []
        assert result['friday_feature'] == []
        assert result['saturday_show'] == []

    def test_empty_saturday_source_is_events(self, db_session, tournament):
        from services.schedule_builder import build_day_schedule
        result = build_day_schedule(tournament)

        assert result['saturday_source'] == 'events'


# ---------------------------------------------------------------------------
# TestFridayFeature — Friday Night Feature events
# ---------------------------------------------------------------------------

class TestFridayFeature:
    """Verify Friday Feature block extraction and ordering."""

    def test_feature_contains_pro_1board_and_jigger(self, db_session, tournament, college_events, pro_events):
        """Pro 1-Board and 3-Board Jigger should auto-populate Friday Feature."""
        from services.schedule_builder import build_day_schedule
        result = build_day_schedule(tournament)

        feature_labels = [e['label'] for e in result['friday_feature']]
        has_pro_1board = any('Pro 1-Board' in lbl for lbl in feature_labels)
        has_jigger = any('3-Board Jigger' in lbl for lbl in feature_labels)
        assert has_pro_1board, "Pro 1-Board should be in Friday Feature"
        assert has_jigger, "3-Board Jigger should be in Friday Feature"

    def test_feature_contains_college_1board(self, db_session, tournament, college_events, pro_events):
        """Collegiate 1-Board Springboard should be extracted to Friday Feature."""
        from services.schedule_builder import build_day_schedule
        result = build_day_schedule(tournament)

        feature_labels = [e['label'] for e in result['friday_feature']]
        assert any('1-Board Springboard' in lbl for lbl in feature_labels)

    def test_explicit_friday_pro_ids_override_default(self, db_session, tournament, college_events, pro_events):
        """Passing explicit friday_pro_event_ids overrides the auto-feature selection."""
        # Only put Underhand in the Friday Feature
        uh = pro_events['Underhand_M']

        from services.schedule_builder import build_day_schedule
        result = build_day_schedule(tournament, friday_pro_event_ids=[uh.id])

        feature_pro_ids = {e['event_id'] for e in result['friday_feature'] if e['event_type'] == 'pro'}
        assert uh.id in feature_pro_ids

        # Pro 1-Board and 3-Board Jigger should now be in saturday_show, not feature
        pro_1board = pro_events['Pro 1-Board_None']
        jigger = pro_events['3-Board Jigger_None']
        saturday_ids = {e['event_id'] for e in result['saturday_show']}
        assert pro_1board.id in saturday_ids or pro_1board.id not in feature_pro_ids
        assert jigger.id in saturday_ids or jigger.id not in feature_pro_ids

    def test_feature_is_list(self, db_session, tournament, college_events, pro_events):
        from services.schedule_builder import build_day_schedule
        result = build_day_schedule(tournament)

        assert isinstance(result['friday_feature'], list)

    def test_feature_entries_have_required_keys(self, db_session, tournament, college_events, pro_events):
        from services.schedule_builder import build_day_schedule
        result = build_day_schedule(tournament)

        required_keys = {'slot', 'event_id', 'label', 'event_type', 'stand_type'}
        for entry in result['friday_feature']:
            assert required_keys.issubset(entry.keys())


# ---------------------------------------------------------------------------
# TestCollegeSpillover — Saturday college events
# ---------------------------------------------------------------------------

class TestCollegeSpillover:
    """Verify college events designated for Saturday appear in saturday_show."""

    def test_spillover_events_in_saturday(self, db_session, tournament, college_events, pro_events):
        """College events passed as saturday_college_event_ids appear in Saturday show."""
        sb_m = college_events['Standing Block Speed_M']

        from services.schedule_builder import build_day_schedule
        result = build_day_schedule(tournament, saturday_college_event_ids=[sb_m.id])

        saturday_ids = {e['event_id'] for e in result['saturday_show']}
        assert sb_m.id in saturday_ids

    def test_spillover_removed_from_friday(self, db_session, tournament, college_events, pro_events):
        """College events moved to Saturday should not appear in Friday day block."""
        sb_m = college_events['Standing Block Speed_M']

        from services.schedule_builder import build_day_schedule
        result = build_day_schedule(tournament, saturday_college_event_ids=[sb_m.id])

        friday_ids = {e['event_id'] for e in result['friday_day']}
        assert sb_m.id not in friday_ids
