"""Tests for services/reference_gate.py.

The gate has two load-bearing behaviours and they pull against each other. It
has to refuse a save that brings in a reference to the wrong competitor, and it
has to let through a save that merely carries forward damage that was already
in the column. Get the first wrong and the era-1 ghosts happen again. Get the
second wrong and every production mirror that still holds those 55 ghosts
becomes unwritable, including for the saves that would repair them.

So the shape of this file is: one class that tests :func:`check_pending`
directly, which is where the delta rule lives and where a wrong answer is
cheap to read; and one much smaller class that proves the ``before_flush``
listener is actually wired to that decision, because a correct rule nobody
calls protects nothing.

The forgiveness tests deliberately move the legacy id to a *different JSON
path* than the one it was found at. A path-keyed rule passes the naive version
of that test and fails this one, and a path-keyed rule is what a bracket save
would trip over on race day.
"""
import contextlib
import json

import pytest

from services.reference_audit import (
    CROSS_KIND,
    DANGLING,
    UNKNOWN_KIND,
    Finding,
    ReferenceSite,
)
from services.reference_gate import (
    BadReferenceWrite,
    check_pending,
    install,
    uninstall,
)
from tests.conftest import (
    make_college_competitor,
    make_event,
    make_pro_competitor,
    make_team,
    make_tournament,
)


@contextlib.contextmanager
def _ungated(session):
    """Write a knowingly broken blob, the way history did.

    Every forgiveness test needs a row that is already damaged, and with the
    gate armed there is no way to create one. This is the reason
    :func:`services.reference_gate.uninstall` exists. It re-arms on the way out
    even if the body raises, because leaving the gate off would silently
    disarm every test that runs after this one in the same process.
    """
    uninstall(session)
    try:
        yield
    finally:
        install(session)


@pytest.fixture()
def armed(db_session):
    """The session with the gate installed.

    Never uninstalls on teardown. The app factory arms the same scoped session
    at startup, so an installed gate is the resting state of the process and a
    fixture that removed it would be lying to whatever ran next.
    """
    install(db_session)
    return db_session


@pytest.fixture()
def world(armed):
    """A college event, a live college competitor, and a live pro id that the
    college pool does not have.

    The asymmetry is built by filling the pro pool deeper than the college pool
    and then picking a pro id no college competitor holds, not by writing an id
    in by hand. Sequences do not roll back; a hardcoded id is a test that
    passes once and then quietly stops testing anything.
    """
    session = armed
    tournament = make_tournament(session)
    team = make_team(session, tournament)
    college = make_college_competitor(session, tournament, team,
                                      name='Davis Underwood')
    pros = [make_pro_competitor(session, tournament, name=name)
            for name in ('Tyler Cook', 'Bee Philips', 'Ann Lee')]
    session.flush()

    live_college = {college.id}
    ghost = next(pro for pro in pros if pro.id not in live_college)

    event = make_event(session, tournament, name='Birling',
                       event_type='college', payouts={
                           'competitors': [{'id': college.id,
                                            'name': 'Davis Underwood'}],
                       })
    session.flush()
    return session, tournament, college, ghost, event


def _set(event, column, blob):
    setattr(event, column, json.dumps(blob))


class TestCheckPending:
    """The decision itself, called directly, no flush."""

    def test_untouched_column_is_not_examined(self, world):
        session, _tournament, _college, _ghost, event = world
        event.name = 'Birling, renamed'
        assert check_pending(session, event, 'payouts') == []
        assert check_pending(session, event, 'event_state') == []

    def test_clean_save_passes(self, world):
        session, _tournament, college, _ghost, event = world
        _set(event, 'payouts', {'competitors': [{'id': college.id,
                                                 'name': 'Davis Underwood'}],
                                'seeding': [college.id]})
        assert check_pending(session, event, 'payouts') == []

    def test_new_cross_kind_reference_is_refused(self, world):
        """A live pro id sitting in a college position. This is the exact shape
        of the production defect: it resolves to a real human, so nothing
        downstream errors, it just prints the wrong name."""
        session, _tournament, college, ghost, event = world
        _set(event, 'payouts', {'competitors': [{'id': college.id,
                                                 'name': 'Davis Underwood'}],
                                'seeding': [ghost.id]})

        findings = check_pending(session, event, 'payouts')

        assert len(findings) == 1
        assert findings[0].verdict == CROSS_KIND
        assert findings[0].site.raw_id == ghost.id
        assert findings[0].collides_with == ghost.name

    def test_new_dangling_reference_is_refused(self, world):
        session, _tournament, _college, _ghost, event = world
        _set(event, 'payouts', {'seeding': [987654321]})

        findings = check_pending(session, event, 'payouts')

        assert len(findings) == 1
        assert findings[0].verdict == DANGLING
        assert findings[0].site.raw_id == 987654321

    def test_event_state_is_gated_too(self, world):
        session, _tournament, _college, ghost, event = world
        _set(event, 'event_state', {'seeding': [ghost.id]})

        findings = check_pending(session, event, 'event_state')

        assert [f.site.raw_id for f in findings] == [ghost.id]

    def test_a_brand_new_row_is_judged_with_nothing_to_forgive(self, world):
        """No previous value at all is not the same as a previous value that
        was clean, and the difference matters: ``history.deleted`` is empty for
        an INSERT, and reading that as "everything was already bad" would let a
        new row carry in anything it liked."""
        session, tournament, _college, ghost, _event = world
        from models.event import Event
        fresh = Event(tournament_id=tournament.id, name='Underhand',
                      event_type='college', scoring_type='time',
                      scoring_order='lowest_wins',
                      payouts=json.dumps({'seeding': [ghost.id]}),
                      event_state=json.dumps({}))
        session.add(fresh)

        findings = check_pending(session, fresh, 'payouts')

        assert [f.site.raw_id for f in findings] == [ghost.id]

    def test_unparseable_new_value_is_not_this_modules_problem(self, world):
        session, _tournament, _college, _ghost, event = world
        event.payouts = 'not json at all'
        assert check_pending(session, event, 'payouts') == []

    def test_emptying_the_column_passes(self, world):
        session, _tournament, _college, _ghost, event = world
        event.payouts = '{}'
        assert check_pending(session, event, 'payouts') == []

    def test_unknown_kind_is_allowed_through(self, world, monkeypatch):
        """Nothing in the two gated stores produces ``UNKNOWN_KIND`` today, so
        this is synthetic on purpose. It is asserted anyway because the day
        something does produce it, the gate must not refuse a save on the
        strength of a verdict that means "the audit could not tell". Refusing
        there is guessing in the direction of an outage."""
        session, _tournament, _college, _ghost, event = world
        site = ReferenceSite(store='events.payouts', row_id=event.id,
                             path='pending.seeding[0]', raw_id=4242,
                             kind='klingon', kind_source='event_type',
                             name_in_blob=None, name_in_row=None)
        monkeypatch.setattr('services.reference_gate.check_blob',
                            lambda *a, **k: [Finding(site=site,
                                                     verdict=UNKNOWN_KIND)])
        _set(event, 'payouts', {'seeding': [4242]})

        assert check_pending(session, event, 'payouts') == []


class TestForgivesLegacyDamage:
    """The half that keeps this installable on a database that is still dirty."""

    @pytest.fixture()
    def damaged(self, world):
        """An event whose payouts already cite the ghost, written past the gate
        the way the c38 reseed wrote them."""
        session, tournament, college, ghost, event = world
        with _ungated(session):
            _set(event, 'payouts', {
                'competitors': [{'id': college.id, 'name': 'Davis Underwood'}],
                'seeding': [ghost.id],
            })
            session.flush()
        return session, tournament, college, ghost, event

    def test_carrying_the_same_bad_id_forward_passes(self, damaged):
        session, _tournament, college, ghost, event = damaged
        _set(event, 'payouts', {
            'competitors': [{'id': college.id, 'name': 'Davis Underwood'}],
            'seeding': [ghost.id],
            'placements': {str(college.id): 1},
        })
        assert check_pending(session, event, 'payouts') == []

    def test_moving_the_bad_id_to_a_different_path_still_passes(self, damaged):
        """The forgiveness is keyed on the id, not on where in the blob it
        sits. A bracket save rewrites paths wholesale as matches advance, so a
        path-keyed rule would read a legacy ghost sliding from one slot to the
        next as a brand new defect and refuse a save that changed nothing about
        it."""
        session, _tournament, college, ghost, event = damaged
        _set(event, 'payouts', {
            'competitors': [{'id': college.id, 'name': 'Davis Underwood'}],
            'falls': [ghost.id],
            'pre_seedings': {str(ghost.id): 2},
        })
        assert check_pending(session, event, 'payouts') == []

    def test_repairing_the_bad_id_passes(self, damaged):
        """The save the repair script makes. Obvious, and the whole reason the
        delta rule exists: a whole-blob gate refuses this one too."""
        session, _tournament, college, _ghost, event = damaged
        _set(event, 'payouts', {
            'competitors': [{'id': college.id, 'name': 'Davis Underwood'}],
            'seeding': [college.id],
        })
        assert check_pending(session, event, 'payouts') == []

    def test_a_new_bad_id_alongside_a_legacy_one_is_refused_alone(self,
                                                                 damaged):
        session, _tournament, college, ghost, event = damaged
        _set(event, 'payouts', {
            'competitors': [{'id': college.id, 'name': 'Davis Underwood'}],
            'seeding': [ghost.id, 987654321],
        })

        findings = check_pending(session, event, 'payouts')

        assert [f.site.raw_id for f in findings] == [987654321]

    def test_forgiveness_survives_an_expired_attribute(self, damaged):
        """The trap that nearly shipped.

        A column attribute records no old value unless it was loaded before
        being assigned, and ``commit()`` expires every attribute on every
        object in the session. So the second save in a request that already
        committed once used to read as "there was nothing here before", the
        legacy id looked brand new, and the gate refused a write it was
        specifically designed to let through. That is the relay and
        partnered-axe shape: one ``Event`` held across repeated
        ``_save_state(commit=True)`` calls.
        """
        session, _tournament, college, ghost, event = damaged
        session.expire(event, ['payouts'])

        _set(event, 'payouts', {
            'competitors': [{'id': college.id, 'name': 'Davis Underwood'}],
            'falls': [ghost.id],
        })

        assert check_pending(session, event, 'payouts') == []

    def test_an_expired_row_still_refuses_a_genuinely_new_bad_id(self,
                                                                damaged):
        """The fallback read must not turn into blanket amnesty."""
        session, _tournament, college, ghost, event = damaged
        session.expire(event, ['payouts'])

        _set(event, 'payouts', {
            'competitors': [{'id': college.id, 'name': 'Davis Underwood'}],
            'seeding': [ghost.id, 987654321],
        })

        findings = check_pending(session, event, 'payouts')

        assert [f.site.raw_id for f in findings] == [987654321]

    def test_forgiveness_does_not_leak_across_columns(self, damaged):
        """``payouts`` being dirty is not a licence for ``event_state``. The
        comparison is per column, against that column's own previous value."""
        session, _tournament, _college, ghost, event = damaged
        _set(event, 'event_state', {'seeding': [ghost.id]})

        findings = check_pending(session, event, 'event_state')

        assert [f.site.raw_id for f in findings] == [ghost.id]


class TestTheListenerIsWired:
    """Small on purpose. The decision is tested above; this is only about
    whether a flush actually consults it."""

    def test_flush_raises_and_names_the_position_and_the_id(self, world):
        session, _tournament, _college, ghost, event = world
        _set(event, 'payouts', {'seeding': [ghost.id]})

        with pytest.raises(BadReferenceWrite) as excinfo:
            session.flush()

        exc = excinfo.value
        assert exc.event_id == event.id
        assert exc.column == 'payouts'
        assert [f.site.raw_id for f in exc.findings] == [ghost.id]
        message = str(exc)
        assert 'seeding[0]' in message
        assert str(ghost.id) in message
        assert ghost.name in message
        session.rollback()

    def test_a_clean_save_flushes(self, world):
        session, _tournament, college, _ghost, event = world
        _set(event, 'payouts', {'seeding': [college.id]})
        session.flush()
        assert json.loads(event.payouts)['seeding'] == [college.id]

    def test_non_event_rows_are_not_examined(self, world):
        """The listener walks everything in ``session.new`` and
        ``session.dirty``. A competitor rename has no blob columns and must not
        be dragged through a pool load."""
        session, tournament, college, _ghost, _event = world
        college.name = 'Davis Underwood II'
        make_pro_competitor(session, tournament, name='Nobody In Particular')
        session.flush()

    def test_install_is_idempotent(self, world):
        session, _tournament, _college, ghost, event = world
        install(session)
        install(session)
        _set(event, 'payouts', {'seeding': [ghost.id]})

        with pytest.raises(BadReferenceWrite):
            session.flush()
        session.rollback()

    def test_uninstall_lets_the_bad_write_through(self, world):
        session, _tournament, _college, ghost, event = world
        with _ungated(session):
            _set(event, 'payouts', {'seeding': [ghost.id]})
            session.flush()
        assert json.loads(event.payouts)['seeding'] == [ghost.id]
