"""
Virtual Woodboss tests — block/saw counting, name-based event resolution,
lottery view, and import event-ID mapping.

Covers the fix for gendered pro events (e.g. "Women's Standing Block") that
were stored as name strings instead of integer event IDs during Excel import,
causing _count_competitors() to silently skip them.

Run:
    pytest tests/test_woodboss.py -v

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
    t = Tournament(name='Wood Test 2026', year=2026, status='setup')
    db_session.add(t)
    db_session.flush()
    return t


@pytest.fixture()
def pro_events(db_session, tournament):
    """Create a standard set of gendered + open pro events.

    Returns a dict of descriptive keys to Event objects:
        uh_m, uh_f, sb_m, sb_f, spring, hot_saw
    """
    from models import Event

    events = {}
    for name, gender, st in [
        ('Underhand',      'M', 'underhand'),
        ('Underhand',      'F', 'underhand'),
        ('Standing Block', 'M', 'standing_block'),
        ('Standing Block', 'F', 'standing_block'),
        ('Springboard',    None, 'springboard'),
        ('Hot Saw',        None, 'hot_saw'),
        ('Single Buck',    'M', 'saw_hand'),
        ('Single Buck',    'F', 'saw_hand'),
        ('Stock Saw',      'M', 'stock_saw'),
        ('Stock Saw',      'F', 'stock_saw'),
    ]:
        e = Event(
            tournament_id=tournament.id,
            name=name,
            event_type='pro',
            gender=gender,
            scoring_type='time',
            stand_type=st,
        )
        db_session.add(e)

    db_session.flush()

    # Build a keyed lookup
    all_events = Event.query.filter_by(
        tournament_id=tournament.id, event_type='pro'
    ).all()
    for e in all_events:
        if e.name == 'Underhand' and e.gender == 'M':
            events['uh_m'] = e
        elif e.name == 'Underhand' and e.gender == 'F':
            events['uh_f'] = e
        elif e.name == 'Standing Block' and e.gender == 'M':
            events['sb_m'] = e
        elif e.name == 'Standing Block' and e.gender == 'F':
            events['sb_f'] = e
        elif e.name == 'Springboard':
            events['spring'] = e
        elif e.name == 'Hot Saw':
            events['hot_saw'] = e
        elif e.name == 'Single Buck' and e.gender == 'M':
            events['singlebuck_m'] = e
        elif e.name == 'Single Buck' and e.gender == 'F':
            events['singlebuck_f'] = e
        elif e.name == 'Stock Saw' and e.gender == 'M':
            events['stocksaw_m'] = e
        elif e.name == 'Stock Saw' and e.gender == 'F':
            events['stocksaw_f'] = e

    return events


def _make_pro(db_session, tournament, name, gender, event_ids):
    """Helper: create an active ProCompetitor with given events_entered."""
    from models import ProCompetitor
    c = ProCompetitor(
        tournament_id=tournament.id,
        name=name,
        gender=gender,
        status='active',
    )
    c.set_events_entered(event_ids)
    db_session.add(c)
    db_session.flush()
    return c


def _make_college(db_session, tournament, team, name, gender, event_names):
    """Helper: create an active CollegeCompetitor with event names."""
    from models import CollegeCompetitor
    c = CollegeCompetitor(
        tournament_id=tournament.id,
        team_id=team.id,
        name=name,
        gender=gender,
        status='active',
    )
    c.set_events_entered(event_names)
    db_session.add(c)
    db_session.flush()
    return c


# ---------------------------------------------------------------------------
# _count_competitors — ID-based pro event resolution
# ---------------------------------------------------------------------------

class TestCountCompetitorsIdBased:
    """Pro competitors with integer event IDs (normal registration path)."""

    def test_pro_men_underhand_counted(self, db_session, tournament, pro_events):
        _make_pro(db_session, tournament, 'John Doe', 'M', [pro_events['uh_m'].id])
        _make_pro(db_session, tournament, 'Jim Smith', 'M', [pro_events['uh_m'].id])

        from services.woodboss import _count_competitors
        counts = _count_competitors(tournament.id)

        assert counts[('underhand', 'pro', 'M')] == 2

    def test_pro_women_underhand_counted(self, db_session, tournament, pro_events):
        _make_pro(db_session, tournament, 'Jane Doe', 'F', [pro_events['uh_f'].id])
        _make_pro(db_session, tournament, 'Jill Smith', 'F', [pro_events['uh_f'].id])
        _make_pro(db_session, tournament, 'Amy Jones', 'F', [pro_events['uh_f'].id])

        from services.woodboss import _count_competitors
        counts = _count_competitors(tournament.id)

        assert counts[('underhand', 'pro', 'F')] == 3

    def test_pro_women_standing_block_counted(self, db_session, tournament, pro_events):
        _make_pro(db_session, tournament, 'Jane Doe', 'F', [pro_events['sb_f'].id])

        from services.woodboss import _count_competitors
        counts = _count_competitors(tournament.id)

        assert counts[('standing block', 'pro', 'F')] == 1

    def test_open_gender_event_uses_competitor_gender(self, db_session, tournament, pro_events):
        """Springboard is open gender — uses competitor's own gender."""
        _make_pro(db_session, tournament, 'John Doe', 'M', [pro_events['spring'].id])
        _make_pro(db_session, tournament, 'Jane Doe', 'F', [pro_events['spring'].id])

        from services.woodboss import _count_competitors
        counts = _count_competitors(tournament.id)

        # Both counted under 'springboard' — gender from competitor, not event
        assert counts[('springboard', 'pro', 'M')] == 1
        assert counts[('springboard', 'pro', 'F')] == 1

    def test_multiple_events_per_competitor(self, db_session, tournament, pro_events):
        """A competitor entered in 3 events generates 3 separate counts."""
        _make_pro(db_session, tournament, 'Jane Doe', 'F', [
            pro_events['uh_f'].id,
            pro_events['sb_f'].id,
            pro_events['spring'].id,
        ])

        from services.woodboss import _count_competitors
        counts = _count_competitors(tournament.id)

        assert counts[('underhand', 'pro', 'F')] == 1
        assert counts[('standing block', 'pro', 'F')] == 1
        assert counts[('springboard', 'pro', 'F')] == 1

    def test_scratched_competitors_excluded(self, db_session, tournament, pro_events):
        """Scratched competitors must not contribute to block counts."""
        from models import ProCompetitor
        c = ProCompetitor(
            tournament_id=tournament.id,
            name='Scratched Sally',
            gender='F',
            status='scratched',
        )
        c.set_events_entered([pro_events['uh_f'].id])
        db_session.add(c)
        db_session.flush()

        from services.woodboss import _count_competitors
        counts = _count_competitors(tournament.id)

        assert counts[('underhand', 'pro', 'F')] == 0


# ---------------------------------------------------------------------------
# _count_competitors — name-string fallback (legacy import path)
# ---------------------------------------------------------------------------

class TestCountCompetitorsNameFallback:
    """Pro competitors whose events_entered contains name strings instead of IDs.

    This simulates the bug where the Excel importer stored gendered display
    names (e.g. "Women's Standing Block") because event_by_name only indexed
    by Event.name ("Standing Block"), not Event.display_name.
    """

    def test_display_name_string_resolves(self, db_session, tournament, pro_events):
        """'Women's Standing Block' should resolve to the F Standing Block event."""
        _make_pro(db_session, tournament, 'Jane Import', 'F', ["Women's Standing Block"])

        from services.woodboss import _count_competitors
        counts = _count_competitors(tournament.id)

        assert counts[('standing block', 'pro', 'F')] == 1

    def test_mens_display_name_string_resolves(self, db_session, tournament, pro_events):
        """'Men's Underhand' should resolve to the M Underhand event."""
        _make_pro(db_session, tournament, 'John Import', 'M', ["Men's Underhand"])

        from services.woodboss import _count_competitors
        counts = _count_competitors(tournament.id)

        assert counts[('underhand', 'pro', 'M')] == 1

    def test_mixed_ids_and_names(self, db_session, tournament, pro_events):
        """A competitor with a mix of integer IDs and name strings."""
        _make_pro(db_session, tournament, 'Mixed Mary', 'F', [
            pro_events['spring'].id,       # integer ID — normal
            "Women's Standing Block",       # name string — legacy import
        ])

        from services.woodboss import _count_competitors
        counts = _count_competitors(tournament.id)

        assert counts[('springboard', 'pro', 'F')] == 1
        assert counts[('standing block', 'pro', 'F')] == 1

    def test_raw_name_without_gender_prefix(self, db_session, tournament, pro_events):
        """'Standing Block' (no gender prefix) resolves — picks one event.

        When the raw name 'Standing Block' matches both M and F events (same
        Event.name), the name_map returns whichever was indexed last. The test
        just verifies it resolves to *something* rather than being silently
        skipped (the old buggy behavior).
        """
        _make_pro(db_session, tournament, 'Ambiguous Al', 'M', ["Standing Block"])

        from services.woodboss import _count_competitors
        counts = _count_competitors(tournament.id)

        # At minimum, one standing block count should exist (M or F)
        total_sb = (
            counts.get(('standing block', 'pro', 'M'), 0) +
            counts.get(('standing block', 'pro', 'F'), 0)
        )
        assert total_sb == 1

    def test_bogus_name_silently_skipped(self, db_session, tournament, pro_events):
        """An unrecognisable event entry is silently skipped, not an error."""
        _make_pro(db_session, tournament, 'Bad Data Bob', 'M', ["Nonexistent Event XYZ"])

        from services.woodboss import _count_competitors
        counts = _count_competitors(tournament.id)

        # No counts should be generated for the bogus entry
        assert sum(counts.values()) == 0

    def test_all_gendered_display_names(self, db_session, tournament, pro_events):
        """Every gendered pro event display name resolves correctly."""
        _make_pro(db_session, tournament, 'Pro W1', 'F', [
            "Women's Underhand",
            "Women's Standing Block",
            "Women's Single Buck",
            "Women's Stock Saw",
        ])
        _make_pro(db_session, tournament, 'Pro M1', 'M', [
            "Men's Underhand",
            "Men's Standing Block",
            "Men's Single Buck",
            "Men's Stock Saw",
        ])

        from services.woodboss import _count_competitors
        counts = _count_competitors(tournament.id)

        assert counts[('underhand', 'pro', 'F')] == 1
        assert counts[('underhand', 'pro', 'M')] == 1
        assert counts[('standing block', 'pro', 'F')] == 1
        assert counts[('standing block', 'pro', 'M')] == 1
        assert counts[('single buck', 'pro', 'F')] == 1
        assert counts[('single buck', 'pro', 'M')] == 1
        assert counts[('stock saw', 'pro', 'F')] == 1
        assert counts[('stock saw', 'pro', 'M')] == 1


# ---------------------------------------------------------------------------
# calculate_blocks — end-to-end block counts
# ---------------------------------------------------------------------------

class TestCalculateBlocks:
    """Verify calculate_blocks produces correct per-config_key block counts."""

    def test_pro_women_blocks_appear(self, db_session, tournament, pro_events):
        """The original bug: pro women chopping blocks missing from report."""
        _make_pro(db_session, tournament, 'Jane A', 'F', [pro_events['uh_f'].id])
        _make_pro(db_session, tournament, 'Jane B', 'F', [pro_events['sb_f'].id])
        _make_pro(db_session, tournament, 'Jane C', 'F', [
            pro_events['uh_f'].id, pro_events['sb_f'].id,
        ])

        from services.woodboss import calculate_blocks
        blocks = calculate_blocks(tournament.id)

        by_key = {b['config_key']: b for b in blocks}
        assert by_key['block_underhand_pro_F']['competitor_count'] == 2
        assert by_key['block_standing_pro_F']['competitor_count'] == 2

    def test_pro_women_blocks_from_name_strings(self, db_session, tournament, pro_events):
        """Imported name strings should produce block counts."""
        _make_pro(db_session, tournament, 'Import Jane', 'F', ["Women's Standing Block"])

        from services.woodboss import calculate_blocks
        blocks = calculate_blocks(tournament.id)

        by_key = {b['config_key']: b for b in blocks}
        assert by_key['block_standing_pro_F']['competitor_count'] == 1

    def test_pro_men_and_women_separate(self, db_session, tournament, pro_events):
        """Men's and women's blocks are distinct config keys."""
        _make_pro(db_session, tournament, 'Bob', 'M', [pro_events['uh_m'].id])
        _make_pro(db_session, tournament, 'Sue', 'F', [pro_events['uh_f'].id])

        from services.woodboss import calculate_blocks
        blocks = calculate_blocks(tournament.id)

        by_key = {b['config_key']: b for b in blocks}
        assert by_key['block_underhand_pro_M']['competitor_count'] == 1
        assert by_key['block_underhand_pro_F']['competitor_count'] == 1

    def test_open_gender_springboard_accumulates(self, db_session, tournament, pro_events):
        """Men and women in open-gender Springboard share one config key."""
        _make_pro(db_session, tournament, 'Bob', 'M', [pro_events['spring'].id])
        _make_pro(db_session, tournament, 'Sue', 'F', [pro_events['spring'].id])

        from services.woodboss import calculate_blocks
        blocks = calculate_blocks(tournament.id)

        by_key = {b['config_key']: b for b in blocks}
        assert by_key['block_springboard_pro']['competitor_count'] == 2

    def test_relay_blocks_use_count_override(self, db_session, tournament, pro_events):
        """Relay blocks come from count_override, not enrollment."""
        from models import WoodConfig
        wc = WoodConfig(
            tournament_id=tournament.id,
            config_key='block_relay_underhand',
            species='Cottonwood',
            size_value=14,
            count_override=3,
        )
        db_session.add(wc)
        db_session.flush()

        from services.woodboss import calculate_blocks
        blocks = calculate_blocks(tournament.id)

        by_key = {b['config_key']: b for b in blocks}
        assert by_key['block_relay_underhand']['competitor_count'] == 3
        assert by_key['block_relay_underhand']['is_manual'] is True

    def test_zero_enrollment_not_in_report_view(self, db_session, tournament, pro_events):
        """Config keys with zero count should exist in data but report template filters them."""
        from services.woodboss import calculate_blocks
        blocks = calculate_blocks(tournament.id)

        # All BLOCK_CONFIG_LABELS keys should be present
        keys = {b['config_key'] for b in blocks}
        assert 'block_underhand_pro_F' in keys
        assert 'block_standing_pro_F' in keys

        # But they should have count 0
        by_key = {b['config_key']: b for b in blocks}
        assert by_key['block_underhand_pro_F']['competitor_count'] == 0


# ---------------------------------------------------------------------------
# _list_competitors — lottery view event resolution
# ---------------------------------------------------------------------------

class TestListCompetitors:
    """Verify _list_competitors resolves events for the lottery view."""

    def test_id_based_events_resolved(self, db_session, tournament, pro_events):
        _make_pro(db_session, tournament, 'Jane', 'F', [
            pro_events['uh_f'].id, pro_events['sb_f'].id,
        ])

        from services.woodboss import _list_competitors
        comps = _list_competitors(tournament.id)

        pro_comps = [c for c in comps if c['comp_type'] == 'pro']
        assert len(pro_comps) == 1
        assert 'Underhand' in pro_comps[0]['events']
        assert 'Standing Block' in pro_comps[0]['events']

    def test_name_string_events_resolved(self, db_session, tournament, pro_events):
        """Legacy name strings should also resolve in the lottery view."""
        _make_pro(db_session, tournament, 'Import Jane', 'F', ["Women's Standing Block"])

        from services.woodboss import _list_competitors
        comps = _list_competitors(tournament.id)

        pro_comps = [c for c in comps if c['comp_type'] == 'pro']
        assert len(pro_comps) == 1
        assert 'Standing Block' in pro_comps[0]['events']

    def test_bogus_event_excluded(self, db_session, tournament, pro_events):
        """Unrecognisable events are excluded from the list, not errors."""
        _make_pro(db_session, tournament, 'Bad Bob', 'M', ["Bogus Event"])

        from services.woodboss import _list_competitors
        comps = _list_competitors(tournament.id)

        pro_comps = [c for c in comps if c['comp_type'] == 'pro']
        assert len(pro_comps) == 1
        assert pro_comps[0]['events'] == []


# ---------------------------------------------------------------------------
# College competitor counting (name-based, no ID lookup)
# ---------------------------------------------------------------------------

class TestCollegeCompetitorCounting:
    """College competitors use event names directly — no ID resolution needed."""

    def test_college_events_counted(self, db_session, tournament):
        from models import Event, Team
        team = Team(
            tournament_id=tournament.id,
            team_code='UM-A',
            school_name='University of Montana',
            school_abbreviation='UM',
        )
        db_session.add(team)
        db_session.flush()

        # Create college events — capture by (name, gender) so we can pass IDs
        # to _make_college below.  CollegeCompetitor.events_entered stores event
        # IDs, not names; _count_competitors() resolves them via the Event table.
        events_by_key = {}
        for name, gender in [
            ('Underhand Hard Hit', 'M'),
            ('Underhand Hard Hit', 'F'),
            ('Underhand Speed', 'M'),
            ('Underhand Speed', 'F'),
        ]:
            e = Event(
                tournament_id=tournament.id, name=name, event_type='college',
                gender=gender, scoring_type='hits' if 'Hit' in name else 'time',
                stand_type='underhand',
            )
            db_session.add(e)
            events_by_key[(name, gender)] = e
        db_session.flush()

        alice_events = [
            str(events_by_key[('Underhand Hard Hit', 'F')].id),
            str(events_by_key[('Underhand Speed', 'F')].id),
        ]
        bob_events = [str(events_by_key[('Underhand Hard Hit', 'M')].id)]
        _make_college(db_session, tournament, team, 'Alice', 'F', alice_events)
        _make_college(db_session, tournament, team, 'Bob', 'M', bob_events)

        from services.woodboss import _count_competitors
        counts = _count_competitors(tournament.id)

        # Both "Underhand Hard Hit" and "Underhand Speed" contain 'underhand'
        assert counts[('underhand hard hit', 'college', 'F')] == 1
        assert counts[('underhand speed', 'college', 'F')] == 1
        assert counts[('underhand hard hit', 'college', 'M')] == 1


# ---------------------------------------------------------------------------
# Import route — event_by_name index
# ---------------------------------------------------------------------------

class TestImportEventByNameIndex:
    """Verify the import route's event_by_name dict indexes display names."""

    def test_display_name_indexed(self, db_session, tournament, pro_events):
        """Both Event.name and Event.display_name should be indexed."""
        from models import Event
        pro_ev = Event.query.filter_by(
            tournament_id=tournament.id, event_type='pro'
        ).all()

        # Simulate the fixed import route logic
        event_by_name = {}
        for e in pro_ev:
            event_by_name[e.name.strip()] = e
            event_by_name[e.display_name.strip()] = e

        # Raw names resolve
        assert 'Standing Block' in event_by_name
        assert 'Underhand' in event_by_name

        # Gendered display names resolve
        assert "Women's Standing Block" in event_by_name
        assert "Men's Standing Block" in event_by_name
        assert "Women's Underhand" in event_by_name
        assert "Men's Underhand" in event_by_name

        # Gendered display names resolve to the correct gender
        assert event_by_name["Women's Standing Block"].gender == 'F'
        assert event_by_name["Men's Standing Block"].gender == 'M'
        assert event_by_name["Women's Underhand"].gender == 'F'
        assert event_by_name["Men's Underhand"].gender == 'M'

    def test_open_gender_display_name(self, db_session, tournament, pro_events):
        """Open-gender events have no prefix — name and display_name are identical."""
        from models import Event
        spring = Event.query.filter_by(
            tournament_id=tournament.id, name='Springboard', event_type='pro'
        ).first()

        assert spring.display_name == 'Springboard'

    def test_event_id_stored_for_gendered_import(self, db_session, tournament, pro_events):
        """Simulated import: gendered name resolves to integer ID, not name string."""
        from models import Event
        pro_ev = Event.query.filter_by(
            tournament_id=tournament.id, event_type='pro'
        ).all()

        event_by_name = {}
        for e in pro_ev:
            event_by_name[e.name.strip()] = e
            event_by_name[e.display_name.strip()] = e

        # Simulate what the import route does per event entry
        event_name = "Women's Standing Block"
        ev = event_by_name.get(event_name)
        stored_value = ev.id if ev else event_name

        # Should be an integer ID, not a name string
        assert isinstance(stored_value, int)
        assert ev.gender == 'F'


# ---------------------------------------------------------------------------
# get_wood_report — full report integration
# ---------------------------------------------------------------------------

class TestWoodReport:
    """Integration tests for the full wood report."""

    def test_report_includes_pro_women_blocks(self, db_session, tournament, pro_events):
        _make_pro(db_session, tournament, 'Jane Chopper', 'F', [
            pro_events['uh_f'].id,
            pro_events['sb_f'].id,
        ])

        from services.woodboss import get_wood_report
        report = get_wood_report(tournament.id)

        assert report['total_blocks'] >= 2
        by_key = {b['config_key']: b for b in report['blocks']}
        assert by_key['block_underhand_pro_F']['competitor_count'] == 1
        assert by_key['block_standing_pro_F']['competitor_count'] == 1

    def test_report_total_blocks_accurate(self, db_session, tournament, pro_events):
        _make_pro(db_session, tournament, 'Bob', 'M', [pro_events['uh_m'].id])
        _make_pro(db_session, tournament, 'Sue', 'F', [pro_events['uh_f'].id])
        _make_pro(db_session, tournament, 'Jim', 'M', [pro_events['spring'].id])

        from services.woodboss import get_wood_report
        report = get_wood_report(tournament.id)

        assert report['total_blocks'] == 3

    def test_empty_tournament_report(self, db_session, tournament, pro_events):
        """No competitors enrolled — report returns zero counts, no errors."""
        from services.woodboss import get_wood_report
        report = get_wood_report(tournament.id)

        assert report['total_blocks'] == 0
        assert report['total_saw_inches'] == 0.0
        assert isinstance(report['blocks'], list)
        assert len(report['blocks']) > 0  # all config_keys present, just zero


# ---------------------------------------------------------------------------
# Regression: college events_entered stores NAMES, not IDs
# ---------------------------------------------------------------------------

class TestCollegeEnrollmentByName:
    """Guards the V2.8.2 root-cause fix. College registration + the Excel
    importer store event NAMES (e.g. "Underhand Hard Hit") in
    events_entered, not IDs. Prior woodboss code built an ID-only map,
    silently dropped everything, and the Wood Count Report showed zero
    college blocks the day before block turning. This test MUST use names
    as stored in the real DB — do not "simplify" it to pass IDs.
    """

    def _seed_team_and_events(self, db_session, tournament):
        from models import Event, Team
        team = Team(
            tournament_id=tournament.id,
            team_code='UM-A',
            school_name='University of Montana',
            school_abbreviation='UM',
        )
        db_session.add(team)
        db_session.flush()
        for name, gender, st, scoring in [
            ('Underhand Hard Hit', 'M', 'underhand', 'hits'),
            ('Underhand Hard Hit', 'F', 'underhand', 'hits'),
            ('Standing Block Speed', 'M', 'standing_block', 'time'),
            ('Standing Block Speed', 'F', 'standing_block', 'time'),
            ('Single Buck', 'M', 'saw_hand', 'time'),
            ('Single Buck', 'F', 'saw_hand', 'time'),
        ]:
            db_session.add(Event(
                tournament_id=tournament.id, name=name, event_type='college',
                gender=gender, scoring_type=scoring, stand_type=st,
            ))
        db_session.flush()
        return team

    def test_count_competitors_resolves_by_name(self, db_session, tournament):
        team = self._seed_team_and_events(db_session, tournament)
        # Store event NAMES exactly as college registration does.
        _make_college(db_session, tournament, team, 'Alice', 'F',
                      ['Underhand Hard Hit', 'Standing Block Speed'])
        _make_college(db_session, tournament, team, 'Bob', 'M',
                      ['Underhand Hard Hit', 'Single Buck'])

        from services.woodboss import _count_competitors
        counts = _count_competitors(tournament.id)

        # H0 regression — these were all zero before the fix.
        assert counts[('underhand hard hit', 'college', 'F')] == 1
        assert counts[('underhand hard hit', 'college', 'M')] == 1
        assert counts[('standing block speed', 'college', 'F')] == 1
        assert counts[('single buck', 'college', 'M')] == 1

    def test_wood_report_includes_college_blocks_from_names(self, db_session, tournament):
        team = self._seed_team_and_events(db_session, tournament)
        _make_college(db_session, tournament, team, 'Alice', 'F',
                      ['Underhand Hard Hit'])
        _make_college(db_session, tournament, team, 'Bob', 'M',
                      ['Underhand Hard Hit', 'Standing Block Speed'])

        from services.woodboss import get_wood_report
        report = get_wood_report(tournament.id)
        by_key = {b['config_key']: b for b in report['blocks']}
        # H0 — these keys all reported 0 in V2.8.1 and earlier.
        assert by_key['block_underhand_college_F']['competitor_count'] == 1
        assert by_key['block_underhand_college_M']['competitor_count'] == 1
        assert by_key['block_standing_college_M']['competitor_count'] == 1

    def test_closed_event_count_uses_names(self, db_session, tournament):
        """CollegeCompetitor.closed_event_count built a set of event IDs
        then compared against names — silently returned 0 for everyone,
        meaning 6-CLOSED-events enforcement never ran. Fixed V2.8.2."""
        from models import CollegeCompetitor, Event, Team
        team = Team(tournament_id=tournament.id, team_code='UM-B',
                    school_name='U Montana', school_abbreviation='UM')
        db_session.add(team)
        db_session.flush()
        for name in ['Underhand Hard Hit', 'Axe Throw']:
            db_session.add(Event(
                tournament_id=tournament.id, name=name, event_type='college',
                gender='M', scoring_type='hits', stand_type='underhand',
                is_open=(name == 'Axe Throw'),
            ))
        db_session.flush()

        c = CollegeCompetitor(
            tournament_id=tournament.id, team_id=team.id, name='Eve',
            gender='F', status='active',
        )
        c.set_events_entered(['Underhand Hard Hit', 'Axe Throw'])
        db_session.add(c)
        db_session.flush()
        # Underhand Hard Hit is CLOSED, Axe Throw is OPEN
        assert c.closed_event_count == 1


# ---------------------------------------------------------------------------
# Wood preset save / apply / delete roundtrip
# ---------------------------------------------------------------------------

@pytest.fixture()
def tmp_preset_file(monkeypatch, tmp_path):
    """Redirect _preset_path to a temp file so tests never touch
    instance/wood_presets.json."""
    import services.woodboss as wb
    tmp_file = tmp_path / 'wood_presets.json'
    monkeypatch.setattr(wb, '_preset_path', lambda: str(tmp_file))
    return tmp_file


class TestPresetRoundtrip:
    """Covers H1 (apply_preset must not wipe existing diameters),
    M1 (per-cfg_key block specs), M2 (log_relay_doublebuck in presets),
    L4 (built-in name collision), plus basic save/load/delete."""

    def test_save_and_load_roundtrip(self, tmp_preset_file):
        from services.woodboss import get_all_presets, save_custom_preset
        save_custom_preset('my-2026', {'blocks': {'species': 'Western White Pine',
                                                  'size_value': 13.0, 'size_unit': 'in'}})
        presets = get_all_presets()
        assert 'my-2026' in presets
        assert presets['my-2026']['blocks']['species'] == 'Western White Pine'

    def test_delete_custom_preset(self, tmp_preset_file):
        from services.woodboss import delete_custom_preset, get_all_presets, save_custom_preset
        save_custom_preset('disposable', {'blocks': {'species': 'Pine'}})
        assert 'disposable' in get_all_presets()
        delete_custom_preset('disposable')
        assert 'disposable' not in get_all_presets()

    def test_save_rejects_builtin_name_collision(self, tmp_preset_file):
        """L4 — saving under a built-in preset name must fail loudly."""
        import config as app_config
        from services.woodboss import save_custom_preset
        if not app_config.WOOD_PRESETS:
            pytest.skip('no built-in presets configured')
        builtin_name = next(iter(app_config.WOOD_PRESETS.keys()))
        with pytest.raises(ValueError, match='built-in'):
            save_custom_preset(builtin_name, {'blocks': {'species': 'Pine'}})

    def test_apply_preset_does_not_wipe_existing_diameter(self, tmp_preset_file, db_session, tournament):
        """H1 — applying a species-only preset must preserve existing size_value.

        Previously apply_preset wrote `None` through whenever the preset
        key was present with a None value. Users who saved a preset from
        a form with no diameter then applied it to a new tournament saw
        every diameter wiped.
        """
        from models.event import Event
        from models.wood_config import WoodConfig
        # Set up an event so _active_block_keys() accepts the block_key.
        db_session.add(Event(
            tournament_id=tournament.id, name='Underhand Hard Hit',
            event_type='college', gender='M', scoring_type='hits',
            stand_type='underhand',
        ))
        # Seed an existing config with a known diameter.
        db_session.add(WoodConfig(
            tournament_id=tournament.id,
            config_key='block_underhand_college_M',
            species='Douglas Fir',
            size_value=14.0,
            size_unit='in',
        ))
        db_session.flush()

        from services.woodboss import apply_preset, save_custom_preset
        # Preset carries species only — diameter is None.
        save_custom_preset('species-only', {
            'blocks': {'species': 'Western White Pine',
                       'size_value': None, 'size_unit': 'in'},
        })
        apply_preset(tournament.id, 'species-only')

        row = WoodConfig.query.filter_by(
            tournament_id=tournament.id,
            config_key='block_underhand_college_M',
        ).first()
        assert row is not None
        assert row.species == 'Western White Pine'
        # H1 regression — size_value must NOT have been nulled.
        assert row.size_value == 14.0, 'apply_preset wiped existing diameter'

    def test_apply_preset_per_cfg_key_support(self, tmp_preset_file, db_session, tournament):
        """M1 — blocks_by_key gives different species to different categories."""
        from models.event import Event
        from models.wood_config import WoodConfig
        for name, gender in [('Underhand Hard Hit', 'M'),
                             ('Standing Block Speed', 'M')]:
            db_session.add(Event(
                tournament_id=tournament.id, name=name, event_type='college',
                gender=gender, scoring_type='time',
                stand_type='underhand' if 'Underhand' in name else 'standing_block',
            ))
        db_session.flush()

        from services.woodboss import apply_preset, save_custom_preset
        save_custom_preset('per-key', {
            'blocks_by_key': {
                'block_underhand_college_M': {
                    'species': 'Douglas Fir', 'size_value': 12.0, 'size_unit': 'in',
                },
                'block_standing_college_M': {
                    'species': 'Hybrid Poplar', 'size_value': 9.0, 'size_unit': 'in',
                },
            },
        })
        apply_preset(tournament.id, 'per-key')

        uh = WoodConfig.query.filter_by(
            tournament_id=tournament.id,
            config_key='block_underhand_college_M',
        ).first()
        sb = WoodConfig.query.filter_by(
            tournament_id=tournament.id,
            config_key='block_standing_college_M',
        ).first()
        assert uh.species == 'Douglas Fir' and uh.size_value == 12.0
        assert sb.species == 'Hybrid Poplar' and sb.size_value == 9.0

    def test_apply_preset_includes_log_relay_doublebuck(self, tmp_preset_file, db_session, tournament):
        """M2 — log_relay_doublebuck must roundtrip through presets."""
        from models.wood_config import WoodConfig
        from services.woodboss import apply_preset, save_custom_preset
        save_custom_preset('with-relay-log', {
            'log_relay_doublebuck': {
                'species': 'Western Larch', 'size_value': 18.0, 'size_unit': 'in',
            },
        })
        apply_preset(tournament.id, 'with-relay-log')
        row = WoodConfig.query.filter_by(
            tournament_id=tournament.id, config_key='log_relay_doublebuck',
        ).first()
        assert row is not None
        assert row.species == 'Western Larch'
        assert row.size_value == 18.0

    def test_build_preset_from_form_captures_per_cfg_key(self, db_session):
        """M1 + unsaved-form-data capture. build_preset_from_form should
        emit both legacy `blocks` and new `blocks_by_key` per-category."""
        from services.woodboss import build_preset_from_form
        form = {
            'species_block_underhand_college_M': 'Douglas Fir',
            'size_value_block_underhand_college_M': '12',
            'size_unit_block_underhand_college_M': 'in',
            'species_block_standing_college_M': 'Hybrid Poplar',
            'size_value_block_standing_college_M': '9',
            'size_unit_block_standing_college_M': 'in',
            'species_log_general': 'Western Larch',
            'size_value_log_general': '18',
            'size_unit_log_general': 'in',
        }
        preset = build_preset_from_form(form)
        assert 'blocks_by_key' in preset
        assert preset['blocks_by_key']['block_underhand_college_M']['species'] == 'Douglas Fir'
        assert preset['blocks_by_key']['block_standing_college_M']['species'] == 'Hybrid Poplar'
        assert preset['log_general']['species'] == 'Western Larch'
        # Legacy broadcast set to first populated spec.
        assert preset['blocks']['species'] in ('Douglas Fir', 'Hybrid Poplar')

    def test_atomic_write_survives_simulated_crash(self, tmp_preset_file):
        """M6 — partial write via .tmp + os.replace keeps the live file
        readable even if an in-progress write never finishes."""
        import json

        from services.woodboss import get_all_presets, save_custom_preset
        save_custom_preset('round-one', {'blocks': {'species': 'Pine'}})

        # Simulate a crashed write: leave a stray .tmp file AND corrupt the
        # real file to empty. _load_preset_file should recover by treating
        # it as missing/corrupt and returning {} rather than crashing.
        real = tmp_preset_file
        real.write_text('')  # empty → json.JSONDecodeError
        presets = get_all_presets()
        assert isinstance(presets, dict)  # did not raise
        # Subsequent save should still succeed atomically.
        save_custom_preset('round-two', {'blocks': {'species': 'Oak'}})
        loaded = json.loads(real.read_text())
        assert 'round-two' in loaded


# ---------------------------------------------------------------------------
# save_config clear-row behaviour (H2)
# ---------------------------------------------------------------------------

class TestSaveConfigClear:
    """H2 — blanking every field on an existing row must clear the DB row,
    not silently skip it."""

    def test_blanking_existing_row_clears_it(self, auth_client, db_session, tournament):
        from models.event import Event
        from models.wood_config import WoodConfig

        db_session.add(Event(
            tournament_id=tournament.id, name='Underhand Hard Hit',
            event_type='college', gender='M', scoring_type='hits',
            stand_type='underhand',
        ))
        db_session.add(WoodConfig(
            tournament_id=tournament.id,
            config_key='block_underhand_college_M',
            species='Douglas Fir', size_value=14.0, size_unit='in',
        ))
        db_session.commit()

        # Post blank values for the existing key. auth_client is logged in
        # as admin so the management-route guard doesn't redirect us.
        resp = auth_client.post(
            f'/woodboss/{tournament.id}/config',
            data={
                'csrf_token': 'x',
                'species_block_underhand_college_M': '',
                'size_value_block_underhand_college_M': '',
                'size_unit_block_underhand_college_M': 'in',
                'notes_block_underhand_college_M': '',
                'count_override_block_underhand_college_M': '',
            },
            follow_redirects=False,
        )
        assert resp.status_code in (302, 303)

        row = WoodConfig.query.filter_by(
            tournament_id=tournament.id,
            config_key='block_underhand_college_M',
        ).first()
        assert row is not None  # row still exists
        assert row.species is None
        assert row.size_value is None  # cleared

