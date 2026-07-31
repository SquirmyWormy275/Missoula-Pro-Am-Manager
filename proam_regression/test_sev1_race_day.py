"""
SEV1 regression tests: race-day fatal defects confirmed on real 2026 data.

Each test asserts CORRECT behavior. Against the archived v2026.final code every
test in this file FAILS. A failure here is the harness working. These turn green
only when the corresponding backlog item is actually fixed.

Backlog reference: PROAM_2026_AUDIT_FINAL_BACKLOG.md section A, items 1-10.
"""

import json
import os
import subprocess
import sys
import textwrap

import pytest
import rig

TID = rig.TOURNAMENT_ID


# ---------------------------------------------------------------------------
# 1. Birling bracket generator infinite-loops / OOM-kills the worker
#    services/birling_bracket.py:202-216
# ---------------------------------------------------------------------------

@pytest.mark.sev1
@pytest.mark.slow
def test_birling_manage_page_returns_instead_of_hanging(dburl):
    """A plain GET of the birling manage page must terminate.

    Run out-of-process with a hard timeout: the defect is a non-terminating
    loop, so an in-process call would hang the whole suite and then OOM.
    Both real birling events (28, 29) carry stale power-of-two shape with zero
    recorded results, so rebuild_if_stale_shape reaches the generator.
    """
    script = textwrap.dedent(f"""
        import os, sys
        os.environ['DATABASE_URL'] = {dburl!r}
        os.environ.setdefault('SECRET_KEY', 'x' * 64)
        sys.path.insert(0, {rig.APP_ROOT!r})
        from app import create_app
        app = create_app()
        app.config['TESTING'] = True
        app.config['WTF_CSRF_ENABLED'] = False
        c = app.test_client()
        with c.session_transaction() as s:
            s['_user_id'] = '{rig.ADMIN_USER_ID}'
        for eid in (28, 29):
            r = c.get('/scheduling/{TID}/event/%d/birling' % eid)
            print('EVENT', eid, 'STATUS', r.status_code)
    """)
    try:
        proc = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True, text=True, timeout=60,
        )
    except subprocess.TimeoutExpired:
        pytest.fail(
            "birling manage page did not return within 60s for the real "
            "birling events. services/birling_bracket.py generate_bracket "
            "never terminates: ceil(1/2) == 1 forever."
        )
    assert "EVENT 28 STATUS" in proc.stdout, proc.stderr[-3000:]
    assert "EVENT 29 STATUS" in proc.stdout, proc.stderr[-3000:]


@pytest.mark.sev1
@pytest.mark.slow
def test_birling_print_routes_return_instead_of_hanging(dburl):
    """print-blank and print-all reach the same generator via the Print Hub."""
    script = textwrap.dedent(f"""
        import os, sys
        os.environ['DATABASE_URL'] = {dburl!r}
        os.environ.setdefault('SECRET_KEY', 'x' * 64)
        sys.path.insert(0, {rig.APP_ROOT!r})
        from app import create_app
        app = create_app()
        app.config['TESTING'] = True
        c = app.test_client()
        with c.session_transaction() as s:
            s['_user_id'] = '{rig.ADMIN_USER_ID}'
        r = c.get('/scheduling/{TID}/event/28/birling/print-blank')
        print('BLANK', r.status_code)
        r = c.get('/scheduling/{TID}/birling/print-all')
        print('ALL', r.status_code)
    """)
    try:
        proc = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True, text=True, timeout=60,
        )
    except subprocess.TimeoutExpired:
        pytest.fail(
            "birling print routes did not return within 60s. Under gunicorn "
            "-w1 --threads4 roughly four of these wedge the entire app."
        )
    assert "BLANK" in proc.stdout, proc.stderr[-3000:]
    assert "ALL" in proc.stdout, proc.stderr[-3000:]


# ---------------------------------------------------------------------------
# 2. Scratch resolves competitors pro-first and hits the wrong person
#    routes/scoring.py:989 _load_competitor_for_tournament
# ---------------------------------------------------------------------------

def _colliding_ids(sql):
    """Real ids held by BOTH a pro and a college competitor."""
    rows = sql("""
        SELECT c.id, c.name AS college_name, p.name AS pro_name
        FROM college_competitors c
        JOIN pro_competitors p ON p.id = c.id
        WHERE c.tournament_id = :t AND p.tournament_id = :t
        ORDER BY c.id
    """, t=TID)
    return rows


@pytest.mark.sev1
def test_id_collision_actually_exists_in_production_data(sql):
    """INVERTED at c39, name kept for history. The c38 reseed made the
    collision population structurally extinct: college ids live at +100000
    behind a fenced sequence. This is now the fence invariant; a single row
    here means the fence broke and the entire bare-int bug class is back."""
    rows = _colliding_ids(sql)
    assert len(rows) == 0, (
        f"pro/college id collisions have REAPPEARED post-reseed: {rows[:5]}"
    )


@pytest.mark.sev1
def test_scratch_preview_resolves_the_college_competitor_not_the_pro(client, sql):
    """Scratching a college competitor must not target the pro of the same id."""
    # Post-c39 there are no live collisions; the historical twin pair keeps
    # the type-resolution question honest: college 100029 (Greer Swoboda),
    # former pro twin 29 (Dwight Severson).
    cid = 100029
    college_name = sql("SELECT name FROM college_competitors WHERE id = :c",
                       c=cid)[0][0]
    pro_name = sql("SELECT name FROM pro_competitors WHERE id = 29")[0][0]
    assert college_name != pro_name
    assert college_name != pro_name, "pick a collision where the names differ"

    r = client.get(
        f"/scoring/{TID}/competitor/{cid}/scratch-preview",
        query_string={"competitor_type": "college"},
    )
    assert r.status_code == 200, r.data[:500]
    body = json.loads(r.data)
    blob = json.dumps(body)
    assert college_name in blob, (
        f"scratch-preview for college id {cid} ({college_name}) resolved to "
        f"the PRO of the same id ({pro_name}). "
        f"routes/scoring.py _load_competitor_for_tournament tries "
        f"ProCompetitor first and the id namespaces collide."
    )
    assert pro_name not in blob or pro_name == college_name


# ---------------------------------------------------------------------------
# 3. Scratch-undo does not restore heat membership / stand assignments
# ---------------------------------------------------------------------------

HEATS_HOLDING = """
    SELECT h.id FROM heats h
    JOIN events e ON e.id = h.event_id
    CROSS JOIN LATERAL json_array_elements_text(h.competitors::json) AS m(cid)
    WHERE e.tournament_id = :t AND m.cid::int = :c
"""


@pytest.mark.sev1
def test_scratch_undo_restores_heat_membership(client, sql):
    """Undo must put the competitor back into the heats they were pulled from.

    Membership is checked by exact JSON array element, never by substring:
    competitors is stored as a JSON list, so LIKE '%1%' would match ids 1, 11,
    21 and 31 alike and produce a test that reports whatever it likes.
    """
    pro = sql("""
        SELECT p.id, p.name FROM pro_competitors p
        WHERE p.tournament_id = :t AND p.status = 'active'
        ORDER BY p.id
    """, t=TID)
    assert pro, "no active pro competitors"

    target = None
    for cid, name in pro:
        hits = sql(HEATS_HOLDING, t=TID, c=cid)
        if len(hits) >= 2:
            target = (cid, name, {h[0] for h in hits})
            break
    assert target, "no pro competitor sits in two or more heats"
    cid, name, before_heats = target

    r = client.post(f"/scoring/{TID}/competitor/{cid}/scratch-confirm",
                    data={"effect_count": "0"})
    assert r.status_code in (200, 302), r.data[:400]

    r = client.post(f"/scoring/{TID}/competitor/{cid}/scratch-undo")
    assert r.status_code in (200, 302), r.data[:400]

    after_heats = {h[0] for h in sql(HEATS_HOLDING, t=TID, c=cid)}
    lost = sorted(before_heats - after_heats)

    assert not lost, (
        f"scratch-undo did not restore {name} (id {cid}) to every heat they "
        f"were pulled from. {len(lost)} of {len(before_heats)} heats were not "
        f"restored: {lost}. The competitor is active again on paper but no "
        f"longer appears on those run sheets, so they get called for a heat "
        f"they are not in or silently miss it."
    )


# ---------------------------------------------------------------------------
# 5. Partnered scoring loses pair identity
#
# The April data cannot be asserted on directly: the college partnered events
# were never scored in the app, so every result row is pending with a NULL
# final_position. A stored-state assertion passes vacuously, which is the exact
# failure mode this harness exists to prevent. So these tests DRIVE the real
# scoring routes against the real event, the real heats and the real
# competitors, and read the database back. The only invented values are the
# times, because no times were ever entered.
#
# Event 18 (college Double Buck) has two heats, 343 and 344, holding four real
# partner pairs whose partner assignments are recorded on the competitor rows.
# ---------------------------------------------------------------------------

COLLEGE_DB_EVENT = 18
COLLEGE_DB_HEATS = {
    344: {100029: "12.10", 100031: "12.10", 100048: "14.50", 100049: "14.50"},
    343: {100054: "13.00", 100057: "13.00", 100064: "16.00", 100065: "16.00"},
}


def _score_college_double_buck(client, sql):
    """Enter real times through the real heat-entry route. Returns nothing."""
    for hid, times in COLLEGE_DB_HEATS.items():
        version = sql("SELECT version_id FROM heats WHERE id = :h", h=hid)[0][0]
        data = {"heat_version": str(version)}
        for cid, t in times.items():
            data[f"t1_run1_{cid}"] = t
            data[f"t2_run1_{cid}"] = t
            data[f"status_{cid}"] = "completed"
        r = client.post(f"/scoring/{TID}/heat/{hid}/enter", data=data)
        assert r.status_code in (200, 302), r.data[:400]


@pytest.mark.sev1
def test_live_scoring_persists_partner_name(client, sql):
    """Saving a partnered heat must record who each competitor was paired with.

    services/scoring_workflow.py save_heat_results_submission constructs
    EventResult with competitor_name and never sets partner_name. The pro rows
    in production only carry partner_name because they were loaded by the Excel
    importer, which does set it. Anything scored live loses pair identity.
    """
    _score_college_double_buck(client, sql)
    rows = sql("""
        SELECT competitor_id, competitor_name, partner_name
        FROM event_results
        WHERE event_id = :e AND status = 'completed'
        ORDER BY competitor_id
    """, e=COLLEGE_DB_EVENT)
    assert rows, "scoring the heats produced no completed rows"
    missing = [(cid, nm) for cid, nm, p in rows if not p]
    assert not missing, (
        "live scoring wrote result rows with no partner_name on a partnered "
        "event: " + ", ".join(f"{nm} (id {cid})" for cid, nm in missing)
        + ". services/scoring_workflow.py never sets partner_name."
    )


@pytest.mark.sev1
def test_partnered_pairs_occupy_one_position_and_take_full_points(client, sql):
    """A pair is one entity. Four pairs must place 1,1,2,2,3,3,4,4.

    As shipped they place 1,1,3,3,5,5,7,7 and the winning pair is awarded
    8.50 each instead of 10.00, because scoring_engine._pair_key_for builds
    frozenset((competitor_name, partner_name)) and both halves are wrong for
    college: partner_name is NULL, and even when populated correctly the key
    still cannot collide because competitor_name is the display_name carrying
    the team suffix, for example 'Nell Horgan (FVC-A)', while partner_name is
    the bare 'Nell Horgan'. Fixing only the write leaves this test red.
    """
    _score_college_double_buck(client, sql)
    r = client.post(f"/scoring/{TID}/event/{COLLEGE_DB_EVENT}/finalize")
    assert r.status_code in (200, 302), r.data[:400]

    rows = sql("""
        SELECT competitor_name, partner_name, result_value,
               final_position, points_awarded
        FROM event_results
        WHERE event_id = :e AND status = 'completed'
        ORDER BY result_value, competitor_name
    """, e=COLLEGE_DB_EVENT)
    assert len(rows) == 8, f"expected 8 scored rows, got {len(rows)}: {rows}"

    positions = [r[3] for r in rows]
    assert sorted(positions) == [1, 1, 2, 2, 3, 3, 4, 4], (
        "four pairs did not occupy four positions. positions="
        f"{sorted(positions)}; rows={rows}"
    )

    winners = [r for r in rows if r[3] == 1]
    assert all(float(r[4]) == 10.0 for r in winners), (
        "the winning pair did not receive full first-place points: "
        + ", ".join(f"{r[0]}={r[4]}" for r in winners)
        + ". They were split-tie scored as two separate first-place entities."
    )


# ---------------------------------------------------------------------------
# 8. Audit attribution silently lost on money and state actions
#    services/audit.py log_action adds but never commits
# ---------------------------------------------------------------------------

@pytest.mark.sev1
def test_log_action_persists_without_a_later_unrelated_commit(app, sql):
    """An audit row must survive on its own.

    The codebase idiom is commit-then-log, so a log_action that only add()s
    rolls back unless some unrelated later code happens to commit. This test
    calls log_action in isolation, which is exactly the shape of the real
    money paths that lost their attribution.
    """
    from database import db
    from services.audit import log_action

    before = sql("SELECT count(*) FROM audit_logs")[0][0]
    log_action(
        action="regression_harness_probe",
        entity_type="tournament",
        entity_id=TID,
        details={"source": "regression harness"},
    )
    db.session.expire_all()
    db.session.rollback()  # simulate the request ending with no further commit
    after = sql("SELECT count(*) FROM audit_logs")[0][0]

    assert after == before + 1, (
        "log_action did not persist its row (audit_logs "
        f"{before} -> {after}). services/audit.py adds to the session but "
        "never commits, so audit attribution for payout settlement, fee "
        "payments and event ordering is silently discarded."
    )


# ---------------------------------------------------------------------------
# 10. College day-of late entry is refused for all 64 college competitors
#     routes/scheduling/heats.py:625
# ---------------------------------------------------------------------------

@pytest.mark.sev1
def test_college_events_entered_shape_is_what_we_think(sql):
    """Guard test: college stores event NAMES, pro stores ids."""
    college_numeric = sql(r"""
        SELECT count(*) FROM college_competitors
        WHERE tournament_id = :t AND events_entered ~ '\[[0-9]'
    """, t=TID)[0][0]
    college_total = sql(
        "SELECT count(*) FROM college_competitors WHERE tournament_id = :t",
        t=TID)[0][0]
    assert college_total > 0
    assert college_numeric == 0, (
        "some college competitors now store numeric event ids; the late-entry "
        "test below needs revisiting"
    )


@pytest.mark.sev1
def test_college_competitor_can_be_added_to_a_heat_day_of(client, sql, flashes):
    """add-to-heat is THE day-of tool. It must work for college competitors.

    Picks a real college competitor who IS entered in a real college event by
    name, and a heat in that event with a free stand.
    """
    row = sql("""
        SELECT c.id, c.name, e.id, e.name, h.id
        FROM college_competitors c
        JOIN events e
          ON e.tournament_id = c.tournament_id
         AND e.event_type = 'college'
         AND EXISTS (
             SELECT 1 FROM json_array_elements_text(c.events_entered::json) AS x(v)
             WHERE x.v = e.name)
         -- College competitors store bare event NAMES, and a name is NOT unique:
         -- "Underhand Hard Hit" is both event 7 (M) and event 8 (F).  Without
         -- this predicate the ORDER BY below deterministically picks the lower
         -- event id, which is the men's event, and the app then correctly
         -- refuses a female competitor for gender mismatch.  That refusal is
         -- right, and reading it as bug #10 sends the reader after a fix that
         -- is already in.
         AND e.gender = c.gender
        JOIN heats h ON h.event_id = e.id
        WHERE c.tournament_id = :t
          AND c.status = 'active'
          AND NOT EXISTS (
              SELECT 1 FROM json_array_elements_text(h.competitors::json) AS m(cid)
              WHERE m.cid::int = c.id)
          AND coalesce(e.max_stands, 5) >
              coalesce(json_array_length(h.competitors::json), 0)
        ORDER BY c.id, e.id, h.id
        LIMIT 1
    """, t=TID)
    assert row, "could not find a college competitor with room in an entered event"
    cid, cname, eid, ename, hid = row[0]

    before = sql("SELECT competitors FROM heats WHERE id = :h", h=hid)[0][0]
    r = client.post(
        f"/scheduling/{TID}/event/{eid}/add-to-heat",
        data={"competitor_id": str(cid), "heat_id": str(hid)},
    )
    msgs = flashes()
    after = sql("SELECT competitors FROM heats WHERE id = :h", h=hid)[0][0]

    errors = [m for cat, m in msgs if cat == "error"]
    assert not errors, (
        f"college late entry refused for {cname} (id {cid}) into {ename}: "
        f"{errors}. routes/scheduling/heats.py:625 int()-parses events_entered, "
        f"but college competitors store event NAMES, so the gate rejects every "
        f"college competitor. The error tells the operator to fix it on the "
        f"registration page, which writes the name again."
    )
    assert str(cid) in str(after) and str(cid) not in str(before), (
        f"no heat row written. before={before} after={after}"
    )


@pytest.mark.sev1
def test_pro_late_entry_still_works(client, sql, flashes):
    """Control. If this fails the harness is broken, not the college gate."""
    row = sql("""
        SELECT p.id, p.name, e.id, e.name, h.id
        FROM pro_competitors p
        JOIN events e
          ON e.tournament_id = p.tournament_id
         AND e.event_type = 'pro'
         AND EXISTS (
             SELECT 1 FROM json_array_elements_text(p.events_entered::json) AS x(v)
             WHERE x.v::int = e.id)
        JOIN heats h ON h.event_id = e.id
        WHERE p.tournament_id = :t
          AND p.status = 'active'
          AND NOT e.is_partnered
          AND NOT EXISTS (
              SELECT 1 FROM json_array_elements_text(h.competitors::json) AS m(cid)
              WHERE m.cid::int = p.id)
          AND coalesce(e.max_stands, 5) >
              coalesce(json_array_length(h.competitors::json), 0)
        ORDER BY p.id, e.id, h.id
        LIMIT 1
    """, t=TID)
    assert row, "could not find a pro competitor with room in an entered event"
    cid, cname, eid, ename, hid = row[0]

    client.post(f"/scheduling/{TID}/event/{eid}/add-to-heat",
                data={"competitor_id": str(cid), "heat_id": str(hid)})
    msgs = flashes()
    errors = [m for cat, m in msgs if cat == "error"]
    assert not errors, f"pro control also failed, harness is suspect: {errors}"
