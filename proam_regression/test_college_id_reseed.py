"""
O2, cycle c38: the college id reseed, proven end to end on production clones.

Every test here clones the mirror, runs scripts/reseed_college_ids.py
against the clone as a real subprocess, and asserts the outcome with its
own SQL, never by trusting the script's exit code alone. The mutation
battery includes a composite that disables the script's internal post-check
while breaking a remap, precisely so these tests have to stand on their own
measurements.

THE ERA-1 GHOSTS, measured here first (c38): current college ids run 29-92.
The roster was deleted and re-imported mid-2026; ids 1-28 belong to a dead
first era. Both birling brackets (20 references) and the relay state (11
references) still cite era-1 ids, mixed with current ones in the same
seeding lists; Alex Kaper himself is ghost id 1 in the relay pool. The
reseed PRESERVES these bit-for-bit rather than guessing at history: a
mapping keyed on current rows cannot verify which live person a dead id
meant. Cleanup is O6's, with names as the join key and Alex's sign-off.
"""

import json
import os
import subprocess
import sys

import pytest
import rig
from rosters import event_rosters

TID = rig.TOURNAMENT_ID
OFFSET = 100000

# c39: the lane templates are reseeded at the source now. This module's job
# is to keep proving the MIGRATION on real unreseeded data, so it pins its
# clones to the archival pristine snapshot taken before the cutover.
PRISTINE = "proam_prod_mirror_2026pristine"


@pytest.fixture(autouse=True)
def _use_pristine_template(monkeypatch):
    monkeypatch.setattr(rig, "TEMPLATE_DB", PRISTINE)

# Measured on the mirror. The era-1 ghost counts are load-bearing: the
# reseed must neither create nor destroy one.
PRE_COLLISIONS = 21
GHOST_BRACKET_IDS = 20
GHOST_RELAY_IDS = 11
COLLEGE_COUNT = 64
OLD_MAX_ID = 92


def _run_script(dburl, mode):
    env = dict(os.environ, DATABASE_URL=dburl)
    return subprocess.run(
        [sys.executable, "scripts/reseed_college_ids.py", mode],
        cwd=rig.APP_ROOT, env=env, capture_output=True, text=True)


@pytest.mark.sev1
@pytest.mark.slow
def test_the_mirror_still_carries_the_defect_the_reseed_kills(dburl, sql):
    """Adversarial baseline as a standing fact: 21 pro/college collisions
    and the era-1 ghosts, measured fresh on every run. When this fails
    because the template was reseeded at the source, retire the module's
    PRE_ constants and celebrate."""
    p = _run_script(dburl, "--check")
    assert p.returncode == 0, p.stderr
    assert f"collisions             {PRE_COLLISIONS}" in p.stdout, p.stdout
    assert f"orphan_bracket_ids     {GHOST_BRACKET_IDS}" in p.stdout
    assert f"orphan_relay_ids       {GHOST_RELAY_IDS}" in p.stdout


@pytest.mark.sev1
@pytest.mark.slow
def test_the_reseed_kills_every_collision_and_breaks_nothing(dburl, sql):
    p = _run_script(dburl, "--apply")
    assert p.returncode == 0, p.stdout + p.stderr

    # Independent measurements, not the script's word for it.
    assert sql("SELECT count(*) FROM college_competitors c "
               "JOIN pro_competitors p ON p.id = c.id")[0][0] == 0
    assert sql("SELECT count(*), min(id), max(id) FROM college_competitors"
               )[0] == (COLLEGE_COUNT, 29 + OFFSET, OLD_MAX_ID + OFFSET)

    # Every reference class resolves, ghosts exactly preserved.
    assert sql("SELECT count(*) FROM event_results r WHERE "
               "r.competitor_type = 'college' AND r.competitor_id NOT IN "
               "(SELECT id FROM college_competitors)")[0][0] == 0
    assert sql("SELECT count(*) FROM heat_assignments a WHERE "
               "a.competitor_type = 'college' AND a.competitor_id NOT IN "
               "(SELECT id FROM college_competitors)")[0][0] == 0
    orphan_heat = sql("""
        SELECT count(*) FROM (
            SELECT jsonb_array_elements_text(h.competitors::jsonb)::int AS cid
            FROM heats h JOIN events e ON e.id = h.event_id
            WHERE e.event_type = 'college') x
        WHERE cid NOT IN (SELECT id FROM college_competitors)""")[0][0]
    assert orphan_heat == 0

    valid = {r[0] for r in sql("SELECT id FROM college_competitors")}
    ghosts = 0
    for (payouts,) in sql("SELECT payouts FROM events WHERE event_type = "
                          "'college' AND scoring_type = 'bracket' "
                          "AND payouts IS NOT NULL"):
        d = json.loads(payouts or "{}")
        ids = [c.get("id") for c in d.get("competitors") or []]
        ids += list(d.get("seeding") or [])
        ghosts += sum(1 for i in ids if isinstance(i, int) and i not in valid)
    assert ghosts == GHOST_BRACKET_IDS, (
        f"bracket ghost count moved from {GHOST_BRACKET_IDS} to {ghosts}; "
        f"the reseed must preserve era-1 history bit-for-bit")

    # The fence: the next allocated college id lands above the new range.
    nxt = sql("SELECT nextval('college_competitors_id_seq')")[0][0]
    assert nxt > OLD_MAX_ID + OFFSET, f"sequence not fenced: next id {nxt}"


@pytest.mark.sev1
@pytest.mark.slow
def test_the_app_still_works_on_reseeded_data(dburl, client, sql):
    """Route smoke on a reseeded clone: the placed panel keeps its numbers,
    heat regeneration produces the c35 rosters shifted by exactly OFFSET,
    and the birling manage page still serves and rebuilds."""
    p = _run_script(dburl, "--apply")
    assert p.returncode == 0, p.stdout + p.stderr

    from flask import current_app

    from database import db
    from models import Tournament
    from services.schedule_status import build_schedule_status

    db.session.expire_all()
    with current_app.test_request_context():
        s = build_schedule_status(db.session.get(Tournament, TID))
    assert s["friday"]["competitors_total"] == 64
    assert s["friday"]["competitors_placed"] == 37
    assert s["friday"]["competitors_missing_from_heats"] == 27

    r = client.post(f"/scheduling/{TID}/event/7/generate-heats",
                    data={"confirm": "true"}, follow_redirects=False)
    assert r.status_code in (302, 303)
    # D12-C commit F1: the regenerated rosters are read off `heat_assignments`.
    # The pins below are unchanged and must stay unchanged: `event_rosters`
    # orders heats the same way this query did and orders each roster by
    # assignment id, which is the order `set_roster` writes, which is the order
    # the JSON array carried. If a pin moves, the rewrite is wrong, not the pin.
    rosters = event_rosters(sql, 7)
    c35_pins = [[32, 50, 51, 80, 85], [33, 43, 59, 79, 86],
                [37, 42, 60, 78], [38, 39, 61, 74]]
    assert rosters == [[i + OFFSET for i in heat] for heat in c35_pins], rosters

    r = client.get(f"/scheduling/{TID}/event/29/birling")
    assert r.status_code == 200
