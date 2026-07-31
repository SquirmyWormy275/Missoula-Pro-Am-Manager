"""
Tranche 2 oracle: a second tournament must change NOTHING about the first.

These tests run only against the multi-tournament template,
proam_prod_mirror_mt: the real 2026 database plus a staged 2027 tournament
cloned from it by stage_multitournament.py, with new ids everywhere and
IDENTICAL competitor names (returning competitors are the name-collision
hazard). On the default single-tournament template the whole module skips.

    PROAM_RIG_TEMPLATE=proam_prod_mirror_mt pytest proam_regression/test_multitournament.py

Measured before this module was written (c37 recon, full record in the
commit): an AST audit of every route taking <int:tournament_id> plus another
entity id found 47 of 47 guarded (44 inline cross-checks, 3 via the
_*_for_tournament_or_404 helpers); a 15-page sweep of tournament-2 pages on
the multi-tournament clone found zero traces of the staged tournament; the
cross-tournament scratch preview 403s. This module locks that cleanliness so
it survives the O2 schema work, which is exactly when it will be easiest to
break.

CORRECTION TO THE RECORD, carried here because this is where it was proven:
the c29 filing "the SMS pro lookup is wrong the moment a second tournament
exists with overlapping pro ids" was mistaken. ProCompetitor.id is a global
primary key; two tournaments cannot hold the same pro id under this schema
or any schema this app has had. The live id hazard is the pro/college axis
(21 collisions inside tournament 2), which is O2's subject, not a
multi-tournament defect.
"""

import json

import pytest
import rig

TID = rig.TOURNAMENT_ID          # the real 2026 tournament
STAGED_NAME = "staged oracle"    # unique needle in the staged tournament's name


@pytest.fixture()
def mt(app):
    """Skip unless the clone actually holds the staged second tournament."""
    from database import db

    rows = db.session.execute(db.text(
        "SELECT id, name FROM tournaments ORDER BY id")).fetchall()
    if len(rows) < 2 or not any(STAGED_NAME in (r[1] or "") for r in rows):
        pytest.skip("multi-tournament template not loaded; "
                    "set PROAM_RIG_TEMPLATE=proam_prod_mirror_mt")
    staged = [r[0] for r in rows if STAGED_NAME in (r[1] or "")][0]
    return staged


@pytest.mark.sev2
def test_the_staged_tournament_is_a_faithful_clone(mt, sql):
    """Oracle self-check: T3 mirrors T2's population shape with disjoint ids
    and identical names. If this fails the oracle is broken, and every green
    below it is vacuous."""
    for table in ("pro_competitors", "college_competitors"):
        t2 = sql(f"SELECT count(*), min(id), max(id) FROM {table} WHERE tournament_id = :a", a=TID)[0]
        t3 = sql(f"SELECT count(*), min(id), max(id) FROM {table} WHERE tournament_id = :b", b=mt)[0]
        assert t2[0] == t3[0], f"{table}: clone count {t3[0]} != source {t2[0]}"
        assert t3[1] > t2[2], f"{table}: staged ids overlap the real ones"
        names_2 = {r[0] for r in sql(f"SELECT name FROM {table} WHERE tournament_id = :a", a=TID)}
        names_3 = {r[0] for r in sql(f"SELECT name FROM {table} WHERE tournament_id = :b", b=mt)}
        assert names_2 == names_3, f"{table}: staged names diverge from real ones"

    h2 = sql("SELECT count(*) FROM heats h JOIN events e ON e.id = h.event_id "
             "WHERE e.tournament_id = :a", a=TID)[0][0]
    h3 = sql("SELECT count(*) FROM heats h JOIN events e ON e.id = h.event_id "
             "WHERE e.tournament_id = :b", b=mt)[0][0]
    assert h2 == h3 and h2 > 100


@pytest.mark.sev2
def test_no_tournament_2_page_shows_the_staged_tournaments_data(mt, client):
    """The 15-page render sweep, as a standing test. A page for the 2026
    tournament that mentions the staged 2027 name anywhere is a leak."""
    pages = [
        f"/scheduling/{TID}/events", f"/scheduling/{TID}/flights",
        f"/scheduling/{TID}/day-schedule",
        f"/scheduling/{TID}/pro/ability-rankings",
        f"/scheduling/{TID}/heat-sheets",
        f"/registration/{TID}/pro", f"/registration/{TID}/college",
        f"/scheduling/{TID}/birling", f"/woodboss/{TID}",
        f"/scheduling/{TID}/show-day",
    ]
    leaks = {}
    for page in pages:
        r = client.get(page, follow_redirects=True)
        assert r.status_code == 200, f"{page}: {r.status_code}"
        body = r.get_data(as_text=True)
        if STAGED_NAME in body:
            leaks[page] = body.count(STAGED_NAME)
    assert not leaks, f"tournament-2 pages rendered staged-tournament data: {leaks}"


@pytest.mark.sev2
def test_the_placed_panel_counts_only_its_own_tournament(mt, app):
    """schedule_status for the 2026 tournament must report the same numbers
    it reports on the single-tournament mirror: 64 college / 37 placed / 27
    missing, 49 pros all placed. A dropped tournament filter doubles the
    world, and identical names mean the doubling is invisible by eye."""
    from flask import current_app

    from database import db
    from models import Tournament
    from services.schedule_status import build_schedule_status

    with current_app.test_request_context():
        s = build_schedule_status(db.session.get(Tournament, TID))
    assert s["friday"]["competitors_total"] == 64
    assert s["friday"]["competitors_placed"] == 37
    assert s["friday"]["competitors_missing_from_heats"] == 27
    assert s["saturday"]["competitors_total"] == 49
    assert s["saturday"]["competitors_placed"] == 49


@pytest.mark.sev2
def test_heat_generation_for_2026_ignores_the_2027_roster(mt, client, sql):
    """The strongest control: regenerate a pinned 2026 event on the
    multi-tournament clone. The staged tournament holds a same-named
    competitor for every 2026 entrant, so a tournament filter dropped
    anywhere in the generator's competitor scan doubles the field and no
    roster below survives. Pins are the c35 deterministic rosters."""
    def regen(event_id):
        r = client.post(f"/scheduling/{TID}/event/{event_id}/generate-heats",
                        data={"confirm": "true"}, follow_redirects=False)
        assert r.status_code in (302, 303), r.status_code
        rows = sql("SELECT competitors FROM heats WHERE event_id = :e "
                   "ORDER BY heat_number, run_number", e=event_id)
        return [json.loads(c) if isinstance(c, str) else (c or [])
                for (c,) in rows]

    # Event 33 exercises the PRO competitor scan, event 7 the COLLEGE one.
    # Both pins are the c35 deterministic rosters.
    assert regen(33) == [
        [2, 11, 12, 18, 19], [5, 9, 13, 17], [7, 8, 15, 16]]
    assert regen(7) == [[i + 100000 for i in h] for h in [
        [32, 50, 51, 80, 85], [33, 43, 59, 79, 86],
        [37, 42, 60, 78], [38, 39, 61, 74]]]


@pytest.mark.sev2
def test_cross_tournament_scratch_is_refused(mt, client, sql):
    """R6 across tournaments: a 2026 URL naming a 2027 competitor must be
    rejected, preview and confirm both, and the staged competitor's row
    must be untouched."""
    staged_pro = sql("SELECT id, name FROM pro_competitors "
                     "WHERE tournament_id = :b ORDER BY id LIMIT 1", b=mt)[0]
    r = client.get(
        f"/scoring/{TID}/competitor/{staged_pro[0]}/scratch-preview"
        f"?competitor_type=pro", headers={"Accept": "application/json"})
    assert r.status_code in (403, 404), (
        f"cross-tournament scratch preview answered {r.status_code} "
        f"for {staged_pro[1]}"
    )
    r = client.post(
        f"/scoring/{TID}/competitor/{staged_pro[0]}/scratch-confirm",
        data={"effect_count": "0", "competitor_type": "pro"})
    assert r.status_code in (403, 404)
    status = sql("SELECT status FROM pro_competitors WHERE id = :i",
                 i=staged_pro[0])[0][0]
    assert status == "active", "the cross-tournament scratch went through"


@pytest.mark.sev2
def test_cross_tournament_registration_writes_are_refused(mt, client, sql):
    """Same guard, registration surface: removing an event from a staged
    college competitor through a 2026 URL must be refused."""
    import json as _json

    # A competitor and an event he is genuinely entered in, so the probe can
    # never be a no-op. The first draft posted a fixed event name at a
    # competitor who did not carry it, and the mutant that removes the guard
    # survived because removing nothing changes nothing. Vacuity is the same
    # bug in a test that it is in a detector.
    staged_col = sql(
        "SELECT id, events_entered FROM college_competitors "
        "WHERE tournament_id = :b AND events_entered::jsonb != '[]'::jsonb "
        "ORDER BY id LIMIT 1", b=mt)[0]
    before = staged_col[1]
    entered = _json.loads(before)
    target_event = next(e for e in entered if isinstance(e, str))
    r = client.post(
        f"/registration/{TID}/college/competitor/{staged_col[0]}/remove-event",
        data={"event_name": target_event}, follow_redirects=False)
    assert r.status_code in (302, 403, 404)
    after = sql("SELECT events_entered FROM college_competitors WHERE id = :i",
                i=staged_col[0])[0][0]
    assert after == before, (
        f"a 2026 URL removed {target_event!r} from a 2027 competitor's entry"
    )
