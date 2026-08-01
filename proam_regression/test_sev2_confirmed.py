"""
SEV2 regression tests: workflow-breaking defects confirmed on real 2026 data.

Same contract as the SEV1 file. Every test asserts CORRECT behavior, so every
test in here FAILS against v2026.final. Backlog items 11 through 16.

Two tests create a user account (spectator, viewer). That is a real operation
performed through the real admin route, not a fixture: the production database
holds exactly one user, so there is no read-only account to borrow.
"""

import json
import re

import pytest
import rig
from population import Claim, register

TID = rig.TOURNAMENT_ID


def _loads(value):
    """psycopg returns json columns as dicts and text columns as strings."""
    return json.loads(value) if isinstance(value, str) else value


# ---------------------------------------------------------------------------
# 11. Heat undo destroys partner_name on partnered pro events
#     routes/scoring.py heat undo deletes EventResult rows wholesale
# ---------------------------------------------------------------------------

PRO_DB_EVENT = 38          # pro Double Buck
PRO_DB_HEAT = 425          # competitors [23, 21, 24, 27, 45, 28, 48]
PRO_DB_TIMES = {23: "9.10", 21: "9.10", 24: "10.20", 27: "10.20"}

# O5: the roster in the comment above is a claim about heat 425, and every
# key in PRO_DB_TIMES rides on it. Registered against the stored row.
register(Claim(
    name="PRO_DB_HEAT 425 holds exactly the seven-man Double Buck roster",
    claimed=[23, 21, 24, 27, 45, 28, 48],
    sql=("SELECT competitors FROM heats "
         "WHERE id = :h AND event_id = :e"),
    params={"h": PRO_DB_HEAT, "e": PRO_DB_EVENT},
    shape=lambda rows: (json.loads(rows[0][0])
                        if rows and isinstance(rows[0][0], str)
                        else (rows[0][0] if rows else [])),
))


def _score_pro_double_buck(client, sql, heat_id=PRO_DB_HEAT, times=None):
    times = times or PRO_DB_TIMES
    version = sql("SELECT version_id FROM heats WHERE id = :h", h=heat_id)[0][0]
    data = {"heat_version": str(version)}
    for cid, t in times.items():
        data[f"t1_run1_{cid}"] = t
        data[f"t2_run1_{cid}"] = t
        data[f"status_{cid}"] = "completed"
    r = client.post(f"/scoring/{TID}/heat/{heat_id}/enter", data=data)
    assert r.status_code in (200, 302), r.data[:400]


@pytest.mark.sev2
def test_heat_undo_then_reentry_preserves_partner_name(client, sql):
    """Undo is a routine correction. It must not destroy pair identity.

    The pro partnered rows in production carry partner_name only because the
    Excel importer wrote it. Undo deletes the rows; re-entry recreates them
    through the live path, which never writes partner_name. The pair then
    scores as two tied individuals and the payout moves.
    """
    before = dict(sql("""
        SELECT competitor_id, partner_name FROM event_results
        WHERE event_id = :e AND partner_name IS NOT NULL
    """, e=PRO_DB_EVENT))
    assert before, "no imported partner_name rows to lose"

    _score_pro_double_buck(client, sql)
    r = client.post(f"/scoring/{TID}/heat/{PRO_DB_HEAT}/undo")
    assert r.status_code in (200, 302), r.data[:400]
    _score_pro_double_buck(client, sql)

    after = dict(sql("""
        SELECT competitor_id, partner_name FROM event_results
        WHERE event_id = :e
    """, e=PRO_DB_EVENT))

    lost = {cid: nm for cid, nm in before.items()
            if cid in PRO_DB_TIMES and not after.get(cid)}
    assert not lost, (
        "heat undo plus re-entry erased partner_name for "
        + ", ".join(f"id {cid} (was paired with {nm})" for cid, nm in lost.items())
        + ". Undo deletes EventResult rows wholesale and the live scoring path "
        "recreates them without partner_name, so the pair scores as two tied "
        "individuals and the payout position shifts."
    )


# ---------------------------------------------------------------------------
# 12. Read-only roles can write money
#     app.py BLUEPRINT_PERMISSIONS maps reporting -> can_report, which
#     models/user.py:97 grants to spectator and viewer
# ---------------------------------------------------------------------------

def _make_user(client, username, role):
    """Create a real account through the real admin route."""
    r = client.post("/auth/users", data={
        "username": username,
        "password": "harness-password-123",
        "role": role,
        "display_name": username,
    })
    assert r.status_code in (200, 302), r.data[:400]


@pytest.mark.sev2
@pytest.mark.parametrize("role", ["spectator", "viewer"])
def test_read_only_role_cannot_write_fee_state(app, client, sql, role):
    """A spectator must not be able to mark entry fees paid.

    routes/reporting.py fee_tracker has no inner role gate, and the whole
    reporting blueprint is admitted on can_report, which includes both
    read-only roles.
    """
    username = f"harness_{role}"
    _make_user(client, username, role)

    created = sql("SELECT id, role FROM users WHERE username = :u", u=username)
    assert created, f"the admin route did not create the {role} account"
    uid, stored_role = created[0]
    # Guard against a vacuous pass: if the account were silently created as an
    # admin, a successful write would prove nothing.
    assert uid != rig.ADMIN_USER_ID, "created account collided with the admin"
    assert stored_role == role, (
        f"asked for role={role}, the app stored role={stored_role}")

    target = sql("""
        SELECT id, name, fees_paid FROM pro_competitors
        WHERE tournament_id = :t AND status = 'active'
        ORDER BY id LIMIT 1
    """, t=TID)[0]
    cid, cname, fees_before = target

    ro = app.test_client()
    with ro.session_transaction() as sess:
        sess["_user_id"] = str(uid)
        sess["_fresh"] = True

    resp = ro.post(f"/reporting/{TID}/pro/fee-tracker",
                   data={"competitor_id": str(cid), "action": "mark_all_paid"})
    fees_after = sql("SELECT fees_paid FROM pro_competitors WHERE id = :c",
                     c=cid)[0][0]

    assert fees_after == fees_before, (
        f"a {role} changed fee payment state for {cname} (id {cid}). "
        f"before={fees_before} after={fees_after}, response {resp.status_code}. "
        f"models/user.py can_report grants {role} the reporting blueprint and "
        f"fee_tracker has no inner write gate."
    )


# ---------------------------------------------------------------------------
# 13. Routine pro-detail save deletes the relay lottery fee
#     routes/registration.py:676 update_pro_events
# ---------------------------------------------------------------------------

@pytest.mark.sev2
def test_pro_detail_save_preserves_the_relay_entry_fee(client, sql):
    """Re-saving a pro's detail page must not silently drop the relay fee.

    entry_fees carries numeric event-id keys plus one non-numeric 'relay' key.
    update_pro_events rebuilds the dict by iterating pro_events only, so the
    relay key cannot survive. The fee tracker can then never settle it.
    """
    row = sql("""
        SELECT id, name, entry_fees FROM pro_competitors
        WHERE tournament_id = :t AND entry_fees::text LIKE '%relay%'
        ORDER BY id LIMIT 1
    """, t=TID)
    assert row, "no pro competitor carries a relay entry fee in this data"
    cid, cname, fees_raw = row[0]
    fees_before = json.loads(fees_raw) if isinstance(fees_raw, str) else fees_raw
    assert "relay" in fees_before

    entered, paid_raw, partners_raw, gear_raw = sql("""
        SELECT events_entered, fees_paid, partners, gear_sharing
        FROM pro_competitors WHERE id = :c
    """, c=cid)[0]
    entered = _loads(entered) or []
    paid = _loads(paid_raw) or {}
    partners = _loads(partners_raw) or {}
    gear = _loads(gear_raw) or {}

    # Replay the whole form exactly as competitor_detail.html posts it: same
    # events, same fees, same paid flags, same partners, same gear. Nothing is
    # edited. A save with no changes must be a no-op.
    data = {"event_ids": [str(eid) for eid in entered]}
    for k, v in fees_before.items():
        if k != "relay":
            data[f"fee_{k}"] = str(v)
    for k, v in paid.items():
        if v:
            data[f"paid_{k}"] = "on"
    for k, v in partners.items():
        data[f"partner_{k}"] = v
    for k, v in gear.items():
        data[f"gear_{k}"] = v

    r = client.post(f"/registration/{TID}/pro/{cid}/update-events", data=data)
    assert r.status_code in (200, 302), r.data[:400]

    fees_raw_after = sql("SELECT entry_fees FROM pro_competitors WHERE id = :c",
                         c=cid)[0][0]
    fees_after = (json.loads(fees_raw_after)
                  if isinstance(fees_raw_after, str) else fees_raw_after)

    assert "relay" in fees_after, (
        f"saving {cname}'s detail page with no changes deleted the relay fee. "
        f"before={fees_before} after={fees_after}. update_pro_events rebuilds "
        f"entry_fees from numeric event ids only."
    )


# ---------------------------------------------------------------------------
# 14. Partial dual-timer entry auto-finalizes the event short
#     services/scoring_workflow.py:277-285
# ---------------------------------------------------------------------------

PARTIAL_EVENT = 13   # Standing Block Speed, single heat 386, competitors [30, 35]
PARTIAL_HEAT = 386


@pytest.mark.sev2
def test_partial_timer_entry_does_not_auto_finalize_the_event(client, sql):
    """One timer entered is an incomplete result, not a finished event.

    Entering only t1 leaves the row status='partial'. The workflow then sets
    heat.status='completed' unconditionally and auto-finalizes with no partial
    check, so the competitor lands at position None with 0.00 points and
    disappears from the public results API, which filters status=='completed'.
    """
    version = sql("SELECT version_id FROM heats WHERE id = :h",
                  h=PARTIAL_HEAT)[0][0]
    comps = sql("SELECT competitors FROM heats WHERE id = :h", h=PARTIAL_HEAT)[0][0]
    comps = json.loads(comps) if isinstance(comps, str) else comps
    assert len(comps) >= 2, comps
    whole, partial = comps[0], comps[1]

    data = {
        "heat_version": str(version),
        f"t1_run1_{whole}": "12.00",
        f"t2_run1_{whole}": "12.00",
        f"status_{whole}": "completed",
        f"t1_run1_{partial}": "13.00",   # second timer deliberately missing
        f"status_{partial}": "completed",
    }
    r = client.post(f"/scoring/{TID}/heat/{PARTIAL_HEAT}/enter", data=data)
    assert r.status_code in (200, 302), r.data[:400]

    finalized = sql("SELECT is_finalized FROM events WHERE id = :e",
                    e=PARTIAL_EVENT)[0][0]
    rows = sql("""
        SELECT competitor_id, status, final_position, points_awarded
        FROM event_results WHERE event_id = :e ORDER BY competitor_id
    """, e=PARTIAL_EVENT)

    assert not finalized, (
        "the event auto-finalized while a competitor's result was still "
        f"partial. rows={rows}. services/scoring_workflow.py sets "
        "heat.status='completed' unconditionally and finalizes without "
        "consulting validate_finalization, which has no partial check either."
    )


def _enter_heat(client, sql, heat_id, fields):
    """POST the scoring form with a freshly read optimistic-lock version."""
    version = sql("SELECT version_id FROM heats WHERE id = :h", h=heat_id)[0][0]
    data = {"heat_version": str(version)}
    data.update(fields)
    r = client.post(f"/scoring/{TID}/heat/{heat_id}/enter", data=data)
    assert r.status_code in (200, 302), r.data[:400]
    return r


@pytest.mark.sev2
def test_a_clean_dual_timer_heat_still_auto_finalizes(client, sql):
    """Positive control. Do not fix the partial bug by killing auto-finalize.

    The cheapest way to make the test above pass is to stop auto-finalizing at
    all, or to gate it on something that is never true on real data. Both leave
    the operator finalizing every event by hand on race day and neither test
    above would notice.

    Same heat, same competitors, both timers present for both. This must still
    finalize by itself, with real positions and real points, exactly as it does
    on v2026.final. It passes before the fix and it has to keep passing after.
    """
    comps = _loads(sql("SELECT competitors FROM heats WHERE id = :h",
                       h=PARTIAL_HEAT)[0][0])
    assert len(comps) >= 2, comps
    fast, slow = comps[0], comps[1]

    _enter_heat(client, sql, PARTIAL_HEAT, {
        f"t1_run1_{fast}": "12.00", f"t2_run1_{fast}": "12.00",
        f"status_{fast}": "completed",
        f"t1_run1_{slow}": "13.00", f"t2_run1_{slow}": "13.00",
        f"status_{slow}": "completed",
    })

    finalized = sql("SELECT is_finalized FROM events WHERE id = :e",
                    e=PARTIAL_EVENT)[0][0]
    rows = dict((r[0], r) for r in sql("""
        SELECT competitor_id, status, final_position, points_awarded
        FROM event_results WHERE event_id = :e
    """, e=PARTIAL_EVENT))

    assert finalized, (
        "a heat with every timer entered did not auto-finalize. rows="
        f"{list(rows.values())}. The partial guard has been written too wide "
        "and now blocks the normal path."
    )
    assert rows[fast][1] == "completed" and rows[slow][1] == "completed", rows
    assert rows[fast][2] == 1, (
        f"the faster competitor is at position {rows[fast][2]}, not 1. rows="
        f"{list(rows.values())}")
    assert rows[slow][2] == 2, (
        f"the slower competitor is at position {rows[slow][2]}, not 2. rows="
        f"{list(rows.values())}")


@pytest.mark.sev2
def test_supplying_the_missing_timer_finalizes_the_event(client, sql, flashes):
    """Positive control. Blocking finalize must be a deferral, not a dead end.

    Two lazy fixes survive the defect test on their own. One poisons the event
    the moment it sees a partial row, so entering the second timer never
    finalizes it and the operator is stuck with no in-app way out. The other
    finalizes anyway while dropping the partial competitor from placement,
    which is the shipped bug wearing a different hat.

    So: enter the partial, watch it not finalize, then enter the timer that was
    missing and demand the event finalizes with BOTH competitors holding a real
    position, real points, and a place in the public results the spectators
    read. The second half is the half that matters and it can only be observed
    after the fix.
    """
    comps = _loads(sql("SELECT competitors FROM heats WHERE id = :h",
                       h=PARTIAL_HEAT)[0][0])
    assert len(comps) >= 2, comps
    whole, partial = comps[0], comps[1]

    _enter_heat(client, sql, PARTIAL_HEAT, {
        f"t1_run1_{whole}": "12.00", f"t2_run1_{whole}": "12.00",
        f"status_{whole}": "completed",
        f"t1_run1_{partial}": "13.00",   # judge's second stopwatch not read yet
        f"status_{partial}": "completed",
    })

    assert not sql("SELECT is_finalized FROM events WHERE id = :e",
                   e=PARTIAL_EVENT)[0][0], (
        "finalized while a row was still partial; see the test above")
    assert sql("""SELECT status FROM event_results
                  WHERE event_id = :e AND competitor_id = :c""",
               e=PARTIAL_EVENT, c=partial)[0][0] == "partial", (
        "the half-entered row is not stored as partial, so the rest of this "
        "test is not measuring what it claims to measure")

    # A silent deferral is barely better than a silent finalize. The operator
    # is standing at the entry screen watching the flash bar and nothing else.
    told = [(cat, msg) for cat, msg in flashes()
            if "not finalized" in msg.lower() or "one timer" in msg.lower()]
    assert told, (
        "the event quietly refused to finalize and the save still flashed the "
        "ordinary success message. The operator walks away believing the "
        f"event is published. flashes={flashes()}")
    assert any(cat == "warning" for cat, _ in told), told
    partial_name = sql("""SELECT competitor_name FROM event_results
                          WHERE event_id = :e AND competitor_id = :c""",
                       e=PARTIAL_EVENT, c=partial)[0][0]
    assert any(partial_name in msg for _, msg in told), (
        f"the warning does not name {partial_name!r}, so the operator has to "
        f"hunt for which row is short. told={told}")

    # The judge reads the second stopwatch and re-saves. Both competitors go
    # back in, because the real form posts every competitor in the heat.
    _enter_heat(client, sql, PARTIAL_HEAT, {
        f"t1_run1_{whole}": "12.00", f"t2_run1_{whole}": "12.00",
        f"status_{whole}": "completed",
        f"t1_run1_{partial}": "13.00", f"t2_run1_{partial}": "13.00",
        f"status_{partial}": "completed",
    })

    rows = dict((r[0], r) for r in sql("""
        SELECT competitor_id, status, final_position, points_awarded
        FROM event_results WHERE event_id = :e
    """, e=PARTIAL_EVENT))

    assert sql("SELECT is_finalized FROM events WHERE id = :e",
               e=PARTIAL_EVENT)[0][0], (
        "the missing timer was supplied and the event still refuses to "
        f"finalize. rows={list(rows.values())}. Blocking auto-finalize on a "
        "partial row has to lift when the row stops being partial, otherwise "
        "one slow stopwatch kills the event for the rest of the show.")

    assert rows[partial][1] == "completed", rows[partial]
    assert rows[partial][2] is not None, (
        f"the competitor who was briefly partial finalized at position None. "
        f"rows={list(rows.values())}")
    assert rows[whole][2] == 1 and rows[partial][2] == 2, (
        f"12.00 should beat 13.00. rows={list(rows.values())}")

    body = client.get(f"/api/public/tournaments/{TID}/results").get_json()
    published = {
        r["competitor_id"]
        for e in body["results"] if e["event_id"] == PARTIAL_EVENT
        for r in e["results"]
    }
    assert published, (
        "event 13 published no results at all. The public API filters on "
        "events.status == 'completed' (routes/api.py:183), which is a "
        "different gate from is_finalized, so it is worth seeing separately.")
    assert partial in published, (
        f"competitor {partial} finalized at position {rows[partial][2]} but is "
        f"absent from the public results feed. published={published}")



# Pro Standing Block. Two heats, one run, not partnered, not handicap, so the
# only thing that separates it from event 13 above is that the partial row and
# the heat being saved can be different heats.
CROSS_HEAT_EVENT = 34
CROSS_HEAT_A = 464   # competitors [2, 11, 12, 9, 13]
CROSS_HEAT_B = 465   # competitors [5, 8, 16, 18, 15]


@pytest.mark.sev2
def test_a_partial_row_in_another_heat_still_blocks_finalize(client, sql):
    """Positive control. The guard is event-scoped, and it has to stay that way.

    Event 13 has exactly one heat, so every test above passes identically
    whether the partial check looks at the event's results or only at the rows
    in the heat being saved. Those are very different guards. Auto-finalize
    fires when the LAST heat of an event completes, so the heat that trips it
    is usually not the heat holding the bad row.

    Here the short row is left in heat 464 and heat 465 is the one that
    completes the event. A heat-scoped guard sees nothing wrong in 465 and
    publishes the event with a competitor at position None, which is the
    original bug with an extra step.
    """
    a = _loads(sql("SELECT competitors FROM heats WHERE id = :h",
                   h=CROSS_HEAT_A)[0][0])
    b = _loads(sql("SELECT competitors FROM heats WHERE id = :h",
                   h=CROSS_HEAT_B)[0][0])
    assert len(a) >= 2 and len(b) >= 2, (a, b)
    short = a[0]

    fields_a = {}
    for i, cid in enumerate(a):
        fields_a[f"t1_run1_{cid}"] = f"{20 + i}.00"
        fields_a[f"status_{cid}"] = "completed"
        if cid != short:                      # everyone but `short` gets both
            fields_a[f"t2_run1_{cid}"] = f"{20 + i}.00"
    _enter_heat(client, sql, CROSS_HEAT_A, fields_a)

    assert sql("""SELECT status FROM event_results
                  WHERE event_id = :e AND competitor_id = :c""",
               e=CROSS_HEAT_EVENT, c=short)[0][0] == "partial", (
        "heat 464 did not leave a partial row, so this test proves nothing")
    assert not sql("SELECT is_finalized FROM events WHERE id = :e",
                   e=CROSS_HEAT_EVENT)[0][0], "finalized on the first of two heats"

    fields_b = {}
    for i, cid in enumerate(b):
        fields_b[f"t1_run1_{cid}"] = f"{30 + i}.00"
        fields_b[f"t2_run1_{cid}"] = f"{30 + i}.00"
        fields_b[f"status_{cid}"] = "completed"
    _enter_heat(client, sql, CROSS_HEAT_B, fields_b)

    rows = sql("""
        SELECT competitor_id, status, final_position, points_awarded
        FROM event_results WHERE event_id = :e ORDER BY competitor_id
    """, e=CROSS_HEAT_EVENT)
    assert not sql("SELECT is_finalized FROM events WHERE id = :e",
                   e=CROSS_HEAT_EVENT)[0][0], (
        f"saving the LAST heat finalized the event while competitor {short} "
        f"in the FIRST heat still had one timer. rows={rows}. The partial "
        f"check is looking at the heat being saved instead of the event.")

    # And it lifts the same way it does within one heat.
    fields_a[f"t2_run1_{short}"] = fields_a[f"t1_run1_{short}"]
    _enter_heat(client, sql, CROSS_HEAT_A, fields_a)
    assert sql("SELECT is_finalized FROM events WHERE id = :e",
               e=CROSS_HEAT_EVENT)[0][0], (
        "both heats are complete with no partial rows left and the event still "
        "will not finalize")
    assert sql("""SELECT final_position FROM event_results
                  WHERE event_id = :e AND competitor_id = :c""",
               e=CROSS_HEAT_EVENT, c=short)[0][0] is not None


# ---------------------------------------------------------------------------
# 15. Async flight-build completion page 500s on every refresh
#     routes/reporting.py:373 export_results_job_status
# ---------------------------------------------------------------------------

@pytest.mark.sev2
def test_async_flight_build_status_page_returns(app, client, sql):
    """The build POST redirects straight to the status URL, so this is forced.

    export_results_job_status does int(job.get('result') or 0) while
    _build_flights_async returns a dict, so the completion handler raises and
    the operator sees a 500 that reads like the build crashed. The flights
    actually committed.
    """
    # TESTING=True re-raises inside the test client. Production returns a 500
    # page, so turn propagation off to observe what the operator observes.
    app.config["PROPAGATE_EXCEPTIONS"] = False

    r = client.post(f"/scheduling/{TID}/flights/build",
                    data={"run_async": "1", "flight_sizing_mode": "count",
                          "num_flights": "4"},
                    follow_redirects=False)
    assert r.status_code == 302, (r.status_code, r.data[:400])

    location = r.headers.get("Location")
    assert location and "job" in location, (
        f"the async build did not redirect to a job status URL: {location}")

    # Poll the status page the way the operator's browser does.
    import time
    s = None
    for _ in range(60):
        s = client.get(location)
        if s.status_code >= 500:
            break
        body = s.data.decode("utf-8", "replace").lower()
        if "running" not in body and "pending" not in body:
            break
        time.sleep(0.5)

    assert s is not None and s.status_code < 500, (
        f"the flight-build status page returned {s.status_code}. "
        f"routes/reporting.py:445 does int(job['result']) on a dict returned "
        f"by _build_flights_async. The flights did build; this is a false "
        f"crash signal during show prep."
    )


def _poll_job(client, location, tries=60):
    """Refresh a job status URL the way the operator's browser does."""
    import time
    s = None
    for _ in range(tries):
        s = client.get(location)
        if s.status_code >= 500:
            return s
        body = s.data.decode("utf-8", "replace").lower()
        if "running" not in body and "pending" not in body:
            return s
        time.sleep(0.5)
    return s


@pytest.mark.sev2
def test_async_flight_build_reports_the_count_it_actually_built(app, client, sql, flashes):
    """Positive control. Not 500ing is a low bar and the cheap fixes clear it.

    `int(job.get('result') or 0)` sits inside a handler that could be made to
    stop raising in two useless ways: wrap it in try/except and flash nothing,
    or leave the `or 0` reachable so the operator is told "Built 0 flight(s)"
    after a build that produced seven. Both return 200 and both pass the test
    above. Neither tells the truth, and this is the screen the operator reads
    to decide whether the schedule is ready to print.

    So: demand the number on the screen match the number of Flight rows the job
    committed. Fails pre-fix at the 500, same as the test above, but it fails
    for a second reason afterwards if the count is faked.
    """
    app.config["PROPAGATE_EXCEPTIONS"] = False

    r = client.post(f"/scheduling/{TID}/flights/build",
                    data={"run_async": "1", "flight_sizing_mode": "count",
                          "num_flights": "4"},
                    follow_redirects=False)
    assert r.status_code == 302, (r.status_code, r.data[:400])
    s = _poll_job(client, r.headers["Location"])
    assert s.status_code < 500, s.status_code

    in_db = sql("SELECT count(*) FROM flights WHERE tournament_id = :t", t=TID)[0][0]
    assert in_db > 0, (
        "the async job committed no flights at all, so this test cannot tell a "
        "truthful report from a fake one. The build itself is broken.")

    told = [msg for cat, msg in flashes() if "flight" in msg.lower()]
    assert told, (
        f"the build finished and the operator was told nothing about it. "
        f"flashes={flashes()}")
    numbers = [int(n) for msg in told for n in re.findall(r"\b(\d+)\b", msg)]
    assert in_db in numbers, (
        f"the status page reported {numbers} flight(s) but {in_db} rows are in "
        f"the flights table. told={told}. Reporting a count the database does "
        f"not agree with is worse than the 500, because the operator believes "
        f"it.")


@pytest.mark.sev2
def test_the_synchronous_flight_build_still_reports_its_count(client, sql, flashes):
    """Positive control. Both build paths go through the same route function.

    The async branch is an early return inside `flights_build`. A fix that
    reaches back into that function instead of into the status handler can
    break the ordinary, non-async build that every operator who does not tick
    the box uses. That path works today, so this test passes today, and it has
    to keep passing.
    """
    r = client.post(f"/scheduling/{TID}/flights/build",
                    data={"flight_sizing_mode": "count", "num_flights": "4"},
                    follow_redirects=False)
    assert r.status_code == 302, (r.status_code, r.data[:400])
    assert "job" not in (r.headers.get("Location") or ""), (
        "a build with no run_async flag was handed off to the background job "
        f"queue anyway: {r.headers.get('Location')}")

    in_db = sql("SELECT count(*) FROM flights WHERE tournament_id = :t", t=TID)[0][0]
    assert in_db > 0, "the synchronous build committed no flights"
    told = [msg for cat, msg in flashes() if "flight" in msg.lower()]
    numbers = [int(n) for msg in told for n in re.findall(r"\b(\d+)\b", msg)]
    assert in_db in numbers, (
        f"the synchronous build reported {numbers} but {in_db} flights exist. "
        f"told={told}")


@pytest.mark.sev2
def test_an_ordinary_export_job_still_downloads_from_the_same_status_route(
        app, client, sql):
    """Positive control. One route serves every background job kind.

    `export_results_job_status` branches on metadata['kind']. The flight branch
    is the broken one, but the file-download branch below it is what an
    operator gets when they export results for the awards table, and it works
    today. A fix that restructures the handler and drops the send_file path, or
    that runs the flight branch for every kind, breaks that with nothing else
    in the suite watching.
    """
    app.config["PROPAGATE_EXCEPTIONS"] = False

    r = client.post(f"/reporting/{TID}/export-results/async",
                    follow_redirects=False)
    assert r.status_code == 302, (r.status_code, r.data[:400])
    s = _poll_job(client, r.headers["Location"])

    assert s.status_code < 500, (
        f"the ordinary results export status page returned {s.status_code}")
    assert s.status_code == 200, (
        f"the completed export redirected ({s.status_code} -> "
        f"{s.headers.get('Location')}) instead of delivering the file")
    assert len(s.data) > 0 and "attachment" in (
        s.headers.get("Content-Disposition") or ""), (
        f"the export job completed but no file came back. headers={dict(s.headers)}")


# ---------------------------------------------------------------------------
# 16. Manual relay team builder renders nameless pool cards
#     templates/proam_relay/manual_teams.html:84,107
# ---------------------------------------------------------------------------

@pytest.mark.sev2
def test_manual_relay_builder_renders_competitor_names(client, sql):
    """An operator dragging blank cards is assigning people at random.

    The template renders comp.display_name; services/proam_relay.py supplies
    plain dicts keyed 'name'. Jinja's default Undefined renders as an empty
    string, so every pool card is blank. Drag-drop still works on data-id,
    which is why nothing errors.
    """
    r = client.get(f"/tournament/{TID}/proam-relay/manual-teams")
    assert r.status_code == 200, (r.status_code, r.data[:400])
    body = r.data.decode("utf-8", "replace")

    cards = re.findall(
        r'<div class="comp-card"[^>]*data-id="(\d+)"[^>]*'
        r'data-division="(\w+)">\s*<span>(.*?)</span>',
        body, re.S)
    # Guard against a vacuous pass: if the page rendered no cards at all there
    # is nothing to check and the assertion below would be meaningless.
    assert cards, (
        "the manual team builder rendered no competitor cards at all, so this "
        "test cannot tell blank cards from an empty pool. Check that the "
        "lottery opt-in pool in this data is non-empty.")

    blank = [(div, cid) for cid, div, nm in cards if not nm.strip()]
    assert not blank, (
        f"{len(blank)} of {len(cards)} competitor cards rendered with an empty "
        f"name. First few: {blank[:5]}. The template prints comp.display_name "
        f"but services/proam_relay.py supplies plain dicts keyed 'name', and "
        f"Jinja's default Undefined renders as an empty string. Drag and drop "
        f"still works because it binds data-id, so the operator is assigning "
        f"nameless cards to relay teams."
    )


_CARD_RE = re.compile(
    r'<div class="comp-card"[^>]*data-id="(\d+)"[^>]*data-division="(\w+)">'
    r'\s*<span>(.*?)</span>', re.S)


def _relay_regions(body):
    """Split the manual builder page into (pro pool, college pool, team slots).

    Pool cards and assigned-team cards are the same markup with the same
    attributes, so they cannot be told apart by a regex over the whole page.
    They are told apart by position: the template lays out proPool, then
    collegePool, then the team-list slots. Each slice is asserted non-empty by
    the callers, so a template restructure fails loudly instead of quietly
    handing every test an empty list to not-find-a-problem in.
    """
    i_pro = body.index('id="proPool"')
    i_col = body.index('id="collegePool"')
    i_slot = body.index('class="team-list"')
    assert i_pro < i_col < i_slot, (i_pro, i_col, i_slot)
    return body[i_pro:i_col], body[i_col:i_slot], body[i_slot:]


def _db_names(sql):
    pro = {r[0]: r[1] for r in sql(
        "SELECT id, name FROM pro_competitors WHERE tournament_id = :t", t=TID)}
    col = {r[0]: r[1] for r in sql(
        "SELECT id, name FROM college_competitors WHERE tournament_id = :t", t=TID)}
    return {"pro": pro, "college": col}


@pytest.mark.sev2
def test_relay_pool_cards_name_the_person_the_card_actually_is(client, sql):
    """Positive control. "Not blank" is not the same as "correct".

    The test above only asks that the span is non-empty. Several fixes satisfy
    it and still hand the operator garbage: print comp.id, print a
    'Competitor 41' placeholder, or reach for the wrong key and print the
    gender letter or the team code on every card. Worse, a fix applied to the
    dicts rather than the template could print the RIGHT name against the
    WRONG id, and drag-and-drop binds data-id, so the operator would build a
    team out of people they never picked and the page would look perfect.

    Pro and college ids collide in this data (21 of them), so the check joins
    on division as well as id. Fails pre-fix with every pool card blank.
    """
    r = client.get(f"/tournament/{TID}/proam-relay/manual-teams")
    assert r.status_code == 200, (r.status_code, r.data[:400])
    body = r.data.decode("utf-8", "replace")
    pro_region, col_region, _ = _relay_regions(body)
    names = _db_names(sql)

    wrong = []
    seen = 0
    for region, division in ((pro_region, "pro"), (col_region, "college")):
        cards = _CARD_RE.findall(region)
        assert cards, (
            f"the {division} pool rendered no cards at all, so this test is "
            f"checking nothing. Either the opt-in pool is empty in this data "
            f"or the page structure moved.")
        for cid, div, shown in cards:
            seen += 1
            assert div == division, (
                f"a card in the {division} pool is tagged data-division={div!r}; "
                f"drag-and-drop routes on that attribute")
            expected = names[division].get(int(cid))
            assert expected is not None, (
                f"pool card data-id={cid} in the {division} pool matches no "
                f"{division} competitor in this tournament")
            shown_text = re.sub(r"<[^>]+>", "", shown).strip()
            if expected not in shown_text:
                wrong.append((division, cid, expected, shown_text))

    assert seen > 0
    assert not wrong, (
        f"{len(wrong)} of {seen} pool cards do not name the competitor their "
        f"data-id points at. (division, id, expected, shown): {wrong[:5]}. "
        f"The operator drags by name and the app assigns by id, so a card that "
        f"disagrees with itself puts the wrong person on the team.")


@pytest.mark.sev2
def test_already_assigned_relay_cards_keep_their_names(client, sql):
    """Positive control. The team slots work today and must keep working.

    Slot cards render m.name and they render correctly right now, so any fix
    that reaches the whole page rather than the two broken pool lines shows up
    here as blank or wrong slot cards.

    Do not read this as covering a service-layer rename. It does not, and I
    mutated the service to prove it: slot members come out of the persisted
    events.event_state JSON, not out of a live service call, so renaming the
    service key leaves every already-drawn team looking perfect. That mutation
    is caught one test down, in
    test_the_pool_dicts_are_keyed_the_way_persisted_team_members_are.
    """
    r = client.get(f"/tournament/{TID}/proam-relay/manual-teams")
    assert r.status_code == 200, (r.status_code, r.data[:400])
    body = r.data.decode("utf-8", "replace")
    _, _, slots = _relay_regions(body)

    cards = _CARD_RE.findall(slots)
    assert cards, (
        "no relay team has any assigned members in this data, so this control "
        "is vacuous. The mirror carries a drawn relay (events.event_state on "
        "'Pro-Am Relay' is populated); if that changed, this test needs a new "
        "fixture rather than a pass.")

    names = _db_names(sql)
    bad = []
    for cid, div, shown in cards:
        shown_text = re.sub(r"<[^>]+>", "", shown).strip()
        expected = names.get(div, {}).get(int(cid))
        if not shown_text or (expected and expected not in shown_text):
            bad.append((div, cid, expected, shown_text))
    assert not bad, (
        f"{len(bad)} of {len(cards)} assigned relay cards lost or changed their "
        f"name: {bad[:5]}. These render correctly on v2026.final, so this is a "
        f"regression introduced by the pool-card fix.")


@pytest.mark.sev2
def test_the_pool_dicts_are_keyed_the_way_persisted_team_members_are(app, sql):
    """Positive control, and the whole argument for fixing the template.

    The obvious alternative fix is to rename the service key to display_name so
    the original template line works. Every other test in this file passes under
    that change. I ran it as a mutation to be sure: four green.

    It is still wrong. ProAmRelay.run_lottery appends these exact dict objects
    into team['pro_members'] / team['college_members'], and the whole structure
    is json.dumps'd into events.event_state (services/proam_relay.py:88, :452).
    The key is not a private detail of the service, it is the on-disk schema of
    every relay ever drawn. Rename it and the already-drawn teams keep their
    names, because they were written before the rename, so the page looks fine
    and stays fine until someone redraws on show day. Then every slot card goes
    blank, and anything reading member['name'] off event_state raises KeyError.

    So: the key the service hands out has to stay the key the persisted records
    already use. That is checkable without drawing anything, and this checks it.
    """
    import json

    from models import Tournament
    from services.proam_relay import ProAmRelay

    with app.app_context():
        tournament = Tournament.query.get(TID)
        assert tournament is not None, f"tournament {TID} is missing from the mirror"
        service = ProAmRelay(tournament)
        pro_pool = service.get_eligible_pro_competitors()
        college_pool = service.get_eligible_college_competitors()

    assert pro_pool and college_pool, (
        f"the eligible pools came back empty (pro={len(pro_pool)}, "
        f"college={len(college_pool)}), so this test is checking nothing")

    rows = sql("SELECT event_state FROM events "
               "WHERE tournament_id = :t AND name = 'Pro-Am Relay'", t=TID)
    assert rows and rows[0][0], (
        "no Pro-Am Relay event_state in the mirror, so there is no persisted "
        "record to compare the live dict shape against. This control needs a "
        "drawn relay; it must not be allowed to pass by finding nothing.")
    state = json.loads(rows[0][0])
    teams = state.get("teams") or []
    assert teams, "the persisted relay has no teams drawn"

    persisted = []
    for team in teams:
        persisted.extend(team.get("pro_members") or [])
        persisted.extend(team.get("college_members") or [])
    assert persisted, "the persisted relay teams have no members"

    live_keys = {k for d in (pro_pool + college_pool) for k in d}
    stored_keys = {k for d in persisted for k in d}

    dropped = stored_keys - live_keys
    assert not dropped, (
        f"the eligible-pool dicts no longer carry {sorted(dropped)}, but every "
        f"member dict already persisted in events.event_state does. run_lottery "
        f"copies pool dicts straight into that JSON, so the next draw would "
        f"write records in a shape the old ones are not in, and the slot cards "
        f"plus anything else reading those keys break on show day. "
        f"live={sorted(live_keys)} stored={sorted(stored_keys)}")

    assert "name" in live_keys, (
        f"the pool dicts have no 'name' key at all: {sorted(live_keys)}")


@pytest.mark.sev2
def test_the_relay_dashboard_still_renders_off_the_same_pools(client):
    """Positive control. Two pages consume these two service methods.

    routes/proam_relay.py:43-44 hands the identical eligible_pro /
    eligible_college lists to the dashboard, which is the page the operator
    actually lands on. Any fix made inside services/proam_relay.py rather than
    in the one broken template lands here too, and nothing else in the suite
    opens this page.
    """
    r = client.get(f"/tournament/{TID}/proam-relay/")
    assert r.status_code == 200, (r.status_code, r.data[:400])
    assert b"comp-card" in r.data or b"Pro-Am Relay" in r.data, (
        "the relay dashboard returned 200 but rendered neither relay markup "
        "nor its own title, which usually means an exception was swallowed "
        "into an empty template block")


# ---------------------------------------------------------------------------
# c01. Springboard stand assignment double-books a stand on 5-stand heats
#      services/heat_generator.py hardcodes the right-handed stands as [1, 2, 3]
# ---------------------------------------------------------------------------

PRO_1BOARD_EVENT = 31      # springboard, max_stands = 5, 10 entrants


@pytest.mark.sev2
def test_springboard_lh_heat_gives_every_cutter_a_distinct_stand(app, sql):
    """One left-handed cutter must not cost the heat a stand.

    Event 31 is the only springboard event in this data configured for five
    stands. The left-hand branch of the stand assignment pins the LH cutter to
    stand 4 and then walks the remaining cutters against a hardcoded [1, 2, 3],
    with an else-arm that falls back to the loop index plus one. On a heat of
    five that puts the fourth right-handed cutter on stand 4 as well, so two
    people are sent to the same springboard and stand 5 is never called.

    This is latent in the data as shipped: no pro currently carries the
    left-handed flag. It is armed by one checkbox on the pro detail form. This
    test sets the column directly rather than posting the form, because the
    defect under test is in heat generation and routing it through registration
    would only add a second failure mode to the diagnosis. The flag is a real
    production column with a real form control behind it, not a fixture
    invention.
    """
    import sys

    import rig as _rig
    if _rig.APP_ROOT not in sys.path:
        sys.path.insert(0, _rig.APP_ROOT)

    from database import db
    from models.event import Event
    from services.heat_generator import generate_event_heats

    entrants = [row[0] for row in sql("""
        SELECT id FROM pro_competitors
        WHERE entry_fees::jsonb ? :e ORDER BY id
    """, e=str(PRO_1BOARD_EVENT))]
    assert len(entrants) >= 5, (
        f"event {PRO_1BOARD_EVENT} has {len(entrants)} entrants in this data; "
        f"this test needs a heat of five to exercise the fifth stand")

    event = db.session.get(Event, PRO_1BOARD_EVENT)
    assert event.stand_type == 'springboard'
    assert event.max_stands == 5, (
        f"event {PRO_1BOARD_EVENT} is configured for {event.max_stands} "
        f"stands; the defect only shows on more than four")

    # Arm the flag on the first entrant and generate.
    db.session.execute(db.text(
        "UPDATE pro_competitors SET is_left_handed_springboard = true "
        "WHERE id = :c"), {"c": entrants[0]})
    db.session.commit()

    generate_event_heats(event)
    db.session.commit()

    rows = sql("""
        SELECT id, heat_number, competitors, stand_assignments
        FROM heats WHERE event_id = :e AND run_number = 1
        ORDER BY heat_number
    """, e=PRO_1BOARD_EVENT)
    assert rows, "heat generation produced no heats, so this test is vacuous"

    collisions = []
    for heat_id, heat_number, competitors, assignments in rows:
        stands = _loads(assignments) or {}
        by_stand = {}
        for comp_id, stand in stands.items():
            by_stand.setdefault(stand, []).append(comp_id)
        doubled = {s: ids for s, ids in by_stand.items() if len(ids) > 1}
        if doubled:
            collisions.append((heat_id, heat_number, doubled, stands))

    assert not collisions, (
        f"{len(collisions)} springboard heat(s) sent two cutters to the same "
        f"stand. First: heat id {collisions[0][0]} (heat {collisions[0][1]}) "
        f"doubled {collisions[0][2]}, full assignment {collisions[0][3]}. "
        f"The LH branch in services/heat_generator.py pins the LH cutter to "
        f"stand 4 and then indexes the rest against a hardcoded [1, 2, 3], so "
        f"the fourth right-handed cutter collides on stand 4 and stand 5 is "
        f"never emitted.")

    # The collision is only half of it: a five-stand heat that never calls
    # stand 5 is running four people through a block sized for five.
    full_heats = [
        (hid, hn, _loads(a))
        for hid, hn, c, a in rows
        if len(_loads(c) or []) >= 5
    ]
    assert full_heats, "no heat of five was generated, so the stand-5 check is vacuous"
    for hid, hn, stands in full_heats:
        assert len(set(stands.values())) >= 5, (
            f"heat id {hid} (heat {hn}) holds {len(stands)} cutters but uses "
            f"only stands {sorted(set(stands.values()))}")


# ---------------------------------------------------------------------------
# c02. The config-declared three-throw flag never reaches the database, and a
#      CSV that carries only run columns is silently marked scratched.
#      routes/scheduling/events.py::_upsert_event  (flag never written)
#      services/scoring_engine.py::import_results_from_csv  (status decided
#      from the raw result cell before the run columns are summed)
# ---------------------------------------------------------------------------

AXE_M_EVENT = 1            # college Axe Throw, men. config declares triple runs.
CABER_M_EVENT = 4          # college Caber Toss, men. dual runs, distance.
CABER_ENTRANTS = ["Trevor Norris", "Trustin Norick", "Noah Chamberlain"]


def _checked_enable_fields(html):
    """Every enable_* checkbox the rendered setup form has ticked.

    The operator's real POST carries one entry per ticked box. Posting an empty
    form instead would ask the route to delete every closed event, which is a
    different operation with a different blast radius. Scraping what the page
    actually renders keeps this test on the path a human takes.
    """
    fields = []
    for tag in re.findall(r'<input[^>]*>', html):
        if 'type="checkbox"' not in tag and "type='checkbox'" not in tag:
            continue
        name = re.search(r'name="([^"]+)"', tag)
        if not name or not name.group(1).startswith('enable_'):
            continue
        if 'checked' in tag:
            fields.append(name.group(1))
    return fields


@pytest.mark.sev2
def test_events_setup_save_writes_the_config_declared_triple_run_flag(client, sql):
    """Saving the event setup must restore every run flag config declares.

    config.py declares 'requires_triple_runs': True on Axe Throw and Partnered
    Axe Throw. _upsert_event copies requires_dual_runs and stops there, so the
    triple flag has never once been written by the application. All 44 real
    2026 events carry requires_triple_runs=false, and routes/main.py copies the
    value verbatim when a tournament is cloned, so a 2027 clone inherits the
    wrong value too.

    Caber Toss is the control. Both flags are zeroed here, the same POST is
    sent, and the dual flag comes back while the triple flag does not. That
    rules out 'the save did not run' as an explanation for the failure.

    Zeroing the flags is a write to the per-test clone, never to the template.
    """
    from database import db
    from models.event import Event

    db.session.execute(db.text(
        "UPDATE events SET requires_dual_runs = false, requires_triple_runs = false "
        "WHERE id IN (:a, :c)"), {"a": AXE_M_EVENT, "c": CABER_M_EVENT})
    db.session.commit()

    page = client.get(f"/scheduling/{TID}/events/setup")
    assert page.status_code == 200, page.status_code
    html = page.get_data(as_text=True)

    form = {"action_scope": "college"}
    for field in _checked_enable_fields(html):
        form[field] = "on"

    saved = client.post(f"/scheduling/{TID}/events/setup", data=form)
    assert saved.status_code == 302, saved.data[:400]

    db.session.expire_all()
    axe = db.session.get(Event, AXE_M_EVENT)
    caber = db.session.get(Event, CABER_M_EVENT)

    # Control first. If this fails the POST never reached the upsert and the
    # real assertion below would be meaningless.
    assert caber.requires_dual_runs is True, (
        "the control failed: the setup save did not restore requires_dual_runs "
        "on Caber Toss either, so this run proves nothing about the triple flag")

    assert axe.requires_triple_runs is True, (
        "the setup save left requires_triple_runs=False on Axe Throw while "
        "restoring requires_dual_runs on Caber Toss from the same POST. "
        "_upsert_event in routes/scheduling/events.py writes requires_dual_runs "
        "and has no requires_triple_runs twin, so the flag config declares can "
        "never reach the database.")


@pytest.mark.sev2
def test_csv_import_with_only_run_columns_is_not_marked_scratched(client, sql):
    """Run columns that produce a result must not land as a scratch.

    The import page tells the operator the result column is optional and that
    run1 and run2 are accepted. Follow that instruction and the importer reads
    the blank result cell, decides the row is a scratch, and only afterwards
    sums or picks the runs into result_value. The row ends up with a real
    number and status='scratched', under a green 'Imported 3 result(s),
    skipped 0.' banner. Nothing on screen says a competitor was dropped.

    Caber Toss is used rather than Axe Throw because it already carries
    requires_dual_runs=true in production, so this test needs no flag setup and
    isolates the importer defect from the missing-flag defect above.

    A genuinely empty row, no result and no runs, must still scratch. That is
    the second assertion.
    """
    from database import db
    from models.event import Event

    event = db.session.get(Event, CABER_M_EVENT)
    assert event.requires_dual_runs is True
    assert event.scoring_order == 'highest_wins'

    csv_text = "competitor_name,result,run1,run2\n"
    throws = {"Trevor Norris": (28.0, 31.5),
              "Trustin Norick": (30.0, 29.0),
              "Noah Chamberlain": (26.5, 27.0)}
    for name in CABER_ENTRANTS:
        r1, r2 = throws[name]
        csv_text += f"{name},,{r1},{r2}\n"
    # A row with nothing in it at all. This one is a real scratch.
    csv_text += "COOPER DRISKELL,,,\n"

    posted = client.post(f"/scoring/{TID}/event/{CABER_M_EVENT}/import-results",
                         data={"csv_text": csv_text})
    assert posted.status_code == 302, posted.data[:400]

    rows = dict((name, (val, status)) for name, val, status in sql("""
        SELECT competitor_name, result_value, status
        FROM event_results WHERE event_id = :e
    """, e=CABER_M_EVENT))

    for name in CABER_ENTRANTS:
        match = [k for k in rows if k.lower().startswith(name.lower())]
        assert match, f"{name} has no result row after import; rows: {sorted(rows)}"
        value, status = rows[match[0]]
        expected = max(throws[name])
        assert value is not None and float(value) == expected, (
            f"{name} imported with result_value={value}, expected {expected} "
            f"(highest of the two runs)")
        assert status == 'completed', (
            f"{name} imported with three real runs and landed status='{status}'. "
            f"import_results_from_csv decides the status from the raw result "
            f"cell before calculate_best_run fills result_value in, so a row "
            f"that follows the on-screen instruction is scratched silently "
            f"under a success flash.")

    empty = [k for k in rows if k.lower().startswith('cooper')]
    assert empty, "the control row is missing, so the scratch check is vacuous"
    assert rows[empty[0]][1] == 'scratched', (
        "a row with no result and no runs must still scratch; got "
        f"{rows[empty[0]]}")


# ---------------------------------------------------------------------------
# c03. The Scratch button dead-ends on a raw JSON document
#      routes/scoring.py::scratch_preview returns jsonify() to a browser, and
#      the confirmation template that would let the judge finish the scratch
#      is an orphan that raises on render.
# ---------------------------------------------------------------------------

SCRATCH_PRO = 44           # Seth Bergman, active, 8 event_results on TID

# What Chrome, Firefox and Safari all send on a top-level navigation. The
# route has to be able to tell that apart from an XHR or a script.
BROWSER_ACCEPT = ('text/html,application/xhtml+xml,application/xml;q=0.9,'
                  'image/avif,image/webp,*/*;q=0.8')
BROWSER_HEADERS = {'Accept': BROWSER_ACCEPT}


def _scratch_form_action(html, competitor_id):
    """The action URL of the Scratch form for one competitor, or None.

    Scraped rather than hardcoded so the test breaks if the button is moved,
    renamed or removed instead of quietly testing a URL nobody can reach.
    """
    for form in re.findall(r'<form[^>]*>', html):
        action = re.search(r'action="([^"]*)"', form)
        if not action:
            continue
        url = action.group(1)
        # Pro is /registration/<tid>/pro/<cid>/scratch and college is
        # /registration/<tid>/college/competitor/<cid>/scratch, so match on the
        # tail the two shapes share rather than on either prefix.
        if url.endswith(f'/{competitor_id}/scratch'):
            return url
    return None


def _hidden_and_checkbox_fields(html):
    """Every hidden input and ticked checkbox inside the scratch-confirm form.

    This is the judge pressing Confirm with the page exactly as rendered: all
    effects left checked. Parsing what the server actually sent, rather than
    reconstructing the field names here, is what makes this an end-to-end
    test of the page instead of a restatement of the route.
    """
    start = html.find('scratch-confirm')
    assert start != -1, 'no scratch-confirm form in the rendered page'
    form_start = html.rfind('<form', 0, start)
    form_end = html.find('</form>', start)
    block = html[form_start:form_end]

    fields = {}
    for tag in re.findall(r'<input[^>]*>', block):
        name = re.search(r'name="([^"]*)"', tag)
        value = re.search(r'value="([^"]*)"', tag)
        if not name:
            continue
        if 'type="checkbox"' in tag and 'checked' not in tag:
            continue
        fields[name.group(1)] = value.group(1) if value else 'on'
    return fields


@pytest.mark.sev2
def test_scratch_button_lands_the_judge_on_a_usable_confirmation_page(client, sql):
    """The Scratch button is a plain POST form. Wherever it lands must be HTML.

    templates/pro/dashboard.html, pro/registration.html, pro/competitor_detail.html
    and college/team_detail.html all render Scratch as a native form post with
    a data-confirm dialog and nothing else. Nothing in static/ fetches the
    preview endpoint, so whatever that POST redirects to is what the judge
    sees on race day.

    The first assertion scrapes the button off the real registration page. If
    the button is not there, the rest of this test would be checking a URL no
    operator can reach, so it fails loudly instead.
    """
    # /registration/<tid>/pro is the legacy URL and 302s to the pro dashboard,
    # which is where the live Scratch buttons render. Following it keeps this
    # test on the page the operator actually works from.
    page = client.get(f"/registration/{TID}/pro", headers=BROWSER_HEADERS,
                      follow_redirects=True)
    assert page.status_code == 200, page.status_code
    action = _scratch_form_action(page.get_data(as_text=True), SCRATCH_PRO)
    assert action, (
        f"no Scratch form for pro competitor {SCRATCH_PRO} on the pro "
        "dashboard; the entry point this test covers has moved")

    landed = client.post(action, headers=BROWSER_HEADERS, follow_redirects=True)

    assert landed.status_code == 200, (
        f"the Scratch button ended on {landed.status_code}: "
        f"{landed.get_data(as_text=True)[:300]}")

    ctype = landed.headers.get('Content-Type', '')
    body = landed.get_data(as_text=True)
    assert ctype.startswith('text/html'), (
        "a browser pressing Scratch was served "
        f"{ctype!r}; body starts {body[:200]!r}")
    assert 'scratch-confirm' in body, (
        "the page the judge landed on offers no way to confirm the scratch")


@pytest.mark.sev2
def test_confirming_from_the_rendered_page_actually_scratches_the_competitor(client, sql):
    """Reaching the page is not enough. Pressing Confirm on it has to work.

    Everything posted back here is scraped out of the server's own HTML, so a
    page that renders but ships broken field names fails this and passes the
    test above.

    The competitor status is the assertion that matters. scheduling.scratch_competitor
    is the only other scratch path in the UI and it sets a single EventResult
    while leaving pro_competitors.status = 'active', which is what dashboard
    integrity checks, gear-sharing cleanup and partner-orphan detection all
    read.
    """
    before = sql("SELECT status FROM pro_competitors WHERE id = :c", c=SCRATCH_PRO)
    assert before and before[0][0] == 'active', (
        f"competitor {SCRATCH_PRO} is not active in the rig; got {before}")

    page = client.get(f"/registration/{TID}/pro", headers=BROWSER_HEADERS,
                      follow_redirects=True)
    action = _scratch_form_action(page.get_data(as_text=True), SCRATCH_PRO)
    assert action

    landed = client.post(action, headers=BROWSER_HEADERS, follow_redirects=True)
    assert landed.status_code == 200, landed.get_data(as_text=True)[:300]
    fields = _hidden_and_checkbox_fields(landed.get_data(as_text=True))

    assert int(fields.get('effect_count', 0)) > 0, (
        "the confirmation page listed no effects for a competitor holding 8 "
        f"event results; fields were {sorted(fields)}")

    confirmed = client.post(
        f"/scoring/{TID}/competitor/{SCRATCH_PRO}/scratch-confirm", data=fields)
    assert confirmed.status_code in (200, 302), confirmed.get_data(as_text=True)[:300]

    after = sql("SELECT status FROM pro_competitors WHERE id = :c", c=SCRATCH_PRO)
    assert after[0][0] == 'scratched', (
        "the competitor is still "
        f"{after[0][0]!r} after the judge completed the scratch flow")

    live = sql("""
        SELECT count(*) FROM event_results
         WHERE competitor_id = :c AND competitor_type = 'pro'
           AND status NOT IN ('scratched', 'dns')
    """, c=SCRATCH_PRO)[0][0]
    assert live == 0, f"{live} event result(s) still live for a scratched competitor"


@pytest.mark.sev2
def test_scratch_preview_still_answers_json_when_json_is_asked_for(client, sql):
    """Control for the two tests above.

    The preview endpoint has a documented JSON body and tests/ depends on it.
    Serving HTML to browsers must not take that away, so both explicit forms
    of asking, the Accept header and ?format=json, are checked here. If this
    fails the fix traded one broken caller for another.
    """
    base = f"/scoring/{TID}/competitor/{SCRATCH_PRO}/scratch-preview?competitor_type=pro"

    by_header = client.get(base, headers={'Accept': 'application/json'})
    assert by_header.status_code == 200, by_header.get_data(as_text=True)[:300]
    payload = by_header.get_json()
    assert payload is not None, by_header.get_data(as_text=True)[:300]
    assert isinstance(payload.get('effects'), list) and payload['effects'], (
        f"no effects in the JSON body for a competitor with 8 results: {payload}")

    by_param = client.get(base + "&format=json", headers=BROWSER_HEADERS)
    assert by_param.status_code == 200
    assert by_param.get_json() is not None, (
        "?format=json was ignored in favour of the browser Accept header")


SCRATCH_COLLEGE = 100029   # Greer Swoboda, team 5, active, 6 event_results.
                           # Inside the 29-49 id range that collides with the
                           # pro roster, so this doubles as a check that the
                           # rendered page names the right human.
SCRATCH_COLLEGE_TEAM = 5


@pytest.mark.sev2
def test_college_scratch_button_renders_the_page_for_the_right_human(client, sql):
    """The college half of the same flow, on a colliding id.

    College and pro ids come from separate sequences and overlap from 29 to
    49. The route resolves on competitor_type, which the redirect carries, so
    the page must show the college competitor. A page that renders correctly
    but names the pro twin would let a judge scratch the wrong person while
    reading a confirmation that looks right.
    """
    college_name = sql("SELECT name FROM college_competitors WHERE id = :c",
                       c=SCRATCH_COLLEGE)[0][0]
    # Post-c39 the twin lives at SCRATCH_COLLEGE - 100000: ids no longer
    # collide, but type resolution must still pick the right table.
    pro_twin = sql("SELECT name FROM pro_competitors WHERE id = :c",
                   c=SCRATCH_COLLEGE - 100000)
    assert pro_twin and pro_twin[0][0] != college_name, (
        f"id {SCRATCH_COLLEGE} no longer collides across the two rosters, so "
        "this test proves nothing")

    page = client.get(f"/registration/{TID}/college/team/{SCRATCH_COLLEGE_TEAM}",
                      headers=BROWSER_HEADERS, follow_redirects=True)
    assert page.status_code == 200, page.status_code
    action = _scratch_form_action(page.get_data(as_text=True), SCRATCH_COLLEGE)
    assert action, (
        f"no Scratch form for college competitor {SCRATCH_COLLEGE} on team "
        f"{SCRATCH_COLLEGE_TEAM}'s page")

    landed = client.post(action, headers=BROWSER_HEADERS, follow_redirects=True)
    assert landed.status_code == 200, landed.get_data(as_text=True)[:300]
    body = landed.get_data(as_text=True)
    assert landed.headers.get('Content-Type', '').startswith('text/html'), (
        f"college Scratch served {landed.headers.get('Content-Type')!r}")
    assert college_name in body, (
        f"the confirmation page does not name {college_name}")
    assert pro_twin[0][0] not in body, (
        f"the confirmation page names the pro twin {pro_twin[0][0]!r} for a "
        "college scratch")
    fields = _hidden_and_checkbox_fields(body)
    assert fields.get('competitor_type') == 'college', (
        f"the confirm form would post competitor_type={fields.get('competitor_type')!r}")
    assert int(fields.get('effect_count', 0)) > 0, sorted(fields)


# ---------------------------------------------------------------------------
# c04. Undo restores the scratched competitor and leaves the partner destroyed
#      services/scratch_cascade.py::execute_cascade nulls the partner's
#      EventResult.partner_name and strips the partner competitor's partners
#      JSON, and snapshots neither, so reverse_cascade cannot put them back.
#      The same branch filters that JSON by partner NAME across every event
#      key, so one ticked effect also wipes events the judge left unticked.
# ---------------------------------------------------------------------------

# Ben Hansen / Cameron Pilgreen are partnered in event 38 (Men's Double Buck)
# and nowhere else together, so this pair isolates the round trip.
C04_SCRATCHED = 21         # Ben Hansen
C04_PARTNER = 23           # Cameron Pilgreen
C04_PARTNER_RESULT = 97    # Cameron Pilgreen's event 38 row, partner_name='Ben Hansen'
C04_EVENT = 38

# Stirling Hart is Cody labahn's partner in BOTH event 38 (Double Buck) and
# event 40 (Partnered Axe Throw). Two separate partner effects are offered for
# him, so ticking one and not the other is a real judge decision and the test
# can tell a scoped wipe from a name-wide one.
C04_MULTI_SCRATCHED = 4    # Stirling Hart
C04_MULTI_PARTNER = 26     # Cody labahn
C04_TICKED_RESULT = 117    # labahn's event 38 row  — effect IS ticked
C04_UNTICKED_RESULT = 122  # labahn's event 40 row  — effect is NOT ticked
C04_UNTICKED_EVENT = 40


def _confirm_form(html):
    """(fields, effects) scraped out of the rendered scratch-confirm form.

    fields is every hidden input, ready to post as-is. effects is
    [(index, effect_type, affected_entity_id)] in render order, so a test can
    tick exactly the boxes a judge would tick rather than all of them. The
    unticked case is the whole point of the checkbox, and it is the case the
    over-broad wipe violates.
    """
    start = html.find('scratch-confirm')
    assert start != -1, 'no scratch-confirm form in the rendered page'
    block = html[html.rfind('<form', 0, start):html.find('</form>', start)]

    fields = {}
    for tag in re.findall(r'<input[^>]*>', block):
        name = re.search(r'name="([^"]*)"', tag)
        if not name or 'type="checkbox"' in tag:
            continue
        value = re.search(r'value="([^"]*)"', tag)
        fields[name.group(1)] = value.group(1) if value else ''

    effects = [
        (i, fields.get(f'effect_type_{i}'), fields.get(f'affected_entity_id_{i}'))
        for i in range(int(fields.get('effect_count') or 0))
    ]
    return fields, effects


def _undo_control(client, competitor_id, page_url):
    """(action, fields) of the Undo control on a roster page, or (None, None).

    scratch_confirm redirects the judge back to the roster, so this is the
    only screen a real operator is on after a scratch. Scratch buttons are
    gated on status == 'active' and the confirmation page is only reachable
    through one, so if the roster carries no Undo control the 30-minute window
    does not exist in practice no matter what reverse_cascade would do.
    """
    page = client.get(page_url, headers=BROWSER_HEADERS, follow_redirects=True)
    assert page.status_code == 200, page.status_code
    for form in re.findall(r'<form[^>]*>.*?</form>',
                           page.get_data(as_text=True), re.S):
        action = re.search(r'action="([^"]*)"', form)
        if not action or not action.group(1).endswith(
                f'/competitor/{competitor_id}/scratch-undo'):
            continue
        fields = {}
        for tag in re.findall(r'<input[^>]*>', form):
            name = re.search(r'name="([^"]*)"', tag)
            if not name:
                continue
            value = re.search(r'value="([^"]*)"', tag)
            fields[name.group(1)] = value.group(1) if value else ''
        return action.group(1), fields
    return None, None


def _reach_preview(client, competitor_id):
    """Press the Scratch button for one competitor and return the page HTML."""
    page = client.get(f"/registration/{TID}/pro", headers=BROWSER_HEADERS,
                      follow_redirects=True)
    assert page.status_code == 200, page.status_code
    action = _scratch_form_action(page.get_data(as_text=True), competitor_id)
    assert action, (
        f"no Scratch form for pro competitor {competitor_id} on the pro "
        "dashboard; the entry point this test covers has moved")
    landed = client.post(action, headers=BROWSER_HEADERS, follow_redirects=True)
    assert landed.status_code == 200, landed.status_code
    return landed.get_data(as_text=True)


@pytest.mark.sev2
def test_undoing_a_scratch_gives_the_partner_back_their_partner(client, sql):
    """Undo has to restore the counterparty, not just the person who scratched.

    reverse_cascade restoring the scratched competitor's own status, results
    and partners JSON is the control inside this test: if those assertions
    pass and the partner's do not, undo is selectively broken rather than
    broken outright, which is exactly what makes it dangerous. The judge sees
    'Scratch reversed for Ben Hansen', the competitor is active again, and the
    pair is quietly gone.

    Nothing downstream catches it. routes/scheduling/partners.py's orphan
    queue only lists rows whose partner_name is set AND names a scratched
    competitor; after an undo it is neither, so the queue is empty and the
    pair is simply not seated the next time heats are generated.
    """
    before_name = sql(
        "SELECT partner_name FROM event_results WHERE id = :r",
        r=C04_PARTNER_RESULT)[0][0]
    before_json = _loads(sql(
        "SELECT partners FROM pro_competitors WHERE id = :c",
        c=C04_PARTNER)[0][0])
    assert before_name, "fixture drift: the partner row carries no partner_name"
    assert before_json.get(str(C04_EVENT)), (
        f"fixture drift: competitor {C04_PARTNER} has no event {C04_EVENT} partner")

    fields, effects = _confirm_form(_reach_preview(client, C04_SCRATCHED))
    assert any(e[1] == 'partner' for e in effects), (
        f"no partner effect offered for competitor {C04_SCRATCHED}; this test "
        "would prove nothing")

    ticked = {f'effect_checked_{i}': 'on' for i, _, _ in effects}
    confirmed = client.post(
        f"/scoring/{TID}/competitor/{C04_SCRATCHED}/scratch-confirm",
        data=dict(fields, **ticked), headers=BROWSER_HEADERS,
        follow_redirects=True)
    assert confirmed.status_code == 200, confirmed.status_code

    # Control: the scratch really did reach the partner. Without this the undo
    # assertions below could pass on a cascade that never ran.
    mid_name = sql(
        "SELECT partner_name FROM event_results WHERE id = :r",
        r=C04_PARTNER_RESULT)[0][0]
    assert not mid_name, (
        "the scratch left the partner row untouched; this test is no longer "
        "exercising the partner branch")

    # The judge is standing on the pro roster: that is where scratch_confirm
    # just redirected them. If the Undo control is not here it is nowhere.
    action, undo_fields = _undo_control(
        client, C04_SCRATCHED, f"/registration/{TID}/pro")
    assert action, (
        "no Undo control on the pro roster for a competitor scratched seconds "
        "ago; the 30-minute undo window is unreachable by any operator")
    assert undo_fields.get('competitor_type') == 'pro', (
        f"the Undo form would post competitor_type={undo_fields.get('competitor_type')!r}; "
        "college and pro ids collide, so an untyped undo can restore the twin")
    undone = client.post(action, data=undo_fields, headers=BROWSER_HEADERS,
                         follow_redirects=True)
    assert undone.status_code == 200, undone.status_code

    # In-repro control: the actor's own record comes back. This passed before
    # the fix too, which is why the bug read as a working undo.
    assert sql("SELECT status FROM pro_competitors WHERE id = :c",
               c=C04_SCRATCHED)[0][0] == 'active'

    after_name = sql(
        "SELECT partner_name FROM event_results WHERE id = :r",
        r=C04_PARTNER_RESULT)[0][0]
    assert after_name == before_name, (
        f"undo left event_results.{C04_PARTNER_RESULT}.partner_name="
        f"{after_name!r}; it was {before_name!r} before the scratch")

    after_json = _loads(sql(
        "SELECT partners FROM pro_competitors WHERE id = :c",
        c=C04_PARTNER)[0][0])
    assert after_json.get(str(C04_EVENT)) == before_json[str(C04_EVENT)], (
        f"undo left competitor {C04_PARTNER} partners={after_json}; "
        f"event {C04_EVENT} was {before_json[str(C04_EVENT)]!r}")


@pytest.mark.sev2
def test_an_unticked_partner_effect_leaves_its_event_alone(client, sql):
    """The checkbox is a consent control. Unticked means do not touch.

    Stirling Hart partners Cody labahn in two events, so the page offers two
    partner effects. Ticking only the Double Buck one used to strip Partnered
    Axe Throw as well, because the branch filtered the partners JSON by the
    scratched competitor's NAME across every event key instead of dropping
    the key for the event the effect belongs to.

    The ticked effect is asserted too. A fix that simply stopped writing would
    pass the unticked half of this test and fail the ticked half.
    """
    before = _loads(sql("SELECT partners FROM pro_competitors WHERE id = :c",
                        c=C04_MULTI_PARTNER)[0][0])
    assert before.get(str(C04_EVENT)) and before.get(str(C04_UNTICKED_EVENT)), (
        f"fixture drift: competitor {C04_MULTI_PARTNER} partners={before} no "
        "longer covers both events this test separates")

    fields, effects = _confirm_form(_reach_preview(client, C04_MULTI_SCRATCHED))
    ticked_idx = [i for i, etype, eid in effects
                  if etype == 'partner' and eid == str(C04_TICKED_RESULT)]
    unticked_idx = [i for i, etype, eid in effects
                    if etype == 'partner' and eid == str(C04_UNTICKED_RESULT)]
    assert ticked_idx and unticked_idx, (
        "the page did not offer two separable partner effects "
        f"(effects={effects}); this test would prove nothing")

    confirmed = client.post(
        f"/scoring/{TID}/competitor/{C04_MULTI_SCRATCHED}/scratch-confirm",
        data=dict(fields, **{f'effect_checked_{ticked_idx[0]}': 'on'}),
        headers=BROWSER_HEADERS, follow_redirects=True)
    assert confirmed.status_code == 200, confirmed.status_code

    after = _loads(sql("SELECT partners FROM pro_competitors WHERE id = :c",
                       c=C04_MULTI_PARTNER)[0][0])
    assert str(C04_EVENT) not in after, (
        f"the ticked effect did not clear event {C04_EVENT}: partners={after}")
    assert after.get(str(C04_UNTICKED_EVENT)) == before[str(C04_UNTICKED_EVENT)], (
        f"an unticked effect wiped event {C04_UNTICKED_EVENT}: partners={after}, "
        f"was {before}")

    untouched = sql("SELECT partner_name FROM event_results WHERE id = :r",
                    r=C04_UNTICKED_RESULT)[0][0]
    assert untouched, (
        f"an unticked effect cleared event_results.{C04_UNTICKED_RESULT}."
        "partner_name")


@pytest.mark.sev2
def test_a_scratched_college_member_can_still_be_undone_from_the_team_page(client, sql):
    """The college judge has the same dead end and needs the same way out.

    scratch_confirm sends a college judge back to the team page, where the
    Scratch button is gated on status == 'active'. Without an Undo control
    here the window is unreachable for college exactly as it was for pro.

    competitor_type is asserted because college and pro ids collide on this
    data: an undo posted without it resolves against the wrong table and can
    reinstate a different human.
    """
    team_url = f"/registration/{TID}/college/team/{SCRATCH_COLLEGE_TEAM}"
    page = client.get(team_url, headers=BROWSER_HEADERS, follow_redirects=True)
    assert page.status_code == 200, page.status_code
    action = _scratch_form_action(page.get_data(as_text=True), SCRATCH_COLLEGE)
    assert action, (
        f"no Scratch form for college competitor {SCRATCH_COLLEGE} on team "
        f"{SCRATCH_COLLEGE_TEAM}; the entry point this test covers has moved")

    landed = client.post(action, headers=BROWSER_HEADERS, follow_redirects=True)
    fields, effects = _confirm_form(landed.get_data(as_text=True))
    assert fields.get('competitor_type') == 'college', fields.get('competitor_type')

    confirmed = client.post(
        f"/scoring/{TID}/competitor/{SCRATCH_COLLEGE}/scratch-confirm",
        data=dict(fields, **{f'effect_checked_{i}': 'on' for i, _, _ in effects}),
        headers=BROWSER_HEADERS, follow_redirects=True)
    assert confirmed.status_code == 200, confirmed.status_code
    assert sql("SELECT status FROM college_competitors WHERE id = :c",
               c=SCRATCH_COLLEGE)[0][0] == 'scratched'

    undo_action, undo_fields = _undo_control(client, SCRATCH_COLLEGE, team_url)
    assert undo_action, (
        "no Undo control on the team page for a member scratched seconds ago; "
        "the 30-minute undo window is unreachable by any college judge")
    assert undo_fields.get('competitor_type') == 'college', (
        f"the Undo form would post competitor_type={undo_fields.get('competitor_type')!r}")

    undone = client.post(undo_action, data=undo_fields, headers=BROWSER_HEADERS,
                         follow_redirects=True)
    assert undone.status_code == 200, undone.status_code
    assert sql("SELECT status FROM college_competitors WHERE id = :c",
               c=SCRATCH_COLLEGE)[0][0] == 'active', (
        "the Undo control on the team page did not reinstate the member")


# ---------------------------------------------------------------------------
# c05. Partnered Axe Throw flags every pair as tied with itself, and the
#      throw-off form it then offers corrupts the results if submitted
#      services/scoring_engine.py _detect_axe_ties, throwoff_groups,
#      validate_throwoff_submission; templates/scoring/event_results.html
# ---------------------------------------------------------------------------

PAT_EVENT = 40            # Partnered Axe Throw, pro, is_partnered, has_prelims
COLLEGE_AXE_EVENT = 1     # Axe Throw, college men, solo, 14 real rows


def _pat_pairs(sql):
    """Reciprocal partner pairs on event 40 as ((cid, row_id), (cid, row_id)).

    Read out of the data rather than hardcoded. The 2026 import wrote
    partner_name on both members of every real pair, so the pairing is a
    property of the production rows and not of this test.
    """
    rows = sql("""
        SELECT id, competitor_id, competitor_name, partner_name
        FROM event_results WHERE event_id = :e ORDER BY id
    """, e=PAT_EVENT)
    by_name = {r[2]: r for r in rows}
    seen, pairs = set(), []
    for row_id, cid, name, partner in rows:
        if not partner or name in seen or partner in seen:
            continue
        mate = by_name.get(partner)
        if mate is None or mate[3] != name:
            continue
        seen.add(name)
        seen.add(partner)
        pairs.append(((cid, row_id), (mate[1], mate[0])))
    return pairs


def _score_units(app, row_scores):
    """Mark the given result rows completed at the given scores.

    row_scores maps result row id -> score. Everything else on the event keeps
    whatever status it already has, which in this data is 'pending', so it stays
    out of the completed set calculate_positions works on.
    """
    from database import db
    for row_id, score in row_scores.items():
        db.session.execute(db.text(
            "UPDATE event_results SET result_value = :v, status = 'completed', "
            "throwoff_pending = false, final_position = NULL WHERE id = :r"),
            {"v": score, "r": row_id})
    db.session.commit()


def _score_pat(app, sql, scores):
    """Score the first len(scores) reciprocal pairs, one score per pair.

    Returns the pairs that were scored, in the order the scores were applied.
    """
    pairs = _pat_pairs(sql)
    assert len(pairs) >= len(scores), (
        f"event {PAT_EVENT} yields {len(pairs)} reciprocal pairs in this data; "
        f"this test needs {len(scores)}")
    used = pairs[:len(scores)]
    row_scores = {}
    for (a, b), score in zip(used, scores):
        row_scores[a[1]] = score
        row_scores[b[1]] = score
    _score_units(app, row_scores)
    return used


def _flagged(sql, event_id):
    return {r[0] for r in sql(
        "SELECT id FROM event_results WHERE event_id = :e AND throwoff_pending",
        e=event_id)}


def _positions(sql, event_id):
    return dict(sql("""
        SELECT id, final_position FROM event_results
        WHERE event_id = :e AND status = 'completed'
    """, e=event_id))


def _finalize(client, event_id):
    r = client.post(f"/scoring/{TID}/event/{event_id}/finalize")
    assert r.status_code in (200, 302), r.data[:400]
    return r


def _results_page(client, event_id):
    r = client.get(f"/scoring/{TID}/event/{event_id}/results")
    assert r.status_code == 200, r.status_code
    return r.data.decode("utf-8", "replace")


_SELECT_RE = re.compile(
    r'<select[^>]*name="(throwoff_pos_\d+)"[^>]*>(.*?)</select>', re.S)
_OPTION_RE = re.compile(r'<option value="(\d+)"([^>]*)>', re.S)


def _throwoff_selects(html):
    """[(field_name, [option values], selected value or None)] from the form."""
    out = []
    for name, body in _SELECT_RE.findall(html):
        values, selected = [], None
        for value, attrs in _OPTION_RE.findall(body):
            values.append(int(value))
            if "selected" in attrs:
                selected = int(value)
        out.append((name, values, selected))
    return out


@pytest.mark.sev2
def test_five_pairs_at_five_scores_is_not_a_throw_off(app, client, sql):
    """A partnered pair is one competing unit, not two tied competitors.

    services/partnered_axe.py writes the pair's single score to both member
    rows, so every pair is automatically a two-row bucket at whatever score it
    earned. The tie detector counted rows, so five pairs at five DISTINCT
    scores produced ten flagged rows and a red "Throw-Off Required" banner over
    a result set holding no tie at all. The judge's only way past the banner is
    a form that then rewrites the positions.
    """
    scored = _score_pat(app, sql, [30, 28, 24, 20, 16])
    _finalize(client, PAT_EVENT)

    flags = _flagged(sql, PAT_EVENT)
    assert flags == set(), (
        f"five pairs at five distinct scores flagged {len(flags)} rows as "
        f"needing a throw-off: {sorted(flags)}. Each pair's two rows carry the "
        f"pair's one score, so counting rows makes every pair tied with itself.")

    positions = _positions(sql, PAT_EVENT)
    for index, (a, b) in enumerate(scored, 1):
        assert positions.get(a[1]) == index and positions.get(b[1]) == index, (
            f"pair {index} landed at {positions.get(a[1])}/{positions.get(b[1])}"
            f"; both members of a pair share one finishing position")

    html = _results_page(client, PAT_EVENT)
    assert "Throw-Off Required" not in html, (
        "the results page still shows the throw-off banner with no tie present")


@pytest.mark.sev2
def test_a_real_tie_between_two_pairs_still_flags_exactly_those_pairs(app, client, sql):
    """The control. Suppressing the phantom must not suppress the real thing.

    Two pairs on the same score is a genuine tie: two distinct competing units
    contesting one place. Exactly those four rows must flag, and no others.
    """
    scored = _score_pat(app, sql, [30, 28, 24, 24, 16])
    _finalize(client, PAT_EVENT)

    tied_rows = {scored[2][0][1], scored[2][1][1],
                 scored[3][0][1], scored[3][1][1]}
    flags = _flagged(sql, PAT_EVENT)
    assert flags == tied_rows, (
        f"a genuine two-pair tie flagged {sorted(flags)}; the four rows of the "
        f"two pairs sharing a score are {sorted(tied_rows)}")


@pytest.mark.sev2
@pytest.mark.parametrize("tie", [False, True])
def test_solo_axe_throw_tie_detection_is_unchanged(app, client, sql, tie):
    """The second control. The solo college event must behave exactly as before.

    On a solo event every row is its own competing unit, so row count and unit
    count are the same number and the pair-aware detector must be a no-op.
    """
    rows = [r[0] for r in sql("""
        SELECT id FROM event_results WHERE event_id = :e ORDER BY id
    """, e=COLLEGE_AXE_EVENT)]
    assert len(rows) >= 4, f"event {COLLEGE_AXE_EVENT} has {len(rows)} rows"

    scores = [40 - 2 * i for i in range(len(rows))]
    if tie:
        scores[1] = scores[0]
    _score_units(app, dict(zip(rows, scores)))
    _finalize(client, COLLEGE_AXE_EVENT)

    flags = _flagged(sql, COLLEGE_AXE_EVENT)
    expected = {rows[0], rows[1]} if tie else set()
    assert flags == expected, (
        f"solo event {COLLEGE_AXE_EVENT} flagged {sorted(flags)}, expected "
        f"{sorted(expected)}. Pair-aware detection must not change solo events.")


@pytest.mark.sev2
def test_throwoff_form_offers_the_places_actually_being_contested(app, client, sql):
    """A tie for 3rd is answered with 3 and 4, not with 1 and 2.

    The form built its options from range(1, len(pending) + 1), so a two-pair
    tie for third place offered "Position 1" and "Position 2" and there was no
    way to record the correct answer at all.
    """
    _score_pat(app, sql, [30, 28, 24, 24, 16])
    _finalize(client, PAT_EVENT)

    selects = _throwoff_selects(_results_page(client, PAT_EVENT))
    assert selects, "the throw-off form rendered no position selects"
    for name, values, _selected in selects:
        assert values == [3, 4], (
            f"{name} offered positions {values} for a tie over 3rd and 4th")


@pytest.mark.sev2
def test_a_partnered_unit_gets_one_select_that_cannot_be_split(app, client, sql):
    """One dropdown per pair, and both member rows take the same place.

    Per-row selects let a judge hand the two halves of a pair different
    finishing positions. The wire format is unchanged, so a hand-built POST is
    the remaining way to split a pair, and the server has to refuse that too.
    """
    scored = _score_pat(app, sql, [30, 28, 24, 24, 16])
    _finalize(client, PAT_EVENT)

    selects = _throwoff_selects(_results_page(client, PAT_EVENT))
    assert len(selects) == 2, (
        f"a two-pair tie rendered {len(selects)} selects; a pair is one "
        f"competing unit and gets one dropdown")

    pair_c, pair_d = scored[2], scored[3]
    c_rows = sorted([pair_c[0][1], pair_c[1][1]])
    d_rows = sorted([pair_d[0][1], pair_d[1][1]])

    r = client.post(f"/scoring/{TID}/event/{PAT_EVENT}/throwoff", data={
        f"throwoff_pos_{c_rows[0]}": "3",
        f"throwoff_pos_{d_rows[0]}": "4",
    })
    assert r.status_code in (200, 302), r.data[:400]

    positions = _positions(sql, PAT_EVENT)
    assert positions[c_rows[0]] == positions[c_rows[1]] == 3, (
        f"the pair given position 3 came out at "
        f"{positions[c_rows[0]]}/{positions[c_rows[1]]}")
    assert positions[d_rows[0]] == positions[d_rows[1]] == 4, (
        f"the pair given position 4 came out at "
        f"{positions[d_rows[0]]}/{positions[d_rows[1]]}")


@pytest.mark.sev2
def test_a_hand_built_post_cannot_split_a_pair_across_two_places(app, client, sql):
    """The wire format is per row, so the pair invariant is enforced server side.

    The rendered form cannot produce this body, but the field names are per
    result row and there is no authentication on the throw-off route, so a
    crafted POST is the remaining way in. Refusing beats letting the spread
    helper pick a winner by dictionary order.
    """
    scored = _score_pat(app, sql, [30, 28, 24, 24, 16])
    _finalize(client, PAT_EVENT)

    pair_c, pair_d = scored[2], scored[3]
    c_rows = sorted([pair_c[0][1], pair_c[1][1]])
    d_rows = sorted([pair_d[0][1], pair_d[1][1]])
    before = _positions(sql, PAT_EVENT)

    r = client.post(f"/scoring/{TID}/event/{PAT_EVENT}/throwoff", data={
        f"throwoff_pos_{c_rows[0]}": "3",
        f"throwoff_pos_{c_rows[1]}": "4",   # same pair, other half, other place
        f"throwoff_pos_{d_rows[0]}": "4",
    })
    assert r.status_code in (200, 302), r.data[:400]

    after = _positions(sql, PAT_EVENT)
    assert after[c_rows[0]] == after[c_rows[1]], (
        f"a hand-built POST split one pair across positions "
        f"{after[c_rows[0]]} and {after[c_rows[1]]}")
    assert after == before, (
        f"a submission naming one pair twice was partly written rather than "
        f"refused. before={before} after={after}")


@pytest.mark.sev2
def test_submitting_the_rendered_throwoff_form_untouched_changes_nothing(app, client, sql):
    """Opening the banner and pressing the button must not publish a result.

    Every select rendered on "Position 1" with no server-side check, so an
    operator who pressed the button without touching a dropdown wrote
    final_position=1 to every pending row and the public spectator page then
    showed all of them in first place. A throw-off output is always a
    permutation of the places being contested, so duplicates are mechanically
    wrong and get refused by name rather than written.
    """
    _score_pat(app, sql, [30, 28, 24, 24, 16])
    _finalize(client, PAT_EVENT)

    before = _positions(sql, PAT_EVENT)
    selects = _throwoff_selects(_results_page(client, PAT_EVENT))
    assert selects, "the throw-off form rendered no position selects"
    for name, values, selected in selects:
        assert selected is not None, (
            f"{name} carried no selected option, so the browser sends "
            f"{values[0]} for it whatever the competitor actually scored")
        assert selected == 3, (
            f"{name} opened on position {selected}; both tied pairs currently "
            f"hold 3rd, so that is where the dropdown must start")

    payload = {name: str(selected) for name, _values, selected in selects}
    r = client.post(f"/scoring/{TID}/event/{PAT_EVENT}/throwoff", data=payload,
                    follow_redirects=True)
    assert r.status_code == 200, r.status_code

    after = _positions(sql, PAT_EVENT)
    assert after == before, (
        f"submitting the form exactly as rendered rewrote the positions. "
        f"before={before} after={after}")
    assert sum(1 for p in after.values() if p == 1) <= 2, (
        f"more than one competing unit came out in first place: {after}")


# ---------------------------------------------------------------------------
# Coverage gap (not a backlog item): nothing asserted that an unauthenticated
# POST to a management write route is refused.
#
# scoring.record_throwoff carries no @login_required, unlike finalize_event
# two hundred lines above it.  That looked like a hole and is not one: the
# before_request gate in app.py (require_judge_for_management_routes) admits
# scoring only to an authenticated principal holding can_score, and refuses
# every unsafe method from a principal without can_write.  Eight of the
# thirteen POST routes in routes/scoring.py rely on that gate the same way, so
# the decorator's absence is the house style rather than an oversight.
#
# What was actually missing is the test.  tests/test_role_access_control.py
# covers the gate with GET only (there is not one .post in the file), and the
# read-only-role write test above enters through reporting, where can_report
# admits a principal the write check then stops.  No test anywhere held the
# unauthenticated half of the gate on a write route.  The gate is one
# before_request hook with a list of prefix exemptions above it; adding a
# prefix there is a one-line change that would open every scoring POST at
# once, silently, with the whole suite still green.  This closes that.
# ---------------------------------------------------------------------------

@pytest.mark.sev2
def test_an_unauthenticated_post_cannot_resolve_a_throw_off(app, client, sql):
    """No session, no position change. With a control proving the payload works.

    The control is the point. Asserting only that the anonymous POST changed
    nothing passes just as happily against a typo in the URL, a payload the
    validator rejects, or an event with no tie to resolve. So the same payload
    is replayed as the admin afterwards and has to land, which makes the first
    assertion mean "the gate stopped it" rather than "nothing was going to
    happen anyway".
    """
    used = _score_pat(app, sql, [30, 28, 24, 24, 16])
    _finalize(client, PAT_EVENT)

    flagged = _flagged(sql, PAT_EVENT)
    assert flagged, "no throw-off is pending, so this test would prove nothing"

    tied = [pair for pair in used if pair[0][1] in flagged]
    assert len(tied) == 2, (
        f"expected the two 24-point pairs to be flagged, got {len(tied)}")
    payload = {f"throwoff_pos_{tied[0][0][1]}": "3",
               f"throwoff_pos_{tied[1][0][1]}": "4"}

    before = _positions(sql, PAT_EVENT)

    anon = app.test_client()
    resp = anon.post(f"/scoring/{TID}/event/{PAT_EVENT}/throwoff", data=payload)
    after_anon = _positions(sql, PAT_EVENT)

    assert resp.status_code != 200, (
        f"an anonymous POST to record_throwoff returned {resp.status_code}; "
        f"a management write route must never serve an unauthenticated caller")
    assert after_anon == before, (
        f"an anonymous POST moved finishing positions on event {PAT_EVENT}. "
        f"before={before} after={after_anon}. record_throwoff has no "
        f"@login_required, so the only thing standing between the open "
        f"internet and the results board is the before_request gate in app.py")

    r_admin = client.post(f"/scoring/{TID}/event/{PAT_EVENT}/throwoff",
                          data=payload, follow_redirects=True)
    assert r_admin.status_code == 200, r_admin.status_code
    after_admin = _positions(sql, PAT_EVENT)
    assert after_admin != before, (
        "the control failed: the same payload changed nothing as the admin "
        "either, so the anonymous assertion above proved nothing about "
        "authentication")


# ---------------------------------------------------------------------------
# c06. One competitor can be registered into two Partnered Axe pairs, and the
#      unique constraint on event_results turns that into silent
#      last-write-wins on their published score
#      services/partnered_axe.py register_pair;
#      routes/partnered_axe.py _eligible_pros
# ---------------------------------------------------------------------------

PAT_BASE = f"/tournament/{TID}/partnered-axe"

# Real pros, really entered in event 40. Trevor Baker (1) is the one this test
# tries to register twice; Kate Page (2) and Stirling Hart (4) are the two
# different people he is paired with.
PAT_DOUBLE = 1
PAT_MATE_A = 2
PAT_MATE_B = 4


def _pat_state(sql, event_id=PAT_EVENT):
    raw = sql("SELECT event_state FROM events WHERE id = :e", e=event_id)[0][0]
    return _loads(raw) if raw else {}


def _pat_register(client, c1, c2):
    return client.post(f"{PAT_BASE}/register-pair",
                       data={"competitor1_id": str(c1), "competitor2_id": str(c2)},
                       follow_redirects=True)


def _pat_member_ids(state):
    """Every competitor id appearing in any registered pair, with duplicates."""
    out = []
    for pair in state.get("pairs", []):
        for key in ("competitor1", "competitor2"):
            member = pair.get(key) or {}
            if member.get("id") is not None:
                out.append(member["id"])
    return out


@pytest.mark.sev2
def test_a_competitor_cannot_be_registered_into_two_partnered_axe_pairs(client, sql):
    """register_pair appends unconditionally, so one thrower can hold two slots.

    services/partnered_axe.py register_pair validates tenancy, active status
    and event entry, then appends to state['pairs'] with no check that either
    competitor is already in a pair. routes/partnered_axe.py only blocks
    pairing someone with themselves.

    The damage is downstream and silent. event_results carries at most one row
    per competitor per event (uq_event_result_competitor), so both
    _sync_prelim_to_event_results and _save_event_results take the
    filter_by(...).first() update-in-place branch. A competitor in two pairs
    therefore does not raise IntegrityError, they get whichever pair was
    written last, and the results board publishes a score that contradicts the
    partnered-axe standings page for the same person.
    """
    first = _pat_register(client, PAT_DOUBLE, PAT_MATE_A)
    assert first.status_code == 200, first.status_code

    state = _pat_state(sql)
    assert len(state.get("pairs", [])) == 1, (
        f"the control failed: registering the first pair produced "
        f"{len(state.get('pairs', []))} pairs, so nothing below is meaningful")

    second = _pat_register(client, PAT_DOUBLE, PAT_MATE_B)
    assert second.status_code == 200, second.status_code

    state = _pat_state(sql)
    members = _pat_member_ids(state)
    assert members.count(PAT_DOUBLE) == 1, (
        f"competitor {PAT_DOUBLE} is registered in {members.count(PAT_DOUBLE)} "
        f"pairs on event {PAT_EVENT}. pairs={state.get('pairs')}. One person "
        f"cannot throw two partnered axe targets, and event_results has room "
        f"for exactly one score for them, so the second registration can only "
        f"overwrite the first.")
    assert len(state.get("pairs", [])) == 1, (
        f"the second registration was accepted: {len(state.get('pairs', []))} "
        f"pairs now stand where one thrower is in two of them")

    # And the score the audience sees survives. Pair 1 scored 20; if the
    # second registration had landed, scoring it would silently rewrite
    # competitor 1's single result row.
    r = client.post(f"{PAT_BASE}/prelims/record",
                    data={"pair_id": "1", "hits": "20"}, follow_redirects=True)
    assert r.status_code == 200, r.status_code

    published = sql("""
        SELECT result_value FROM event_results
        WHERE event_id = :e AND competitor_id = :c AND competitor_type = 'pro'
    """, e=PAT_EVENT, c=PAT_DOUBLE)
    assert len(published) == 1, (
        f"expected exactly one result row for competitor {PAT_DOUBLE}, got "
        f"{len(published)}")
    assert float(published[0][0]) == 20.0, (
        f"competitor {PAT_DOUBLE} publishes {published[0][0]} on the results "
        f"board while their pair scored 20")


@pytest.mark.sev2
def test_the_pairing_dropdown_stops_offering_an_already_paired_competitor(client, sql):
    """_eligible_pros filters on event entry but not on being already paired.

    The judge running the event sees the same 33 names after every
    registration, including the people already standing at a target. The
    service-layer guard now refuses the submission, but a dropdown that offers
    a choice the server will reject is a trap, not a safeguard: the judge finds
    out only after the POST, mid-event.
    """
    r = _pat_register(client, PAT_DOUBLE, PAT_MATE_A)
    assert r.status_code == 200, r.status_code
    assert len(_pat_state(sql).get("pairs", [])) == 1, (
        "the control failed: the first pair did not register")

    html = client.get(f"{PAT_BASE}/").data.decode("utf-8", "replace")
    selects = re.findall(r'<select[^>]*name="competitor1_id"[^>]*>(.*?)</select>',
                         html, re.S)
    assert len(selects) == 1, (
        f"expected one competitor1_id select on the dashboard, found "
        f"{len(selects)}")
    offered = {int(v) for v in re.findall(r'<option value="(\d+)"', selects[0])}

    assert offered, "the control failed: the dropdown offered nobody at all"
    assert PAT_MATE_B in offered, (
        f"the control failed: competitor {PAT_MATE_B} is entered in event "
        f"{PAT_EVENT} and unpaired, so the dropdown must still offer them")
    assert PAT_DOUBLE not in offered, (
        f"competitor {PAT_DOUBLE} is already in a registered pair but the "
        f"dashboard still offers them for a second one. offered={sorted(offered)}")
    assert PAT_MATE_A not in offered, (
        f"competitor {PAT_MATE_A} is already in a registered pair but the "
        f"dashboard still offers them for a second one")


# ---------------------------------------------------------------------------
# c07. The pro-entry importer keys find-or-create on email alone, so any
#      competitor whose row carries no email, or whose email changed, or who
#      retypes it in different case, is written as a brand new person
#      routes/import_routes.py confirm_pro_entries
# ---------------------------------------------------------------------------

# Real rows in the production mirror, tournament 2.
#   1 Trevor Baker      email NULL   <- the blank-email fork
#   4 Stirling Hart     email NULL
#   5 Brianna Kvinge    briannakvinge@gmail.com  <- the changed-email fork
#   8 Erin LaVoie       elavoie57@gmail.com      <- the control, and the
#                                                   case-variant fork
IMP_NOEMAIL_ID = 1
IMP_NOEMAIL_NAME = "Trevor Baker"
IMP_CHANGED_ID = 5
IMP_CHANGED_NAME = "Brianna Kvinge"
IMP_CHANGED_OLD = "briannakvinge@gmail.com"
IMP_CHANGED_NEW = "brianna.kvinge@gmail.com"
IMP_CONTROL_ID = 8
IMP_CONTROL_NAME = "Erin LaVoie"
IMP_CONTROL_EMAIL = "elavoie57@gmail.com"

_IMP_HEADERS = [
    "Timestamp",
    "Email Address",
    "Full Name",
    "Gender",
    "Mailing Address",
    "Phone Number",
    "Are you a current ALA member?",
    "Men's Underhand",
    "Women's Standing Block",
    "I would like to enter into the Pro-Am lottery",
    "Are you sharing gear?",
    "I know that logging events are dangerous and that I accept all risk.",
    "Signature",
]


def _imp_row(name, gender, email="", event=None):
    """One Google-Forms response row, keyed by header text."""
    row = {
        "Timestamp": "2026-03-01 09:00:00",
        "Email Address": email,
        "Full Name": name,
        "Gender": "Male" if gender == "M" else "Female",
        "Mailing Address": "100 Test Rd, Missoula MT",
        "Phone Number": "4065550100",
        "Are you a current ALA member?": "Yes",
        "I would like to enter into the Pro-Am lottery": "No",
        "Are you sharing gear?": "No",
        "I know that logging events are dangerous and that I accept all risk.": "Yes",
        "Signature": name,
    }
    if event is None:
        event = "Men's Underhand" if gender == "M" else "Women's Standing Block"
    row[event] = "Yes"
    return row


def _imp_workbook(rows):
    """Build the Google-Forms-shaped .xlsx the importer expects, in memory."""
    import io

    import openpyxl

    wb = openpyxl.Workbook()
    ws = wb.worksheets[0]
    ws.title = "Form Responses 1"
    ws.append(_IMP_HEADERS)
    for row in rows:
        ws.append([row.get(h, "") for h in _IMP_HEADERS])
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf


def _imp_run(client, app, tmp_path, rows):
    """Drive the real upload -> review -> confirm flow and return the roster."""
    app.config["UPLOAD_FOLDER"] = str(tmp_path)

    up = client.post(
        f"/import/{TID}/pro-entries",
        data={"file": (_imp_workbook(rows), "entries.xlsx")},
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    assert up.status_code == 200, up.status_code
    with client.session_transaction() as sess:
        parsed = sess.get(f"pro_import_{TID}")
    assert parsed, (
        "the control failed: the upload never parsed, so nothing downstream "
        "of it is being tested. The confirm step reads the session key "
        f"pro_import_{TID} and it is not set.")

    conf = client.post(f"/import/{TID}/pro-entries/confirm", follow_redirects=True)
    assert conf.status_code == 200, conf.status_code


def _imp_rows_named(sql, name):
    # email lives on the identity spine as of r7f8a0b2c3d4, not on
    # pro_competitors. The join is on uid, which is NOT NULL and a foreign key,
    # so it cannot drop a row the old single-table SELECT would have returned.
    return sql("""
        SELECT p.id, p.name, c.email
        FROM pro_competitors p
        JOIN competitors c ON c.uid = p.uid
        WHERE p.tournament_id = :t AND lower(btrim(p.name)) = :n
        ORDER BY p.id
    """, t=TID, n=name.strip().lower())


def _imp_roster_size(sql):
    return sql("SELECT count(*) FROM pro_competitors WHERE tournament_id = :t",
               t=TID)[0][0]


@pytest.mark.sev2
def test_reimporting_a_pro_who_has_no_email_does_not_create_a_second_row(
        client, app, sql, tmp_path):
    """Four pros in the 2026 roster have no email address on file.

    confirm_pro_entries only looks a competitor up when the incoming row
    carries an email, so every one of those four is created fresh on any
    re-import. The second row is a different competitor id, which means a
    second set of EventResult rows, a second entry-fee bill, and the same
    human standing in two heats of the same event. Erin LaVoie is the
    control: her stored address matches the submitted one exactly, so she
    must take the update-in-place branch even against the current code.
    """
    before = _imp_roster_size(sql)
    assert len(_imp_rows_named(sql, IMP_NOEMAIL_NAME)) == 1, (
        f"the control failed: {IMP_NOEMAIL_NAME} is not a single existing row")

    _imp_run(client, app, tmp_path, [
        _imp_row(IMP_NOEMAIL_NAME, "M"),
        _imp_row(IMP_CONTROL_NAME, "F", email=IMP_CONTROL_EMAIL),
    ])

    control = _imp_rows_named(sql, IMP_CONTROL_NAME)
    assert len(control) == 1, (
        f"the control failed: {IMP_CONTROL_NAME} submitted her exact stored "
        f"address and still forked into {len(control)} rows {control}. The "
        f"email match path itself is broken, so this test proves nothing "
        f"about the no-email path.")
    assert control[0][0] == IMP_CONTROL_ID, control

    rows = _imp_rows_named(sql, IMP_NOEMAIL_NAME)
    assert len(rows) == 1, (
        f"{IMP_NOEMAIL_NAME} already exists as pro_competitors id "
        f"{IMP_NOEMAIL_ID} with no email on file, and re-importing his entry "
        f"created a second row. Now {len(rows)} rows: {rows}. Both carry "
        f"their own EventResult rows and their own entry_fees, so he is "
        f"billed twice and scheduled into two heats of the same event.")
    assert rows[0][0] == IMP_NOEMAIL_ID, (
        f"the existing row id {IMP_NOEMAIL_ID} was expected to be updated in "
        f"place, but the surviving row is {rows[0]}")
    assert _imp_roster_size(sql) == before, (
        f"the pro roster grew from {before} to {_imp_roster_size(sql)} on a "
        f"re-import of two people who were both already registered")


@pytest.mark.sev2
def test_a_pro_who_changed_their_email_does_not_fork_into_a_second_row(
        client, app, sql, tmp_path):
    """Competitors resubmit the form with a new address all the time.

    The lookup is an equality match on the stored email, so a new address
    matches nothing and the importer creates a second competitor rather
    than updating the one already holding this person's event entries.
    """
    before = _imp_roster_size(sql)
    existing = _imp_rows_named(sql, IMP_CHANGED_NAME)
    assert len(existing) == 1 and existing[0][2] == IMP_CHANGED_OLD, (
        f"the control failed: expected one {IMP_CHANGED_NAME} row holding "
        f"{IMP_CHANGED_OLD}, found {existing}")

    _imp_run(client, app, tmp_path, [
        _imp_row(IMP_CHANGED_NAME, "F", email=IMP_CHANGED_NEW),
    ])

    rows = _imp_rows_named(sql, IMP_CHANGED_NAME)
    assert len(rows) == 1, (
        f"{IMP_CHANGED_NAME} is pro_competitors id {IMP_CHANGED_ID}. She "
        f"resubmitted the entry form from a new address and the importer "
        f"created a second person instead of updating her. Now {len(rows)} "
        f"rows: {rows}.")
    assert rows[0][0] == IMP_CHANGED_ID, (
        f"the existing row id {IMP_CHANGED_ID} was expected to be updated in "
        f"place, but the surviving row is {rows[0]}")
    assert _imp_roster_size(sql) == before, (
        f"the pro roster grew from {before} to {_imp_roster_size(sql)}")


@pytest.mark.sev2
def test_the_same_email_typed_in_a_different_case_matches_the_existing_pro(
        client, app, sql, tmp_path):
    """Email addresses are case-insensitive in the mailbox and in the form.

    Four rows in the 2026 roster are stored with an address that is not
    equal to lower(btrim(address)), so the exact-equality lookup already
    misses in production data. Retyping the same address with a capital
    letter or a trailing space forks the competitor.
    """
    before = _imp_roster_size(sql)
    assert len(_imp_rows_named(sql, IMP_CONTROL_NAME)) == 1, (
        f"the control failed: {IMP_CONTROL_NAME} is not a single existing row")

    _imp_run(client, app, tmp_path, [
        _imp_row(IMP_CONTROL_NAME, "F", email="  ELAVOIE57@Gmail.COM "),
    ])

    rows = _imp_rows_named(sql, IMP_CONTROL_NAME)
    assert len(rows) == 1, (
        f"{IMP_CONTROL_NAME} is pro_competitors id {IMP_CONTROL_ID} holding "
        f"{IMP_CONTROL_EMAIL}. The same address typed with different case and "
        f"surrounding whitespace created a second competitor. Now "
        f"{len(rows)} rows: {rows}.")
    assert rows[0][0] == IMP_CONTROL_ID, (
        f"the existing row id {IMP_CONTROL_ID} was expected to be updated in "
        f"place, but the surviving row is {rows[0]}")
    assert _imp_roster_size(sql) == before, (
        f"the pro roster grew from {before} to {_imp_roster_size(sql)}")


@pytest.mark.sev2
def test_one_person_listed_twice_in_the_same_upload_lands_on_one_row(
        client, app, sql, tmp_path):
    """The pipeline's own deduplicator groups by email and passes every
    email-less row through untouched (services/registration_import.py
    _deduplicate). Two no-email rows for the same person in one workbook
    therefore reach the writer as two entries, and the writer creates a
    person per entry.
    """
    before = _imp_roster_size(sql)

    _imp_run(client, app, tmp_path, [
        _imp_row(IMP_NOEMAIL_NAME, "M"),
        _imp_row(IMP_NOEMAIL_NAME, "M"),
    ])

    rows = _imp_rows_named(sql, IMP_NOEMAIL_NAME)
    assert len(rows) == 1, (
        f"{IMP_NOEMAIL_NAME} appeared twice in one upload, with no email on "
        f"either row, and the importer wrote {len(rows)} competitors: {rows}. "
        f"A duplicated form submission is the normal case the review screen "
        f"exists to catch, and the confirm button is all-or-nothing.")
    assert _imp_roster_size(sql) == before, (
        f"the pro roster grew from {before} to {_imp_roster_size(sql)}")


# ---------------------------------------------------------------------------
# 17. The Pro-Am Relay Double Buck saw log vanishes from the wood order when
#     nobody has typed a team count, while the two relay BLOCK rows for the
#     same three teams are on the same report.
#     services/woodboss.py calculate_saw_wood, the `if relay_team_count:` gate
# ---------------------------------------------------------------------------

RELAY_SAW_LABEL = "Pro-Am Relay — Double Buck"
RELAY_SAW_KEY = "log_relay_doublebuck"
RELAY_BLOCK_KEYS = ("block_relay_underhand", "block_relay_standing")


def _wb_rows(app, tid=TID):
    """(blocks, saw_wood) straight from the service the report renders."""
    with app.app_context():
        from services import woodboss as wb
        return wb.calculate_blocks(tid), wb.calculate_saw_wood(tid)


def _larch_inches(app, tid=TID):
    """Linear inches of Western Larch on the purchase order sheet."""
    with app.app_context():
        from services import woodboss as wb
        blocks = wb.calculate_blocks(tid)
        saw = wb.calculate_saw_wood(tid)
        for item in wb.get_ordering_summary(blocks, saw):
            if item["category"] == "log" and item["species"] == "Western Larch":
                return item["total_inches"]
    raise AssertionError("no Western Larch log line on the order sheet")


def _set_relay_saw_count(app, value, tid=TID):
    with app.app_context():
        from database import db
        from models.wood_config import WoodConfig
        cfg = WoodConfig.query.filter_by(
            tournament_id=tid, config_key=RELAY_SAW_KEY).first()
        assert cfg is not None, "log_relay_doublebuck config row is missing"
        cfg.count_override = value
        db.session.commit()


@pytest.mark.sev2
def test_the_relay_double_buck_log_is_on_the_report_with_no_team_count_typed(
        client, app, sql):
    """A missing number must show as a zero, never as a missing row.

    The shipped 2026 config carries species Western Larch and diameter 18in on
    log_relay_doublebuck with count_override NULL, while both relay block keys
    carry count_override 3. The operator filled in the blocks and left the saw
    count blank, which is the exact mistake a visible row exists to catch.
    calculate_saw_wood instead drops the row entirely: no line, no zero, no
    warning. The woodboss reads a complete-looking saw table and buys 6" less
    Western Larch than the relay needs.

    Every other row in this module is emitted at zero on purpose.
    calculate_saw_wood says so at the top of its emit loop ("Always include
    college and pro rows (even if zero) for visibility") and calculate_blocks
    emits the relay block row unconditionally. This one row disagrees.
    """
    override = sql(
        "SELECT count_override FROM wood_configs "
        "WHERE tournament_id = :t AND config_key = :k",
        t=TID, k=RELAY_SAW_KEY)
    assert override and override[0][0] is None, (
        f"precondition failed: this test measures the NULL count case, but "
        f"log_relay_doublebuck holds count_override={override}")

    _blocks, saw = _wb_rows(app)
    labels = [r["event_label"] for r in saw]
    assert RELAY_SAW_LABEL in labels, (
        f"calculate_saw_wood emitted {len(saw)} rows and none of them is the "
        f"relay double buck log. The same three relay teams are visible in the "
        f"block table on the same page. Rows emitted: {labels}")

    row = next(r for r in saw if r["event_label"] == RELAY_SAW_LABEL)
    assert row["competitor_count"] == 0, (
        f"with no count typed the relay row must read zero teams, got {row}")
    assert row["species"] == "Western Larch", (
        f"the relay log species is configured and must survive onto the row, "
        f"got {row['species']!r}")
    assert row["size_value"] == 18.0, (
        f"the relay log diameter is configured and must survive onto the row, "
        f"got {row['size_value']!r}")
    assert row["total_inches"] == 0.0, (
        f"zero teams is zero inches, not a null. The report footer sums "
        f"total_inches, got {row['total_inches']!r}")

    page = client.get(f"/woodboss/{TID}/report")
    assert page.status_code == 200, page.data[:400]
    body = page.get_data(as_text=True)
    assert RELAY_SAW_LABEL in body, (
        "the relay double buck log is absent from the rendered wood report")


@pytest.mark.sev2
def test_the_report_does_not_open_a_second_pro_saw_events_section(client, app):
    """The saw table groups rows under one heading per division.

    The heading is chosen by comp_type and the relay row's comp_type is
    'relay', which the template's two-way college/pro test funnels into the
    'else' arm. So the moment a relay row exists the table grows a second
    "Pro Saw Events" divider immediately above it. That is invisible in
    production today only because the row itself is being dropped.
    """
    _set_relay_saw_count(app, 3)
    body = client.get(f"/woodboss/{TID}/report").get_data(as_text=True)
    assert body.count("Pro Saw Events") == 1, (
        f"the saw table opened {body.count('Pro Saw Events')} sections titled "
        f"'Pro Saw Events'. The relay row belongs under its own heading.")


@pytest.mark.sev2
def test_the_relay_block_rows_keep_their_three_teams(app):
    """Positive control. The blocks side is already correct and must stay so."""
    blocks, _saw = _wb_rows(app)
    by_key = {b["config_key"]: b for b in blocks}
    for key in RELAY_BLOCK_KEYS:
        assert key in by_key, f"{key} row disappeared from calculate_blocks"
        assert by_key[key]["competitor_count"] == 3, (
            f"{key} must still report its 3 manually entered teams, got "
            f"{by_key[key]}")
        assert by_key[key]["is_manual"] is True, (
            f"{key} must still be flagged manual, got {by_key[key]}")


@pytest.mark.sev2
def test_typing_three_relay_teams_still_adds_six_inches_of_larch(app):
    """Positive control on the path that already works.

    Western Larch 18in is also the general saw log spec, so the relay teams
    land in the group the woodboss actually buys against. Making the zero case
    visible must not disturb this.
    """
    before = _larch_inches(app)
    _set_relay_saw_count(app, 3)
    after = _larch_inches(app)
    assert after == before + 6.0, (
        f"three relay teams cut once each at 2\" = 6\" of Western Larch. "
        f"The order sheet went from {before} to {after}.")


@pytest.mark.sev2
def test_the_relay_log_row_survives_a_tournament_with_no_relay_saw_config(
        client, app, sql):
    """A tournament nobody has configured yet is the case that needs the row most.

    calculate_blocks emits every key in BLOCK_CONFIG_LABELS whether or not a
    wood_configs row exists for it, falling back to a blank spec. Keying the
    saw row's existence on the config row instead of on the count is the same
    bug wearing a different hat: the woodboss opening a fresh tournament would
    see a saw table with no relay line at all and no reason to suspect one is
    missing.
    """
    with app.app_context():
        from database import db
        from models.wood_config import WoodConfig
        WoodConfig.query.filter_by(
            tournament_id=TID, config_key=RELAY_SAW_KEY).delete()
        db.session.commit()
    assert not sql(
        "SELECT 1 FROM wood_configs WHERE tournament_id = :t AND config_key = :k",
        t=TID, k=RELAY_SAW_KEY), "the config row was not actually removed"

    _blocks, saw = _wb_rows(app)
    labels = [r["event_label"] for r in saw]
    assert RELAY_SAW_LABEL in labels, (
        f"with no log_relay_doublebuck config row the relay saw line vanished "
        f"entirely. Rows emitted: {labels}")

    row = next(r for r in saw if r["event_label"] == RELAY_SAW_LABEL)
    assert row["competitor_count"] == 0, row
    assert row["species"] is not None, (
        f"an unconfigured relay log must fall back to the general saw log "
        f"spec, not to a blank species, got {row}")


# ---------------------------------------------------------------------------
# c20. A wrong-gender entrant is dropped from heat generation in total silence,
#      then offered in the Add Competitor dropdown that the server will refuse.
#      services/heat_generator.py:472-474 (the filter)
#      services/heat_generator.py:205-215 (the placement validator it blinds)
#      routes/scheduling/heats.py:75-81 (the dropdown)
#      routes/scheduling/heats.py:637-645 (the guard the dropdown ignores)
#
# Kate Page (pro 2, F) is entered in event 32, pro Underhand, gender M. She is
# the only such row in the whole 2026 database. She has an EventResult row, she
# is billed $10 for the event in entry_fees, and she can never take a start in
# it. Nothing in the app says so.
#
# The placement validator at :205 looks like it would catch this and does not:
# the gender filter runs inside _get_event_competitors BEFORE `competitors` is
# built, so expected_ids never contains her, `missing` is empty, and not even
# the logger.warning fires.
# ---------------------------------------------------------------------------

UH_M = 32           # pro Underhand, gender M, 5 stands, 26 enrolled
UH_F = 33           # pro Underhand, gender F, 13 enrolled, 3 heats already
COOKIE = 43         # pro Cookie Stack, NO gender, 31 enrolled — negative control
KATE = 2            # pro, F, entered in 32/33/34/39/40
CLAY = 3            # pro, M, active, entered only in 36. Never in 32.

_ADD_FORM_RE = re.compile(
    r'<form[^>]+action="[^"]*add-to-heat[^"]*"[^>]*>(.*?)</form>', re.S)
# Named _ADD_OPTION_RE, not _OPTION_RE: the throwoff helpers at the top of
# this file already own that name with a two-group pattern, and shadowing it
# from down here broke three of their tests without touching their code.
_ADD_OPTION_RE = re.compile(r'<option value="(\d+)"')


def _add_dropdown_ids(html):
    """Competitor ids the Add Competitor dropdowns actually offer.

    Scoped to the add-to-heat forms rather than every <option> on the page, so
    the move-competitor and status selects cannot contaminate the reading. The
    heat_id travels as a hidden input, not an option, so it cannot either.
    """
    ids = set()
    for block in _ADD_FORM_RE.findall(html):
        ids.update(int(v) for v in _ADD_OPTION_RE.findall(block))
    return ids


def _placed_ids(sql, event_id):
    out = []
    for _num, comps in sql("SELECT heat_number, competitors FROM heats "
                           "WHERE event_id = :e ORDER BY heat_number",
                           e=event_id):
        out.extend(_loads(comps) or [])
    return [int(c) for c in out]


def _gen(client, flashes, event_id):
    flashes()
    r = client.post(f"/scheduling/{TID}/event/{event_id}/generate-heats",
                    data={"confirm": "true"})
    assert r.status_code in (200, 302), r.data[:400]
    return flashes()


def _named_in_a_warning(said, name):
    """Flashes that name this person under a category the operator reads as
    a problem. Shaped on CATEGORY, not on my wording, so any implementation
    that says it out loud passes whatever words it picks."""
    return [(cat, msg) for cat, msg in said
            if cat in ("warning", "danger", "error") and name in msg]


def _insert_result(app, event_id, comp_id, name, ctype="pro"):
    from database import db
    db.session.execute(db.text(
        "INSERT INTO event_results "
        "(event_id, competitor_id, competitor_type, competitor_name, "
        " points_awarded, payout_amount, status, version_id, is_flagged, "
        " throwoff_pending, handicap_factor, payout_settled) "
        "VALUES (:e, :c, :t, :n, 0, 0, 'pending', 1, false, false, 0, false)"),
        {"e": event_id, "c": comp_id, "t": ctype, "n": name})
    db.session.commit()


# --- the defect -------------------------------------------------------------

@pytest.mark.sev2
def test_generating_heats_says_out_loud_that_an_entered_competitor_was_left_out(
        client, sql, flashes):
    """26 people are entered in event 32. 25 get a stand. Nobody is told.

    Measured against v2026.final: the only flash is
    ('success', "Generated 5 heat(s) for Men's Underhand.") and caplog holds
    no heat_generator warning either.
    """
    said = _gen(client, flashes, UH_M)

    # Vacuity guard: generation must actually have succeeded, or "no success
    # flash" would satisfy this test for the wrong reason.
    assert [m for cat, m in said if cat == "success"], (
        f"heat generation did not succeed at all, so this test proves "
        f"nothing about what it reports: {said}")
    assert len(_placed_ids(sql, UH_M)) == 25, "the roster changed under the test"

    warned = _named_in_a_warning(said, "Kate Page")
    assert warned, (
        f"Kate Page is entered in event {UH_M} and was left out of every heat, "
        f"and the operator was told nothing about it. Flashes: {said}")


@pytest.mark.sev2
def test_the_add_competitor_dropdown_does_not_offer_someone_the_server_refuses(
        client, sql, flashes):
    """The dropdown offers Kate on every heat. add_to_heat refuses her.

    routes/partnered_axe.py:27-36 states this codebase's own position on the
    class: "a dropdown that offers a choice the server will reject is a trap
    the judge only springs mid-event."
    """
    _gen(client, flashes, UH_M)

    page = client.get(f"/scheduling/{TID}/event/{UH_M}/heats")
    assert page.status_code == 200, page.status_code
    offered = _add_dropdown_ids(page.get_data(as_text=True))

    # Not an assertion about Kate by name: nobody the add route will refuse on
    # gender may be offered. Read the genders back out of the database rather
    # than hardcoding them.
    ev_gender = sql("SELECT gender FROM events WHERE id = :e", e=UH_M)[0][0]
    genders = dict(sql("SELECT id, gender FROM pro_competitors "
                       "WHERE tournament_id = :t", t=TID))
    trap = sorted(cid for cid in offered
                  if genders.get(cid) and genders[cid] != ev_gender)
    assert not trap, (
        f"the Add Competitor dropdown offers {trap} for a {ev_gender} event; "
        f"add_to_heat refuses every one of them on gender mismatch")


@pytest.mark.sev2
def test_the_generator_records_the_excluded_entrant_when_called_directly(app):
    """A route-only fix leaves every other caller of the service blind.

    services/schedule_generation.py, routes/scheduling/flights.py,
    routes/scheduling/events.py and two scripts all call generate_event_heats
    without going through the heats route.
    """
    from database import db
    from models import Event
    from services.heat_generator import (
        generate_event_heats,
        get_last_gender_excluded,
    )

    event = Event.query.get(UH_M)
    generate_event_heats(event)
    db.session.commit()

    recorded = get_last_gender_excluded(UH_M)
    ids = [r["comp_id"] for r in recorded]
    assert KATE in ids, (
        f"the service placed 25 of 26 entrants and recorded nothing about the "
        f"26th: {recorded}")

    # And ONLY entrants. Every woman in the tournament is the wrong gender for
    # a men's event; saying so on all of them turns one real problem into a
    # roster dump the operator scrolls past. The record has to be scoped by who
    # is entered, not by who fails the gender check. The entered set is read out
    # of the database rather than hardcoded, so this stays true if the roster
    # moves.
    entered = set()
    for cid, raw in db.session.execute(db.text(
            "SELECT id, events_entered FROM pro_competitors "
            "WHERE tournament_id = :t"), {"t": TID}):
        if UH_M in [int(e) for e in (_loads(raw) or [])]:
            entered.add(cid)
    assert entered, "vacuity guard: nobody reads as entered in event 32 at all"
    stowaways = sorted(set(ids) - entered)
    assert not stowaways, (
        f"the service reported {stowaways} as excluded from event {UH_M} and "
        f"none of them is entered in it. {len(ids)} people named for a problem "
        f"that affects {len(set(ids) & entered)}.")


@pytest.mark.sev2
def test_when_every_entrant_is_the_wrong_gender_the_operator_is_told_who(
        app, client, sql, flashes):
    """The worst version of this lands on the FAILURE path.

    Empty the women's Underhand of eligible entrants and enter one man in it.
    generate_event_heats raises 'No competitors entered', the route swallows
    it and flashes 'see application logs (admin only)', and the one man who IS
    entered is never mentioned. Both halves of that are wrong: competitors ARE
    entered, and the operator is sent to a log file to find out who.
    """
    from database import db
    db.session.execute(db.text(
        "UPDATE pro_competitors SET status = 'scratched' "
        "WHERE tournament_id = :t AND gender = 'F'"), {"t": TID})
    db.session.execute(db.text(
        "UPDATE pro_competitors SET events_entered = :ev WHERE id = :c"),
        {"ev": json.dumps([36, UH_F]), "c": CLAY})
    db.session.commit()

    before = sql("SELECT count(*) FROM heats WHERE event_id = :e", e=UH_F)[0][0]
    said = _gen(client, flashes, UH_F)
    after = sql("SELECT count(*) FROM heats WHERE event_id = :e", e=UH_F)[0][0]

    # Vacuity guard, inverted: this test only means anything if generation
    # really did fail. It raises before _delete_event_heats, so the three
    # production heats are still standing untouched.
    assert after == before, (
        f"heats were rebuilt after all ({before} -> {after}), so this test is "
        f"not exercising the failure path it was written for")
    assert not [m for cat, m in said if cat == "success"], (
        f"generation reported success on an event it could not build: {said}")
    warned = _named_in_a_warning(said, "Clay Stephenson")
    assert warned, (
        f"the only person entered in event {UH_F} is the wrong gender for it, "
        f"and the operator was handed a generic error naming nobody: {said}")


# --- positive controls: these must pass before AND after the fix -------------

@pytest.mark.sev2
def test_the_eligible_men_are_still_placed_when_a_wrong_gender_entrant_is_present(
        client, sql, flashes):
    """The fix must report the excluded entrant, not start dropping people."""
    _gen(client, flashes, UH_M)

    placed = _placed_ids(sql, UH_M)
    heats = sql("SELECT count(*) FROM heats WHERE event_id = :e", e=UH_M)[0][0]
    assert heats == 5, f"expected 5 heats of 5, got {heats}"
    assert len(placed) == 25, f"expected 25 men placed, got {len(placed)}"
    assert len(set(placed)) == 25, f"somebody was placed twice: {sorted(placed)}"
    assert KATE not in placed, (
        "Kate Page was placed in a men's heat — the fix must surface her, not "
        "admit her")


@pytest.mark.sev2
def test_an_unassigned_same_gender_competitor_is_still_offered_in_the_dropdown(
        app, client, sql, flashes):
    """The Add Competitor UI has a job. Filtering must not silence it.

    Clay Stephenson is an active male pro who is not entered in event 32. Give
    him an EventResult row after generation, which is exactly the state the Add
    Competitor dropdown exists to resolve, and check he is still offered.
    """
    _gen(client, flashes, UH_M)
    _insert_result(app, UH_M, CLAY, "Clay Stephenson")

    page = client.get(f"/scheduling/{TID}/event/{UH_M}/heats")
    offered = _add_dropdown_ids(page.get_data(as_text=True))
    assert CLAY in offered, (
        f"an eligible unassigned man vanished from the Add Competitor "
        f"dropdown. Offered: {sorted(offered)}")


@pytest.mark.sev2
def test_an_unresolvable_entry_is_still_offered_in_the_dropdown(
        app, client, sql, flashes):
    """Never hide what cannot be verified.

    ProCompetitor and CollegeCompetitor share an id space, and event_results
    rows can outlive the competitor they point at. An entry whose competitor
    cannot be loaded must stay visible: it is the operator's only sign that the
    row exists at all.
    """
    _gen(client, flashes, UH_M)
    orphan = sql("SELECT max(id) + 1000 FROM pro_competitors")[0][0]
    _insert_result(app, UH_M, orphan, "Orphaned Entry")

    page = client.get(f"/scheduling/{TID}/event/{UH_M}/heats")
    offered = _add_dropdown_ids(page.get_data(as_text=True))
    assert orphan in offered, (
        f"an event_results row with no competitor behind it was hidden from "
        f"the dropdown instead of surfaced. Offered: {sorted(offered)}")


@pytest.mark.sev2
def test_a_non_gendered_event_reports_no_exclusions(client, sql, flashes):
    """The negative control: a fix that warns about everybody is not a fix.

    Cookie Stack has no gender, so no entrant can be the wrong one for it. The
    flash assertion below is the part that carries weight before the fix as
    well as after. The side-channel check is reached through getattr because
    get_last_gender_excluded does not exist yet on the unmodified tree, and is
    stated as vacuous-before rather than dressed up as a passing assertion.
    """
    import services.heat_generator as hg

    said = _gen(client, flashes, COOKIE)

    assert [m for cat, m in said if cat == "success"], (
        f"generation failed, so this control proves nothing: {said}")
    noise = [(c, m) for c, m in said if c in ("warning", "danger", "error")]
    assert not noise, (
        f"a genderless event produced a warning flash: {noise}")

    getter = getattr(hg, "get_last_gender_excluded", None)
    if getter is not None:
        assert getter(COOKIE) == [], (
            f"a genderless event reported gender exclusions: {getter(COOKIE)}")
