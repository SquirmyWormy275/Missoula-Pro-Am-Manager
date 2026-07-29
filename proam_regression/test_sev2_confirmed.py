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
        f"routes/reporting.py:373 does int(job['result']) on a dict returned "
        f"by _build_flights_async. The flights did build; this is a false "
        f"crash signal during show prep."
    )


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
