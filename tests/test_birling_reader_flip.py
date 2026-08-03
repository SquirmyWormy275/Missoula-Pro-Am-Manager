"""D13-C commit A3b. The bracket pages read the rows, and fall back to the blob.

Why this file exists
====================
Every other change in A3b could be implemented as a no-op that still reads
``events.payouts``, and the whole suite would stay green, because until A4 the
blob and the rows say the same thing on every event the app itself wrote. A
test that generates a bracket and reads it back cannot tell the two apart.

So the proofs here make them disagree on purpose, in both directions:

  the rows are the truth   corrupt the blob, keep the rows, demand the rows
  the blob is the net      delete the rows, keep the blob, demand the blob
  reset means gone         reset an event, demand nothing comes back

The first is the only shape that can fail if the reader never moved. The
second is the only shape that can fail if the reader moved without a net,
which is the property that lets A3b ship ahead of A3c. The third is the hole
the survey found: before A3b, ``birling_reset`` blanked the JSON and left
every row in place, so the first page load after a reset would have put the
whole bracket back.

Everything is database-backed. There is no way to prove a reader reads a table
by mocking the table away, which is exactly what ``patched_bracket_deps`` does
for the forty tests in the mock suite.

``BirlingBracket._save_bracket_data`` calls ``db.session.commit()``, which
escapes the ``db_session`` savepoint. Every assertion below is scoped to its
own event id and never to a global count.
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

# ---------------------------------------------------------------------------
# A world
# ---------------------------------------------------------------------------


def _world(session, name="ReaderFlip", people=5):
    """A tournament, a team, some college competitors and one bracket event."""
    tour = make_tournament(session)
    team = make_team(session, tour, code=f"UM-{uuid.uuid4().hex[:4]}")
    roster = [make_college_competitor(session, tour, team, f"Person {i}")
              for i in range(1, people + 1)]
    session.flush()
    event = make_event(session, tour, name, event_type="college",
                       scoring_type="bracket", stand_type="birling")
    session.flush()
    return tour, team, event, roster


def _entrants(people):
    return [{"id": p.id, "name": p.name} for p in people]


def _generate(event, people):
    """Generate a bracket the ordinary way, through the service."""
    BirlingBracket(event).generate_bracket(_entrants(people))
    db.session.flush()


def _wreck_the_blob(event, payload='{"bracket": {"winners": []}}'):
    """Overwrite ``events.payouts`` underneath the ORM.

    Raw SQL plus an expire rather than an attribute assignment, so the object
    the reader loads cannot be holding the good value in its identity map. A
    reader that still consults the blob gets ``payload`` and nothing else.
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


@pytest.fixture()
def birling_logger_awake():
    """Wake the readers' loggers back up.

    ``migrations/env.py`` line 63 calls ``logging.config.fileConfig`` without
    ``disable_existing_loggers=False``, which disables every logger that exists
    at that moment. The default unit lane runs the migration chain in process,
    well after both modules are imported at collection time, so by the time a
    test runs ``caplog`` sees nothing at all. Filed as a finding; worked around
    here rather than fixed, because ``migrations/env.py`` is committed and
    nobody has approved changing it.
    """
    import services.birling_bracket as bracket_module

    was = (rows.logger.disabled, bracket_module.logger.disabled)
    rows.logger.disabled = False
    bracket_module.logger.disabled = False
    yield
    rows.logger.disabled, bracket_module.logger.disabled = was


@pytest.fixture()
def judge_client(app, db_session):
    """A logged-in admin.

    ``app.py`` line 424 gates every management blueprint, and ``scheduling`` is
    one, so an anonymous client gets a redirect to the login page rather than
    the page under test. A redirect would satisfy a loose status assertion
    while proving nothing, which is the trap this fixture exists to avoid.
    """
    from models.user import User

    user = User(username=f"rf_admin_{uuid.uuid4().hex[:8]}", role="admin")
    user.set_password("rf_pass")
    db_session.add(user)
    db_session.flush()
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user.id)
    return client


# ---------------------------------------------------------------------------
# The rows are the truth
# ---------------------------------------------------------------------------


class TestTheRowsAreTheTruth:
    """Make the blob and the rows disagree, and demand the rows win.

    Nothing in this class can pass if ``_load_bracket_data`` still parses
    ``events.payouts`` first.
    """

    def test_a_wrecked_blob_does_not_reach_the_page(self, db_session):
        _tour, _team, event, people = _world(db_session, "Wrecked")
        _generate(event, people[:4])
        expected = rows.load_document(event)
        assert expected["bracket"]["winners"], "the bracket has to exist first"

        _wreck_the_blob(event)

        assert BirlingBracket(event).bracket_data == expected

    def test_an_emptied_blob_does_not_erase_the_bracket(self, db_session):
        """The resurrection shape, running the other way.

        An event whose document was blanked out from under it still has rows,
        and the rows are what a judge needs to see. This is the same disagree
        ment as above with the blob emptied rather than replaced, because an
        empty blob is what a half-finished reset leaves behind and it is the
        one wrong answer that looks like a legitimate state.
        """
        _tour, _team, event, people = _world(db_session, "Emptied")
        _generate(event, people[:4])
        seeds = rows.load_document(event)["seeding"]

        _wreck_the_blob(event, "{}")

        assert BirlingBracket(event).bracket_data["seeding"] == seeds

    def test_the_blob_is_not_even_parsed_when_rows_exist(self, db_session):
        """A blob that is not JSON at all is not an error on this path.

        Before A3b this would have gone through ``json.loads`` and been
        swallowed by the bare ``except`` at open question 6, producing the
        empty skeleton. Now it is never read, so the judge keeps the bracket.
        """
        _tour, _team, event, people = _world(db_session, "NotJson")
        _generate(event, people[:4])
        expected = rows.load_document(event)

        _wreck_the_blob(event, "this is not json {{{")

        assert BirlingBracket(event).bracket_data == expected

    def test_the_index_page_agrees_with_the_manage_page(
            self, db_session, judge_client):
        """Both pages answer "is there a bracket" from the same place.

        ``birling_index`` used to parse the document itself, which made it a
        second reader. On an event whose blob has been wrecked the old index
        would have said "not seeded" while the manage page showed a bracket,
        and an operator following the sidebar would have concluded the seeding
        was lost.

        Asserted on what the page renders, not on what the reader returns. A
        test that calls ``BirlingBracket`` here would prove the reader twice
        and the page not at all, and the page is the thing that regressed.
        ``_world`` puts exactly one bracket event in the tournament, so the
        status badge on the page is this event's badge.
        """
        tour, _team, event, people = _world(db_session, "IndexAgrees")
        _generate(event, people[:4])
        _wreck_the_blob(event)
        db.session.commit()

        response = judge_client.get(f"/scheduling/{tour.id}/birling")
        assert response.status_code == 200
        body = response.get_data(as_text=True)

        assert event.display_name in body
        assert "Not seeded" not in body, (
            "the index read the wrecked blob and called a live bracket empty")
        assert "Seeded" in body
        # Rendered only under ``row.has_bracket``, so it is the second,
        # independent sign that the page found the bracket in the rows.
        assert "/ 4 placed" in body


# ---------------------------------------------------------------------------
# The blob is still the net
# ---------------------------------------------------------------------------


class TestTheBlobIsStillTheNet:
    """An event with no rows reads its document, and says so in the log.

    This is what lets A3b ship without A3c. A reader with something to fall
    back on cannot blank a bracket, so a save that ``project`` declined shows
    the judge the document it declined instead of an empty page.

    The log line is the instrument A4 needs. If it never fires across a real
    event then dropping the JSON is safe. If it fires, it is not.
    """

    def test_a_document_with_no_rows_is_still_rendered(self, db_session):
        _tour, _team, event, people = _world(db_session, "NoRows")
        _generate(event, people[:4])
        expected = json.loads(event.payouts)

        rows.clear_event(event.id)
        db.session.flush()
        assert _counts(event.id) == {"seeds": 0, "pre_seeds": 0,
                                     "matches": 0, "placements": 0}

        assert BirlingBracket(event).bracket_data == expected

    def test_the_fallback_names_the_event_and_the_reason(
            self, db_session, birling_logger_awake, caplog):
        _tour, _team, event, people = _world(db_session, "Logged")
        _generate(event, people[:4])
        rows.clear_event(event.id)
        db.session.flush()

        caplog.clear()
        with caplog.at_level("WARNING"):
            BirlingBracket(event)

        assert str(event.id) in caplog.text
        assert "it has no projected rows" in caplog.text
        assert "A4" in caplog.text, "the log line has to name what removes it"

    def test_an_event_with_neither_gets_the_skeleton(self, db_session):
        """No rows and no document is not a fallback, it is a new event.

        The skeleton returns before the warning, because an event that has
        never had a bracket is not evidence about anything A4 wants to know.
        """
        _tour, _team, event, _people = _world(db_session, "Neither")
        assert _counts(event.id)["seeds"] == 0

        assert BirlingBracket(event).bracket_data == rows.empty_document()

    def test_the_skeleton_does_not_log(
            self, db_session, birling_logger_awake, caplog):
        _tour, _team, event, _people = _world(db_session, "Quiet")
        caplog.clear()
        with caplog.at_level("WARNING"):
            BirlingBracket(event)
        assert "fell back to its JSON document" not in caplog.text

    def test_rows_that_will_not_load_fall_back_and_say_which(
            self, db_session, birling_logger_awake, caplog):
        """The second fallback path, and the one that matters most.

        Rows exist, so ``is_projected`` says yes, and then the reverse lookup
        finds nobody. Reaching it by moving the event to a roster that does not
        hold its competitors, which is the mechanical form of what ``UNLOADABLE``
        describes: the rows name people this event's pool cannot resolve.
        """
        _tour, _team, event, people = _world(db_session, "Unloadable")
        _generate(event, people[:4])
        expected = json.loads(event.payouts)
        assert _counts(event.id)["seeds"] == 4

        event.event_type = "pro"
        db.session.flush()
        with pytest.raises(rows.UnloadableBracket):
            rows.load_document(event)

        caplog.clear()
        with caplog.at_level("WARNING"):
            loaded = BirlingBracket(event).bracket_data

        assert loaded == expected
        assert "its rows will not load" in caplog.text


# ---------------------------------------------------------------------------
# Reset means gone
# ---------------------------------------------------------------------------


class TestResetMeansGone:
    """Hole 1. Before A3b, ``birling_reset`` left every row standing."""

    def test_the_route_clears_every_row_table(self, db_session, judge_client):
        tour, _team, event, people = _world(db_session, "ResetRows")
        _generate(event, people[:4])
        db.session.commit()
        assert _counts(event.id)["seeds"] == 4

        judge_client.post(
            f"/scheduling/{tour.id}/event/{event.id}/birling/reset")

        assert _counts(event.id) == {"seeds": 0, "pre_seeds": 0,
                                     "matches": 0, "placements": 0}

    def test_the_bracket_does_not_come_back_on_the_next_load(
            self, db_session, judge_client):
        """The bug in one line: reset, then look at the page.

        With the rows left standing and the reader flipped, this returns the
        whole bracket the judge just threw away.
        """
        tour, _team, event, people = _world(db_session, "ResetPage")
        _generate(event, people[:4])
        db.session.commit()

        judge_client.post(
            f"/scheduling/{tour.id}/event/{event.id}/birling/reset")
        db.session.expire(event)

        assert BirlingBracket(event).bracket_data == rows.empty_document()

    def test_a_reset_leaves_other_events_alone(self, db_session, judge_client):
        """``clear_event`` is scoped, and a global delete would pass every
        assertion above."""
        tour, team, event, people = _world(db_session, "ResetMine")
        other = make_event(db_session, tour, "ResetTheirs",
                           event_type="college", scoring_type="bracket",
                           stand_type="birling")
        db_session.flush()
        _generate(event, people[:4])
        _generate(other, people[:4])
        db.session.commit()
        assert _counts(other.id)["seeds"] == 4

        judge_client.post(
            f"/scheduling/{tour.id}/event/{event.id}/birling/reset")

        assert _counts(event.id)["seeds"] == 0
        assert _counts(other.id)["seeds"] == 4


# ---------------------------------------------------------------------------
# The two pages that consume the pre-seed map
# ---------------------------------------------------------------------------


class TestThePreSeedConsumersReadTheRows:
    """``load_pre_seedings`` having the right answer is not the whole claim.

    The mutation battery found this gap: reverting either caller to a document
    read left the whole suite green, because every proof of the new function
    called it directly and neither page was exercised on a bracket where the
    rows and the document disagree. So both callers get a page-level proof,
    built on a disagreement the caller cannot resolve by accident.

    The seed orders are exact reverses of each other. A caller reading the
    document produces the mirror image of a caller reading the rows, which is
    the one arrangement no partial credit can pass.
    """

    def _disagreeing_world(self, session, name):
        """Four signed-up competitors, rows seeded backwards, blob forwards."""
        tour, _team, event, people = _world(session, name)
        entered = people[:4]
        for comp in entered:
            comp.set_events_entered([str(event.id)])
        session.flush()

        backwards = {str(p.id): number
                     for number, p in enumerate(reversed(entered), start=1)}
        event.payouts = json.dumps({"pre_seedings": backwards})
        rows.project(event)
        db.session.flush()

        forwards = {str(p.id): number
                    for number, p in enumerate(entered, start=1)}
        _wreck_the_blob(event, json.dumps({"pre_seedings": forwards}))
        db.session.commit()
        return tour, event, entered

    def test_generate_seeds_from_the_rows(self, db_session, judge_client):
        """The race-day one. A wrong map here is a wrong bracket.

        The generate route falls back to the ability-rankings pre-seeds when
        the form carries no manual seeds, which is what an operator gets by
        pressing the button without touching the boxes.
        """
        tour, event, entered = self._disagreeing_world(db_session, "GenSeeds")

        response = judge_client.post(
            f"/scheduling/{tour.id}/event/{event.id}/birling/generate")
        assert response.status_code in (200, 302)

        db.session.expire_all()
        seeding = BirlingBracket(event).bracket_data["seeding"]
        assert seeding == [p.id for p in reversed(entered)], (
            "the bracket was seeded from the JSON document")

    def test_the_rankings_page_orders_from_the_rows(
            self, db_session, judge_client):
        """The display one. All four share a team, so they share a school
        group, and the page sorts a school group by seed number."""
        tour, _event, entered = self._disagreeing_world(db_session, "RankSeeds")

        response = judge_client.get(
            f"/scheduling/{tour.id}/pro/ability-rankings")
        assert response.status_code == 200
        body = response.get_data(as_text=True)

        positions = [body.index(p.name) for p in reversed(entered)]
        assert all(p.name in body for p in entered)
        assert positions == sorted(positions), (
            "the page listed the school in the document's order")


# ---------------------------------------------------------------------------
# The pre-seed map
# ---------------------------------------------------------------------------


class TestPreSeedingsComeOffTheRows:
    """``load_pre_seedings`` is narrow on purpose, and this is why."""

    def _pre_seed(self, event, people):
        pre = {str(p.id): index for index, p in enumerate(people, start=1)}
        event.payouts = json.dumps({"pre_seedings": pre})
        rows.project(event)
        db.session.flush()
        return pre

    def test_it_returns_the_map_keyed_by_string(self, db_session):
        _tour, _team, event, people = _world(db_session, "PreSeeds")
        pre = self._pre_seed(event, people[:3])
        assert rows.load_pre_seedings(event) == pre

    def test_no_rows_is_an_empty_map(self, db_session):
        _tour, _team, event, _people = _world(db_session, "NoPreSeeds")
        assert rows.load_pre_seedings(event) == {}

    def test_it_ignores_the_document(self, db_session):
        """The point of the flip, on the pre-seed path."""
        _tour, _team, event, people = _world(db_session, "PreSeedWrecked")
        pre = self._pre_seed(event, people[:3])
        _wreck_the_blob(event, json.dumps({"pre_seedings": {"999": 1}}))
        assert rows.load_pre_seedings(event) == pre

    def test_it_answers_on_a_bracket_it_could_not_render(self, db_session):
        """The reason it is its own function and not ``load_document``.

        An event whose bracket rows will not resolve still has pre-seeds a
        human can read, and the ability-rankings page still needs them. Routing
        this through ``load_document`` would make the seed defaults fail on
        exactly the events that need repairing.

        Broken by removing a seed row, so the surviving seed numbers are
        ``[1, 3, 4]`` and ``load_document`` raises ``SEED_GAP``. That failure
        mode is chosen because it touches nothing the pre-seed rows depend on:
        a construction that broke the roster lookup instead would break the
        pre-seed lookup with it and prove nothing about which one was
        consulted.
        """
        _tour, _team, event, people = _world(db_session, "PreSeedOnBroken")
        pre = self._pre_seed(event, people[:3])
        _generate(event, people[:4])
        db.session.flush()

        gap = BirlingSeed.query.filter_by(event_id=event.id,
                                          seed_number=2).one()
        db.session.delete(gap)
        db.session.flush()

        with pytest.raises(rows.UnloadableBracket) as raised:
            rows.load_document(event)
        assert str(raised.value) == rows.SEED_GAP

        assert rows.load_pre_seedings(event) == pre

    def test_pre_seeds_survive_a_bracket_generated_over_them(self, db_session):
        """Generating a bracket keeps the pre-seed map, on the rows path.

        This is the c63 defect seen from the other side. ``_stored_document``
        discards a payload carrying no ``bracket`` key, so before A3b the first
        generate over a pre-seeded event read an empty document, wrote an empty
        document plus the new bracket, and the pre-seeds were gone. The rows
        reader does not have that hole, because the pre-seed rows are loaded
        whether or not there is a bracket beside them.

        Recorded as behaviour A3b happens to fix rather than as the fix. c63 is
        still open, and an event whose rows were never projected still loses
        its pre-seeds through the fallback path.
        """
        _tour, _team, event, people = _world(db_session, "PreSeedThenBracket")
        pre = self._pre_seed(event, people[:3])
        _generate(event, people[:4])
        db.session.flush()

        assert rows.load_pre_seedings(event) == pre
        assert BirlingBracket(event).bracket_data["bracket"]["winners"]
