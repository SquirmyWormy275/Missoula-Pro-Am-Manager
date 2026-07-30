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


SCRATCH_COLLEGE = 29       # Greer Swoboda, team 5, active, 6 event_results.
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
    pro_twin = sql("SELECT name FROM pro_competitors WHERE id = :c",
                   c=SCRATCH_COLLEGE)
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
