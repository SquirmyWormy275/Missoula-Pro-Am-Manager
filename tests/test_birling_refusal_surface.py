"""D13-C commit A3c. A refused projection reaches a human, and costs nothing.

Why this file exists
====================
A3b made the row tables what the bracket pages read, with the JSON document as
the fallback. That combination has a quiet failure: a save whose document
cannot be projected leaves the event with no rows, the pages then fall back to
the JSON, and everything on screen looks exactly right. The only record is a
warning in a log nobody is reading during a show. At A4 the fallback goes away
and that same event has nothing behind it at all.

A3c closes it by making ``birling_rows.project`` raise. The whole risk of that
change is on the other side: an exception on a save path is how a judge loses a
scored result. So the shipping shape is deliberately not the obvious one.

  ``project`` raises                         the refusal exists
  ``_save_bracket_data`` catches and commits  the judge never loses the write
  the routes read and flash                   a human hears about it

The first two are proved here against real data, with real unresolvable
documents built the way the rest of the birling suite builds them. The route
proofs are split by what each route can actually reach: record, fall and undo
run a genuine refusal end to end, because their document comes from an event
that is already damaged. Generate and the stale-shape rebuild cannot, because
both build their document fresh from the live roster and a document built from
the roster resolves by construction, so those two inject the refusal at the
projector and prove only the half they own, which is the surfacing.

``BirlingBracket._save_bracket_data`` commits, which escapes the ``db_session``
savepoint. Every assertion below is scoped to its own event id.
"""
from __future__ import annotations

import json
import uuid

import pytest
import sqlalchemy as sa

from database import db
from models import (
    BirlingMatch,
    BirlingPlacement,
    BirlingPreSeed,
    BirlingSeed,
)
from services import birling_rows as rows
from services.birling_bracket import BirlingBracket
from tests.conftest import (
    make_college_competitor,
    make_event,
    make_team,
    make_tournament,
)

GHOST_REASON = 'a competitor reference names nobody in the event pool'
INJECTED = 'an injected reason'
FLASH_MARK = 'could not be written to the bracket tables'


# ---------------------------------------------------------------------------
# A world, and a damaged one
# ---------------------------------------------------------------------------


def _world(session, name, people=5):
    tour = make_tournament(session)
    team = make_team(session, tour, code=f"RS-{uuid.uuid4().hex[:4]}")
    roster = [make_college_competitor(session, tour, team, f"Person {i}")
              for i in range(1, people + 1)]
    session.flush()
    event = make_event(session, tour, name, event_type="college",
                       scoring_type="bracket", stand_type="birling")
    session.flush()
    return tour, team, event, roster


def _entrants(people):
    return [{"id": p.id, "name": p.name} for p in people]


def _set_payouts(event, payload):
    """Overwrite ``events.payouts`` underneath the ORM.

    Raw SQL rather than an attribute assignment, for the same two reasons as
    the identical helper in ``test_birling_reader_flip.py``: the reader must
    not be able to serve the good value out of the identity map, and the
    reference gate must not see an ORM write carrying a reference it would be
    right to refuse. The damage being simulated is a roster that changed after
    the document was written, which is not something the gate is meant to stop.
    """
    db.session.execute(
        sa.text("UPDATE events SET payouts = :value WHERE id = :id"),
        {"value": payload, "id": event.id})
    db.session.expire(event)


def _counts(event_id):
    return {
        "seeds": BirlingSeed.query.filter_by(event_id=event_id).count(),
        "pre_seeds": BirlingPreSeed.query.filter_by(event_id=event_id).count(),
        "matches": BirlingMatch.query.filter_by(event_id=event_id).count(),
        "placements": BirlingPlacement.query.filter_by(
            event_id=event_id).count(),
    }


def _damaged(session, name):
    """An event whose stored document names one competitor who does not exist.

    Built by generating a real bracket and then replacing one id in ``seeding``
    and the matching entry in ``competitors``, which is the shape a roster
    change leaves behind: the person was withdrawn, the bracket still names
    them. The match tree is left alone, so exactly one reason is filed and the
    bracket stays playable, which is what makes it usable for the record, fall
    and undo proofs.

    The rows are cleared as part of the setup. An event in this state has no
    rows in production either, because the save that damaged it is the save
    that cleared them, and clearing here is also what makes the reader fall
    back to the damaged document rather than serving the clean rows forever.
    """
    _tour, _team, event, people = _world(session, name)
    entrants = people[:4]
    BirlingBracket(event).generate_bracket(_entrants(entrants))
    db.session.flush()

    doc = rows.load_document(event)
    victim = doc["seeding"][-1]
    ghost = max(p.id for p in people) + 5000
    doc["seeding"] = [ghost if raw == victim else raw for raw in doc["seeding"]]
    for entry in doc["competitors"]:
        if entry["id"] == victim:
            entry["id"] = ghost

    rows.clear_event(event.id)
    db.session.flush()
    _set_payouts(event, json.dumps(doc))
    db.session.commit()
    return event, entrants, doc


def _playable_match(doc):
    """A round-1 match with two competitors in it, from the stored document."""
    for match in doc["bracket"]["winners"][0]:
        if match.get("competitor1") and match.get("competitor2"):
            return match
    raise AssertionError("the fixture bracket has no playable round-1 match")


def _stored(event_id):
    """``events.payouts`` straight out of the database, parsed."""
    raw = db.session.execute(
        sa.text("SELECT payouts FROM events WHERE id = :id"),
        {"id": event_id}).scalar()
    return json.loads(raw or "{}")


@pytest.fixture()
def judge_client(app, db_session):
    """A logged-in admin.

    ``app.py`` line 424 gates every management blueprint and ``scheduling`` is
    one, so an anonymous client is redirected to the login page and every
    assertion below would be made against it.
    """
    from models.user import User

    user = User(username=f"rs_admin_{uuid.uuid4().hex[:8]}", role="admin")
    user.set_password("rs_pass")
    db_session.add(user)
    db_session.flush()
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user.id)
    return client


def _flashes(client):
    """The flash queue, read out of the session rather than off a page.

    Reading the rendered page would prove the same thing plus a template, and
    a template that changes its markup would then break a test about a service
    contract. The queue is what the route actually wrote.
    """
    with client.session_transaction() as sess:
        return [(category, str(message))
                for category, message in sess.get("_flashes", [])]


def _refusal_flashes(client):
    return [message for _cat, message in _flashes(client) if FLASH_MARK in message]


def _refusal_on_page(response):
    """The same warning, read off a rendered page instead of the queue.

    A route that renders rather than redirects has already consumed its own
    flashes by the time the test looks, because the base template calls
    ``get_flashed_messages``. Only the manage page needs this; every other
    write path here answers with a redirect and leaves the queue intact.
    """
    return FLASH_MARK in response.get_data(as_text=True)


@pytest.fixture()
def injected_refusal(monkeypatch):
    """Make every projection refuse, without damaging any data.

    For the two routes that build their document from the live roster. Their
    document resolves by construction, so there is no arrangement of real rows
    that makes them refuse, and a proof that waited for one would never run.
    What those routes own is reading ``projection_refused`` and flashing it,
    and that is exactly what this isolates.

    The stand-in clears the event's rows first, because the real ``project``
    clears before it decides, and a stand-in that skipped that would let a test
    pass against a save path that had quietly kept stale rows.
    """
    def boom(event):
        rows.clear_event(event.id)
        db.session.flush()
        raise rows.ProjectionRefused(event.id, {INJECTED})

    monkeypatch.setattr(rows, "project", boom)
    return boom


# ---------------------------------------------------------------------------
# The judge never loses the write
# ---------------------------------------------------------------------------


class TestTheJudgeNeverLosesTheWrite:
    """The half of A3c that is a decision rather than a mechanism.

    ``project`` raising is the easy part. What it must not mean is that a
    scored result is rolled back to protect a table the page is not reading in
    that state. Every test here would pass just as well if
    ``_save_bracket_data`` let the exception through and the request rolled
    back, were it not asserting against the committed row.
    """

    def test_the_result_is_committed_even_though_the_projection_refused(
            self, db_session):
        event, entrants, doc = _damaged(db_session, "Committed")
        match = _playable_match(doc)
        winner = match["competitor1"]

        bb = BirlingBracket(event)
        bb.record_match_result(match["match_id"], winner)

        saved = _stored(event.id)
        recorded = [m for r in saved["bracket"]["winners"] for m in r
                    if m["match_id"] == match["match_id"]]
        assert len(recorded) == 1
        assert recorded[0]["winner"] == winner
        assert winner in [c.id for c in entrants]

    def test_the_refusal_is_recorded_on_the_instance(self, db_session):
        event, _entrants, doc = _damaged(db_session, "Recorded")
        match = _playable_match(doc)

        bb = BirlingBracket(event)
        bb.record_match_result(match["match_id"], match["competitor1"])

        assert isinstance(bb.projection_refused, rows.ProjectionRefused)
        assert bb.projection_refused.reasons == [GHOST_REASON]
        assert bb.projection_refused.event_id == event.id

    def test_the_event_is_left_with_no_rows_at_all(self, db_session):
        """No rows is the honest state and the one the fallback is built for.

        Half a projection would be worse than none, because ``is_projected``
        would then say yes and the reader would serve a bracket missing a
        competitor without ever consulting the document that still has them.
        """
        event, _entrants, doc = _damaged(db_session, "Rowless")
        match = _playable_match(doc)

        BirlingBracket(event).record_match_result(
            match["match_id"], match["competitor1"])

        assert _counts(event.id) == {"seeds": 0, "pre_seeds": 0, "matches": 0,
                                     "placements": 0}
        assert rows.is_projected(event.id) is False

    def test_a_save_that_projects_cleanly_records_no_refusal(self, db_session):
        """The negative half. Without it, an implementation that set
        ``projection_refused`` unconditionally would pass everything above."""
        _tour, _team, event, people = _world(db_session, "Clean")
        bb = BirlingBracket(event)
        bb.generate_bracket(_entrants(people[:4]))

        assert bb.projection_refused is None
        assert rows.is_projected(event.id) is True

    def test_a_second_clean_save_clears_a_refusal_from_the_first(
            self, db_session, injected_refusal, monkeypatch):
        """``projection_refused`` is the state of the last save, not a latch.

        A latch would keep flashing a refusal at a judge who had already fixed
        the roster, and the flash would be a lie about the save in front of
        them.
        """
        _tour, _team, event, people = _world(db_session, "Latch")
        bb = BirlingBracket(event)
        bb.generate_bracket(_entrants(people[:4]))
        assert bb.projection_refused is not None

        monkeypatch.undo()
        bb.generate_bracket(_entrants(people[:4]))
        assert bb.projection_refused is None


# ---------------------------------------------------------------------------
# Every write path says so
# ---------------------------------------------------------------------------


class TestTheDamagedRoutesSaySo:
    """Record, fall and undo, driven through the real routes on real damage.

    Nothing is patched in this class. The event is damaged, the reader falls
    back to the document, the save writes the document back, the projector
    refuses it, and the route has to say so.
    """

    def _post(self, client, event, path, data):
        return client.post(
            f"/scheduling/{event.tournament_id}/event/{event.id}/birling/{path}",
            data=data)

    def test_record_says_so(self, db_session, judge_client):
        event, _entrants, doc = _damaged(db_session, "RouteRecord")
        match = _playable_match(doc)

        response = self._post(judge_client, event, "record", {
            "match_id": match["match_id"],
            "winner_id": str(match["competitor1"]),
        })

        assert response.status_code == 302
        flashed = _refusal_flashes(judge_client)
        assert len(flashed) == 1
        assert GHOST_REASON in flashed[0]

    def test_fall_says_so(self, db_session, judge_client):
        event, _entrants, doc = _damaged(db_session, "RouteFall")
        match = _playable_match(doc)

        response = self._post(judge_client, event, "fall", {
            "match_id": match["match_id"],
            "fall_winner_id": str(match["competitor1"]),
        })

        assert response.status_code == 302
        assert len(_refusal_flashes(judge_client)) == 1

    def test_undo_says_so(self, db_session, judge_client):
        event, _entrants, doc = _damaged(db_session, "RouteUndo")
        match = _playable_match(doc)
        BirlingBracket(event).record_match_result(
            match["match_id"], match["competitor1"])

        response = self._post(judge_client, event, "undo", {
            "match_id": match["match_id"],
        })

        assert response.status_code == 302
        assert len(_refusal_flashes(judge_client)) == 1

    def test_a_clean_event_flashes_nothing_of_the_kind(
            self, db_session, judge_client):
        """The control. A route that flashed the warning unconditionally would
        satisfy all three tests above and be worse than no warning at all,
        because an operator who sees it on every save stops reading it."""
        _tour, _team, event, people = _world(db_session, "RouteClean")
        BirlingBracket(event).generate_bracket(_entrants(people[:4]))
        doc = rows.load_document(event)
        match = _playable_match(doc)

        self._post(judge_client, event, "record", {
            "match_id": match["match_id"],
            "winner_id": str(match["competitor1"]),
        })

        assert _refusal_flashes(judge_client) == []


class TestTheRosterBuiltRoutesSaySo:
    """Generate and the stale-shape rebuild, with the refusal injected.

    Both build their document from the live roster, so neither can be made to
    refuse with data. See the module docstring: these prove the surfacing and
    nothing else, and the refusal itself is proved on real documents above.
    """

    def test_generate_says_so(self, db_session, judge_client, injected_refusal):
        _tour, _team, event, people = _world(db_session, "RouteGenerate")
        for comp in people[:4]:
            comp.set_events_entered([str(event.id)])
        db_session.flush()
        db.session.commit()

        response = judge_client.post(
            f"/scheduling/{event.tournament_id}/event/{event.id}"
            f"/birling/generate", data={})

        assert response.status_code == 302
        flashed = _refusal_flashes(judge_client)
        assert len(flashed) == 1
        assert INJECTED in flashed[0]

    def test_the_generate_route_still_reports_a_real_failure_as_a_failure(
            self, db_session, judge_client, monkeypatch):
        """The refusal path must not have eaten the ``except Exception`` path.

        ``birling_generate`` wraps the whole generate in a bare except and
        flashes ``Bracket generation failed``. A3c had to leave that intact,
        because a refusal is now caught below it and anything still reaching it
        is a real defect.
        """
        _tour, _team, event, people = _world(db_session, "RouteBoom")
        for comp in people[:4]:
            comp.set_events_entered([str(event.id)])
        db_session.flush()
        db.session.commit()

        def boom(event_arg):
            raise RuntimeError("not a refusal")

        monkeypatch.setattr(rows, "project", boom)
        response = judge_client.post(
            f"/scheduling/{event.tournament_id}/event/{event.id}"
            f"/birling/generate", data={})

        assert response.status_code == 302
        messages = [message for _cat, message in _flashes(judge_client)]
        assert any("Bracket generation failed" in m for m in messages)
        assert _refusal_flashes(judge_client) == []

    def test_the_stale_shape_rebuild_says_so(
            self, db_session, judge_client, monkeypatch):
        """The manage page is a GET that writes.

        ``rebuild_if_stale_shape`` regenerates a pre-V2.14.14 bracket in place
        on page load, which means the one route nobody thinks of as a write
        path can refuse a projection. Left unsurfaced it would be the quietest
        of the lot, because the operator asked for a page and got one.
        """
        _tour, _team, event, people = _world(db_session, "RouteStale")
        BirlingBracket(event).generate_bracket(_entrants(people[:4]))
        db.session.flush()

        doc = rows.load_document(event)
        assert len(doc["bracket"]["winners"][0]) == 2, "shape assumption"
        doc["bracket"]["winners"][0] = doc["bracket"]["winners"][0][:1]
        rows.clear_event(event.id)
        db.session.flush()
        _set_payouts(event, json.dumps(doc))
        db.session.commit()

        # Injected only now. The setup above needs a real bracket to make a
        # stale shape out of, and a projector that refuses everything would
        # have left it with no rows and nothing to truncate.
        def boom(event_arg):
            rows.clear_event(event_arg.id)
            db.session.flush()
            raise rows.ProjectionRefused(event_arg.id, {INJECTED})

        monkeypatch.setattr(rows, "project", boom)

        response = judge_client.get(
            f"/scheduling/{event.tournament_id}/event/{event.id}/birling")

        assert response.status_code == 200
        assert _refusal_on_page(response)
        assert INJECTED in response.get_data(as_text=True)


# ---------------------------------------------------------------------------
# The rankings page saves the rest of the page
# ---------------------------------------------------------------------------


class TestTheRankingsPageKeepsWhatItCould:
    """``routes/scheduling/ability_rankings.py`` projects inside a loop.

    One unresolvable bracket must not cost the judge the seedings they dragged
    for every other bracket on the same page, which is what letting the raise
    out of the loop would do.
    """

    def _post(self, client, tournament_id, data):
        return client.post(
            f"/scheduling/{tournament_id}/pro/ability-rankings", data=data)

    def _two_birling_events(self, session):
        tour = make_tournament(session)
        team = make_team(session, tour, code=f"RS-{uuid.uuid4().hex[:4]}")
        roster = [make_college_competitor(session, tour, team, f"Person {i}")
                  for i in range(1, 5)]
        session.flush()
        good = make_event(session, tour, "GoodBirl", event_type="college",
                          scoring_type="bracket", stand_type="birling")
        bad = make_event(session, tour, "BadBirl", event_type="college",
                         scoring_type="bracket", stand_type="birling")
        session.flush()
        return tour, good, bad, roster

    def test_the_good_event_is_saved_and_the_bad_one_is_named(
            self, db_session, judge_client, reference_gate_disarmed):
        """The gate is disarmed because the form is the only way in.

        In production the id in this form comes from a page built off the
        roster and the reference gate would refuse a brand new bad one, so the
        real cause of a refusal here is a competitor withdrawn between the page
        load and the save. Reproducing that through two requests would prove
        the same thing with more moving parts and a gate behaviour this test is
        not about.
        """
        tour, good, bad, roster = self._two_birling_events(db_session)
        ghost = max(c.id for c in roster) + 5000
        db.session.commit()

        response = self._post(judge_client, tour.id, {
            f"birling_schools_{good.id}": json.dumps(
                {"UM": [roster[0].id, roster[1].id]}),
            f"birling_schools_{bad.id}": json.dumps({"UM": [ghost]}),
        })

        assert response.status_code == 302
        assert _counts(good.id)["pre_seeds"] == 2
        assert _counts(bad.id)["pre_seeds"] == 0
        assert _stored(bad.id)["pre_seedings"] == {str(ghost): 1}

        flashed = _refusal_flashes(judge_client)
        assert len(flashed) == 1
        assert bad.display_name in flashed[0]
        assert good.display_name not in flashed[0]
        assert GHOST_REASON in flashed[0]

    def test_nothing_is_flashed_when_every_event_projects(
            self, db_session, judge_client):
        tour, good, other, roster = self._two_birling_events(db_session)
        db.session.commit()

        self._post(judge_client, tour.id, {
            f"birling_schools_{good.id}": json.dumps({"UM": [roster[0].id]}),
            f"birling_schools_{other.id}": json.dumps({"UM": [roster[1].id]}),
        })

        assert _counts(good.id)["pre_seeds"] == 1
        assert _counts(other.id)["pre_seeds"] == 1
        assert _refusal_flashes(judge_client) == []


# ---------------------------------------------------------------------------
# The reproject script
# ---------------------------------------------------------------------------


class TestTheScriptReportsAndCarriesOn:
    """``scripts/reproject_birling.py`` is the operator's repair tool.

    It used to read refusals off ``Plan.reasons``. A raise that aborted the
    whole run on the first refused event would make it useless for exactly the
    job it exists for, which is being pointed at the handful of events a repair
    was supposed to fix and being told which ones it did not.
    """

    def test_a_refusal_is_reported_and_the_next_event_still_projects(
            self, db_session):
        from scripts.reproject_birling import reproject

        bad, _entrants_bad, _doc = _damaged(db_session, "ScriptBad")
        _tour, _team, good, people = _world(db_session, "ScriptGood")
        BirlingBracket(good).generate_bracket(_entrants(people[:4]))
        db.session.commit()

        summary = reproject([bad.id, good.id])

        assert summary["aborted"] is None
        assert [row["event_id"] for row in summary["refused"]] == [bad.id]
        assert summary["refused"][0]["reasons"] == [GHOST_REASON]
        assert [row["event_id"] for row in summary["projected"]] == [good.id]

    def test_anything_that_is_not_a_refusal_still_aborts_the_run(
            self, db_session, monkeypatch):
        """The distinction the script now has to draw. A defect in the
        projector leaves the session unusable, and projecting the remaining
        events on top of it reports noise."""
        from scripts.reproject_birling import reproject

        _tour, _team, event, people = _world(db_session, "ScriptBoom")
        BirlingBracket(event).generate_bracket(_entrants(people[:4]))
        db.session.commit()

        def boom(event_arg):
            raise RuntimeError("not a refusal")

        monkeypatch.setattr(rows, "project", boom)
        summary = reproject([event.id])

        assert summary["aborted"] is not None
        assert "RuntimeError" in summary["aborted"]
        assert summary["projected"] == []
        assert summary["refused"] == []
