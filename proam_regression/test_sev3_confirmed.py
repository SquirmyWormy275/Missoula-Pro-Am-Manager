"""
SEV3 regression tests: degraded behavior confirmed on real 2026 data.

Same contract as the SEV1 and SEV2 files. Every test asserts CORRECT behavior,
so every test in here FAILS against v2026.final. Backlog items 17 and 18.

Item 17 builds a Partnered Axe Throw bracket first. That is unavoidable: the
event exists in production with an EMPTY event_state, because the 2026 show was
run on paper and PAT was never played in the app. Every pair is built from real
pros who are really entered in event 40, through the real registration and
scoring routes. The only invented values are the hit counts, which is the same
narrow exception documented in the college partnered scoring test.

Item 18 invents nothing at all. It replays the operator's own gear parse-review
workflow over the free text the competitors actually submitted.
"""

import json
import re

import pytest
import rig

TID = rig.TOURNAMENT_ID

PAT_BASE = f"/tournament/{TID}/partnered-axe"


# ---------------------------------------------------------------------------
# 17. Partnered Axe advance_to_finals has no idempotency guard
#     services/partnered_axe.py:178-192, can_advance_to_finals stays true
# ---------------------------------------------------------------------------

PAT_EVENT = 40

# Real pros, really entered in event 40. Five pairs, because
# can_advance_to_finals requires at least 4 scored and all pairs scored.
PAT_PAIRS = [(1, 2), (4, 5), (8, 11), (12, 13), (14, 15)]
PAT_PRELIM_HITS = [20, 18, 16, 14, 12]   # descending, so seeding is unambiguous


def _pat_state(sql):
    raw = sql("SELECT event_state FROM events WHERE id = :e", e=PAT_EVENT)[0][0]
    if not raw:
        return {}
    return json.loads(raw) if isinstance(raw, str) else raw


@pytest.mark.sev3
def test_advancing_to_finals_twice_does_not_wipe_recorded_finals(client, sql):
    """A stale tab or a double submit must not reset the finals card.

    can_advance_to_finals only checks prelim completeness, and prelims stay
    complete forever, so it never stops returning True. The route reports
    success every time. The second call reassigns state['finalists'] from the
    prelim standings, which drops any final_score already recorded and pushes
    the stage back to 'finals'.
    """
    for c1, c2 in PAT_PAIRS:
        r = client.post(f"{PAT_BASE}/register-pair",
                        data={"competitor1_id": str(c1), "competitor2_id": str(c2)})
        assert r.status_code in (200, 302), r.data[:400]

    pairs = _pat_state(sql).get("pairs", [])
    assert len(pairs) == len(PAT_PAIRS), (
        f"expected {len(PAT_PAIRS)} registered pairs, got {len(pairs)}")

    for pair, hits in zip(pairs, PAT_PRELIM_HITS):
        r = client.post(f"{PAT_BASE}/prelims/record",
                        data={"pair_id": str(pair["pair_id"]), "hits": str(hits)})
        assert r.status_code in (200, 302), r.data[:400]

    r = client.post(f"{PAT_BASE}/advance-to-finals")
    assert r.status_code in (200, 302), r.data[:400]

    state = _pat_state(sql)
    finalists = state.get("finalists", [])
    assert len(finalists) == 4, f"expected 4 finalists, got {finalists}"
    assert state.get("stage") == "finals", state.get("stage")

    # Score two of the four finals pairs, the way a judge does between throws.
    scored = {}
    for pair, hits in zip(finalists[:2], (15, 13)):
        pid = pair["pair_id"]
        r = client.post(f"{PAT_BASE}/finals/record",
                        data={"pair_id": str(pid), "hits": str(hits)})
        assert r.status_code in (200, 302), r.data[:400]
        scored[pid] = hits

    mid = {p["pair_id"]: p.get("final_score") for p in _pat_state(sql)["finalists"]}
    assert all(mid.get(pid) == hits for pid, hits in scored.items()), (
        f"the harness could not record finals scores at all: {mid}")

    # The stale Advance button gets pressed again.
    r = client.post(f"{PAT_BASE}/advance-to-finals")
    assert r.status_code in (200, 302), r.data[:400]

    after = _pat_state(sql)
    now = {p["pair_id"]: p.get("final_score") for p in after.get("finalists", [])}
    lost = {pid: hits for pid, hits in scored.items() if now.get(pid) != hits}

    assert not lost, (
        f"a second advance-to-finals erased recorded finals scores {lost}. "
        f"finals scores now {now}, stage now {after.get('stage')!r}. "
        f"can_advance_to_finals checks prelim completeness only, so it stays "
        f"True forever and the route flashes success on every press."
    )


def _build_pat_prelims(client, sql):
    """Register the five pairs and score every prelim. Returns the state dict.

    Same construction the defect test above does inline. Factored out here so
    the controls do not each grow their own copy and drift apart from it.
    """
    for c1, c2 in PAT_PAIRS:
        r = client.post(f"{PAT_BASE}/register-pair",
                        data={"competitor1_id": str(c1), "competitor2_id": str(c2)})
        assert r.status_code in (200, 302), r.data[:400]

    pairs = _pat_state(sql).get("pairs", [])
    assert len(pairs) == len(PAT_PAIRS), (
        f"expected {len(PAT_PAIRS)} registered pairs, got {len(pairs)}")

    for pair, hits in zip(pairs, PAT_PRELIM_HITS):
        r = client.post(f"{PAT_BASE}/prelims/record",
                        data={"pair_id": str(pair["pair_id"]), "hits": str(hits)})
        assert r.status_code in (200, 302), r.data[:400]

    return _pat_state(sql)


@pytest.mark.sev3
def test_the_first_advance_to_finals_still_seeds_the_top_four(client, sql):
    """Positive control. The guard must refuse the SECOND press, not the first.

    A guard written against the wrong condition kills the feature outright.
    Refusing when state['finalists'] is already truthy looks equivalent and is
    not; refusing when can_advance_to_finals has ever been True blocks
    everything. This is the whole event: five pairs go in, the top four by
    prelim score come out, seeded highest first. Passes before the fix and has
    to pass after.
    """
    _build_pat_prelims(client, sql)

    r = client.post(f"{PAT_BASE}/advance-to-finals")
    assert r.status_code in (200, 302), r.data[:400]

    state = _pat_state(sql)
    finalists = state.get("finalists", [])
    assert len(finalists) == 4, (
        f"the first advance produced {len(finalists)} finalists, not 4: "
        f"{finalists}. The guard is refusing the press it is supposed to allow.")
    assert state.get("stage") == "finals", (
        f"stage is {state.get('stage')!r} after a successful advance")

    seeds = [p.get("prelim_score") for p in finalists]
    assert seeds == sorted(PAT_PRELIM_HITS, reverse=True)[:4], (
        f"the finalists are not the top four by prelim score, or are not seeded "
        f"highest first: {seeds}. Prelim scores were {PAT_PRELIM_HITS}.")


@pytest.mark.sev3
def test_a_second_advance_press_does_not_report_success(client, sql, flashes):
    """The operator has to be told the press did nothing.

    Fails pre-fix: the route flashes 'Top 4 pairs advanced to finals!' every
    single time, so a judge who presses a stale button sees the same green
    confirmation whether it seeded the bracket or silently destroyed the scores
    they just entered.

    This also pins the shape of the fix. A guard that quietly returns the
    existing finalists protects the data and still leaves the operator
    believing something happened, which on show day means they stop looking for
    the problem. Refusing out loud is the point.
    """
    _build_pat_prelims(client, sql)
    r = client.post(f"{PAT_BASE}/advance-to-finals")
    assert r.status_code in (200, 302), r.data[:400]
    flashes()   # drain the legitimate first-press success

    r = client.post(f"{PAT_BASE}/advance-to-finals")
    assert r.status_code in (200, 302), r.data[:400]
    said = flashes()

    assert said, (
        "the second press produced no flash at all. Silence is not an answer "
        "either: the operator pressed a button and the page came back with "
        "nothing to say about it.")
    lying = [(cat, msg) for cat, msg in said
             if cat == "success" or "advanced to finals" in msg.lower()]
    assert not lying, (
        f"the second advance press reported success: {lying}. Nothing advanced. "
        f"The finals bracket was already seeded and the stage is already "
        f"'finals'.")


@pytest.mark.sev3
def test_a_completed_partnered_axe_event_cannot_be_pushed_back_to_finals(client, sql):
    """The worst version of item 17: a finished event, re-opened.

    Fails pre-fix. Once all four finals scores are in, record_final_result
    assigns final_position, writes the placings back into state['pairs'], sets
    stage to 'completed' and calls _save_event_results, which persists rows in
    the EventResult table. can_advance_to_finals is still True at that point,
    because it only ever looks at prelims. So one press on a stale prelims tab
    reseeds finalists from the prelim standings, drops all four final scores
    and placings, and drags a completed, published event back to 'finals' while
    the EventResult rows it already wrote stay behind saying otherwise.
    """
    _build_pat_prelims(client, sql)
    r = client.post(f"{PAT_BASE}/advance-to-finals")
    assert r.status_code in (200, 302), r.data[:400]

    finalists = _pat_state(sql)["finalists"]
    assert len(finalists) == 4, finalists
    for pair, hits in zip(finalists, (15, 13, 11, 9)):
        r = client.post(f"{PAT_BASE}/finals/record",
                        data={"pair_id": str(pair["pair_id"]), "hits": str(hits)})
        assert r.status_code in (200, 302), r.data[:400]

    done = _pat_state(sql)
    assert done.get("stage") == "completed", (
        f"the harness could not finish the event; stage is "
        f"{done.get('stage')!r}, so this test is not checking what it claims")
    placed = {p["pair_id"]: p.get("final_position") for p in done["finalists"]}
    assert all(v is not None for v in placed.values()), placed

    r = client.post(f"{PAT_BASE}/advance-to-finals")
    assert r.status_code in (200, 302), r.data[:400]

    after = _pat_state(sql)
    now = {p["pair_id"]: p.get("final_position")
           for p in after.get("finalists", [])}
    assert after.get("stage") == "completed", (
        f"a completed event was dragged back to stage {after.get('stage')!r}")
    assert now == placed, (
        f"the placings changed on a completed event. was {placed}, now {now}. "
        f"EventResult rows were already written from the old placings, so the "
        f"published results and the event state now disagree.")


@pytest.mark.sev3
def test_resetting_partnered_axe_lets_the_bracket_be_run_again(client, sql):
    """Positive control. The guard must live in state, not beside it.

    reset() rebuilds self.state wholesale from a literal. A guard implemented
    as anything other than a value inside that dict (a column, an attribute, a
    module-level set of event ids) survives the reset and leaves the operator
    permanently unable to re-run the bracket, with the only recovery being
    hand-editing event_state in psql at a live show. Passes before the fix and
    has to pass after.
    """
    _build_pat_prelims(client, sql)
    r = client.post(f"{PAT_BASE}/advance-to-finals")
    assert r.status_code in (200, 302), r.data[:400]
    assert _pat_state(sql).get("stage") == "finals"

    r = client.post(f"{PAT_BASE}/reset")
    assert r.status_code in (200, 302), r.data[:400]
    cleared = _pat_state(sql)
    assert cleared.get("stage") == "prelims", cleared.get("stage")
    assert not cleared.get("pairs"), cleared.get("pairs")

    _build_pat_prelims(client, sql)
    r = client.post(f"{PAT_BASE}/advance-to-finals")
    assert r.status_code in (200, 302), r.data[:400]

    state = _pat_state(sql)
    assert len(state.get("finalists", [])) == 4, (
        f"after a reset the bracket could not be advanced again: "
        f"stage={state.get('stage')!r} finalists={state.get('finalists')}. "
        f"The idempotency guard outlived the state it is supposed to be part of.")


# ---------------------------------------------------------------------------
# 18. Gear free-text parser collapses multiple sharing partners to one
#     services/gear_sharing.py:531-554 picks one name per details string
# ---------------------------------------------------------------------------

GEAR_COMPETITOR = 40         # Owen Vredenburg
GEAR_CONFLICT_PARTNER = 38   # Mason Banks
SHARED_HEAT = 428            # Double Buck heat 4, holds both


@pytest.mark.sev3
def test_gear_parser_keeps_every_declared_sharing_partner(client, sql):
    """One saw shared with three people is three scheduling constraints.

    The parser picks exactly one partner name for the whole details string,
    the longest roster match, and applies it to every matched event and
    category. The other declared names vanish with no warning, so
    build_gear_conflict_pairs never learns about them and the scheduler puts
    two people who share a saw in the same heat.
    """
    name, details = sql("""
        SELECT name, gear_sharing_details FROM pro_competitors WHERE id = :c
    """, c=GEAR_COMPETITOR)[0]
    assert details and "," in details, (
        f"{name} does not declare multiple gear clauses in this data")

    roster = {n for (n,) in sql("""
        SELECT name FROM pro_competitors
        WHERE tournament_id = :t AND status = 'active'
    """, t=TID)}

    # Every roster surname the competitor actually wrote down. Surname matching
    # keeps the misspellings in the real data (Vrendenburg, labahn) honest.
    declared = set()
    lowered = details.lower()
    for person in roster:
        if person == name:
            continue
        surname = person.split()[-1].lower()
        first = person.split()[0].lower()
        if surname in lowered and first[:4] in lowered:
            declared.add(person)
    assert len(declared) >= 2, (
        f"{name} names fewer than two roster members: {declared}")

    # Run the operator's own review-and-confirm workflow over the real text.
    review = client.get(f"/registration/{TID}/pro/gear-sharing/parse-review")
    assert review.status_code == 200, (review.status_code, review.data[:400])

    r = client.post(f"/registration/{TID}/pro/gear-sharing/parse-confirm",
                    data={f"confirm_{GEAR_COMPETITOR}": "on"})
    assert r.status_code in (200, 302), r.data[:400]

    raw = sql("SELECT gear_sharing FROM pro_competitors WHERE id = :c",
              c=GEAR_COMPETITOR)[0][0]
    stored = json.loads(raw) if isinstance(raw, str) else (raw or {})
    kept = " | ".join(str(v) for v in stored.values()).lower()

    dropped = sorted(p for p in declared
                     if p.split()[-1].lower() not in kept)

    assert not dropped, (
        f"{name} declared gear sharing with {sorted(declared)} but the stored "
        f"map keeps only {sorted(set(stored.values()))}. Dropped: {dropped}. "
        f"No warning is raised. services/gear_sharing.py picks one partner "
        f"name per details string and applies it to every matched event."
    )


@pytest.mark.sev3
def test_dropped_gear_partner_is_not_scheduled_into_the_same_heat(client, sql):
    """The scheduling consequence of the dropped name, on shipped data.

    Owen Vredenburg declared a shared Single Buck saw with Mason Banks. That
    name never reached the structured map, so build_gear_conflict_pairs has no
    edge between them, and both are in Double Buck heat 4. Crosscut saws are
    one gear family in this app, so that heat cannot be run as scheduled.
    """
    from models import Tournament
    from services.gear_sharing import build_gear_conflict_pairs

    tournament = Tournament.query.get(TID)
    pairs = build_gear_conflict_pairs(tournament)

    a, b = GEAR_COMPETITOR, GEAR_CONFLICT_PARTNER
    names = dict(sql("SELECT id, name FROM pro_competitors WHERE id IN (:a, :b)",
                     a=a, b=b))

    raw = sql("SELECT competitors FROM heats WHERE id = :h", h=SHARED_HEAT)[0][0]
    members = json.loads(raw) if isinstance(raw, str) else raw
    both_in_heat = a in members and b in members
    assert both_in_heat, (
        f"heat {SHARED_HEAT} no longer holds both competitors: {members}. "
        f"This test is anchored to shipped 2026 scheduling.")

    linked = b in pairs.get(a, set()) or a in pairs.get(b, set())
    assert linked, (
        f"{names.get(a)} and {names.get(b)} declare a shared crosscut saw in "
        f"their own registration text, but build_gear_conflict_pairs sees no "
        f"conflict between them, and the scheduler placed both in Double Buck "
        f"heat {SHARED_HEAT}. One saw, one heat, two competitors."
    )


# ---------------------------------------------------------------------------
# c08. The report cache's disk layer pickles live SQLAlchemy rows, so the
#      college standings page 500s for a full TTL after a worker respawns
#      services/report_cache.py, routes/reporting.py college_standings
#
#      Filed SEV2. Verified SEV3: the outage is self-limiting (it clears on
#      the next TTL expiry or on any invalidate_tournament_caches call), the
#      blast radius is one admin screen, and the /print variant and the
#      spectator portal both stay 200 throughout. It is still a hard 500 on a
#      page a judge reads between events, and the trigger, a gunicorn worker
#      dying and respawning onto an already warm shelve file, needs nothing
#      from the operator to happen.
# ---------------------------------------------------------------------------

RPT_URL = f"/reporting/{TID}/college/standings"
RPT_PRINT_URL = f"/reporting/{TID}/college/standings/print"
RPT_KEY = f"reports:{TID}:college_standings"

# Real 2026 college roster. Every active college competitor in this tournament
# has a team, so c.team is a live relationship on every row the page renders
# and 'N/A' is never the correct output for any of them.
RPT_TOP_MAN = "Abe Chentnik"
RPT_TOP_MAN_TEAM = "FVC-A"
RPT_TOP_TEAM_SCHOOL = "Colorado State University"


def _rpt_isolate(tmp_path):
    """Point the report cache at a private shelve file and hand back a restore.

    report_cache keeps its state in module globals, so without this a test
    would write into the checkout's instance/ directory and leak entries into
    whatever test runs next in the same process.
    """
    import services.report_cache as rc

    saved = (rc._shelf_path, rc._shelf_resolved, dict(rc._cache))
    rc._shelf_path = str(tmp_path / "cache")
    rc._shelf_resolved = True
    with rc._lock:
        rc._cache.clear()

    def _restore():
        with rc._lock:
            rc._cache.clear()
            rc._cache.update(saved[2])
        rc._shelf_path, rc._shelf_resolved = saved[0], saved[1]

    return rc, _restore


def _rpt_break_builders(monkeypatch):
    """Make every database-side builder for this page explode.

    Without this the test is vacuous: if the disk layer fails to serve the
    second request, the route just rebuilds the payload from the database and
    renders a perfectly good page, and the assertion passes while measuring
    nothing.
    """
    from models.tournament import Tournament

    def _boom(*args, **kwargs):
        raise AssertionError(
            "the route rebuilt the payload from the database; the disk cache "
            "layer never served this request, so this test proves nothing")

    for name in ('get_bull_of_woods', 'get_belle_of_woods',
                 'get_bull_belle_with_tiebreak_data', 'get_team_standings'):
        monkeypatch.setattr(Tournament, name, _boom)


@pytest.mark.sev3
def test_college_standings_survives_a_worker_respawn_onto_a_warm_disk_cache(
        client, app, tmp_path, monkeypatch):
    """A judge reloading standings after a worker restart must get standings.

    gunicorn runs the app with a single worker. When that worker dies and is
    respawned, the module-level L1 dict dies with the process and the shelve
    file in instance/report_cache does not. The new worker's first request for
    this page reads the disk entry, gets back CollegeCompetitor objects that
    are detached from every session, and dies in the template on c.team.
    """
    rc, restore = _rpt_isolate(tmp_path)
    try:
        first = client.get(RPT_URL)
        assert first.status_code == 200, first.status_code
        body_before = first.get_data(as_text=True)

        # Controls. The page really renders competitor and team data, so a
        # failure below is a cache failure and not an empty-roster artifact.
        assert RPT_TOP_MAN in body_before
        assert RPT_TOP_MAN_TEAM in body_before
        assert RPT_TOP_TEAM_SCHOOL in body_before

        # Control. The uncached print variant of the same data is fine, which
        # localizes any failure below to the caching path.
        assert client.get(RPT_PRINT_URL).status_code == 200

        # Control. The disk layer actually engaged. On a machine where the
        # instance directory is unwritable this assertion is the only thing
        # standing between a green run and a meaningless one.
        assert rc._shelf_get(RPT_KEY) is not None, (
            "nothing reached the disk cache, so the worker-respawn path below "
            "is not being exercised")

        # The worker dies and respawns.
        with rc._lock:
            rc._cache.clear()
        _rpt_break_builders(monkeypatch)

        second = client.get(RPT_URL)
        assert second.status_code == 200, (
            f"standings returned {second.status_code} on the first request "
            f"after a worker respawn, and will keep doing it until the cache "
            f"entry expires")
        body_after = second.get_data(as_text=True)
        assert RPT_TOP_MAN in body_after
        assert RPT_TOP_MAN_TEAM in body_after, (
            "the page came back but the team column did not; a cached payload "
            "that renders 'N/A' where a team code belongs is not a fix")
        assert RPT_TOP_TEAM_SCHOOL in body_after
        assert body_after.count("N/A") == body_before.count("N/A")
    finally:
        restore()


@pytest.mark.sev3
def test_the_cached_standings_payload_holds_no_live_database_rows(
        client, app, tmp_path):
    """Whatever is cached has to be able to outlive the session that built it."""
    rc, restore = _rpt_isolate(tmp_path)
    try:
        assert client.get(RPT_URL).status_code == 200
        with rc._lock:
            payload = rc._cache[RPT_KEY]['value']

        # Control: the payload is the real thing, not an empty dict that would
        # trivially contain no entities.
        assert payload['bull_tiebreak'], "no Bull rows to check"
        assert payload['team_standings'], "no team rows to check"

        assert not rc._contains_orm_entity(payload), (
            "the standings payload still carries SQLAlchemy rows, so the disk "
            "layer will hand a detached copy to the next worker")
    finally:
        restore()


@pytest.mark.sev3
def test_the_report_cache_refuses_to_put_database_rows_on_disk(app, tmp_path):
    """The guard, tested directly, so the next caller cannot repeat c08."""
    from models.competitor import CollegeCompetitor

    rc, restore = _rpt_isolate(tmp_path)
    try:
        row = CollegeCompetitor.query.filter_by(tournament_id=TID).first()
        assert row is not None, "no college competitor to build the probe from"

        rc.set("reports:c08probe:entities", {"rows": [{"competitor": row}]}, 60)
        assert rc._shelf_get("reports:c08probe:entities") is None, (
            "a payload carrying a live database row reached the disk layer")
        # L1 is not implicated and must keep working: the objects never leave
        # the process that loaded them.
        assert rc.get("reports:c08probe:entities") is not None

        # Control: the guard is not a blanket refusal. Plain data still lands.
        rc.set("reports:c08probe:plain", {"rows": [{"name": row.name}]}, 60)
        assert rc._shelf_get("reports:c08probe:plain") is not None, (
            "the disk layer stopped accepting plain data, which breaks the "
            "whole point of having an L2")
    finally:
        restore()


# ---------------------------------------------------------------------------
# c09. The ALA membership PDF cannot be produced at all, and the failed
#      attempt still marks the compliance document as printed
#      requirements.txt (no reportlab), services/print_catalog.py record_print
#
#      Filed SEV2. Verified SEV3: nothing in the live show loop touches
#      reportlab, the ALA data itself is intact, and the HTML page renders, so
#      the Download path has a same-minute workaround in browser print-to-PDF.
#      The two things with no workaround are the "Email to ALA" button, whose
#      only job is to attach a PDF, and the false "Fresh" badge, which can
#      convince an operator the filing already happened.
# ---------------------------------------------------------------------------

ALA_HTML_URL = f"/reporting/ala-membership-report/{TID}"
ALA_PDF_URL = f"/reporting/ala-membership-report/{TID}/pdf"
STANDINGS_PRINT_URL = f"/reporting/{TID}/college/standings/print"
HUB_URL = f"/scheduling/{TID}/print-hub"

# A real 2026 pro attendee, so the HTML control below cannot pass on an
# empty report.
ALA_REAL_ATTENDEE = "Trevor Baker"


def _tracker_rows(sql, doc_key):
    """Read print_trackers back from the database rather than the ORM.

    The write happens in the request's session and the assertion runs in the
    test's, so going through the ORM here would risk reading an identity-map
    copy instead of what was actually committed.
    """
    return sql(
        "SELECT id, entity_id, last_printed_at FROM print_trackers "
        "WHERE tournament_id = :tid AND doc_key = :key ORDER BY id",
        tid=TID, key=doc_key)


def _hub_row(html: str, needle: str) -> str:
    """Return the single Print Hub table row containing needle.

    Asserting against the whole hub page is worthless: it lists every document
    and the words "Never printed" and "Fresh" both appear on it regardless.

    Pass a needle that only the hub table can contain, normally the document's
    own print_url. A bare report slug is not safe: the base template renders a
    sidebar nav link to the same report roughly 108,000 bytes above the table,
    outside any <tr>, so html.find() lands there and rfind("<tr", ...) returns
    -1. The assertion below catches that rather than silently matching the
    wrong element.
    """
    marker = html.find(needle)
    assert marker != -1, f"{needle!r} is not on the Print Hub page at all"
    start = html.rfind("<tr", 0, marker)
    end = html.find("</tr>", marker)
    assert start != -1 and end != -1, (
        f"could not isolate a table row around {needle!r}; the match at "
        f"offset {marker} is probably not inside the hub table")
    return html[start:end]


@pytest.mark.sev3
def test_a_failed_print_does_not_mark_the_document_as_printed(
        client, sql, monkeypatch):
    """record_print's own contract, held against a view that catches its error.

    The decorator's docstring promises that if the view raises, no tracker row
    is written. ala_membership_report_pdf does not raise: it catches, logs,
    flashes and redirects. The decorator saw a return value, treated the failed
    print as a success, and upserted a PrintTracker row, so the Print Hub
    reported the ALA compliance document as "Fresh ... by STRATHEX" with zero
    bytes ever produced.

    This is asserted through the ALA route because that is where it was found,
    but the gate being tested lives in the decorator and covers all seventeen
    @record_print routes.
    """
    import services.ala_report as ala_report

    # Control. The production mirror has never printed this document, so any
    # row found below was written by the failed attempt and nothing else.
    assert _tracker_rows(sql, 'ala_report') == []

    def _boom(report_data):
        raise ModuleNotFoundError("No module named 'reportlab'")

    # The route does its import inside the function body, so patching the
    # module attribute is what the route will actually resolve at call time.
    monkeypatch.setattr(ala_report, 'generate_ala_pdf', _boom)

    response = client.get(ALA_PDF_URL)

    # Control on the control: the route really did take its failure path. If
    # this ever returns 200 the test below is measuring nothing.
    assert response.status_code == 302, response.status_code

    assert _tracker_rows(sql, 'ala_report') == [], (
        "a print that produced no bytes was recorded as a completed print, so "
        "the Print Hub will report the ALA filing as done")

    hub = client.get(HUB_URL)
    assert hub.status_code == 200
    row = _hub_row(hub.get_data(as_text=True), ALA_PDF_URL)
    assert 'Never printed' in row, row
    assert 'Fresh' not in row, row


@pytest.mark.sev3
def test_a_successful_print_is_still_recorded(client, sql):
    """Guard against over-tightening the gate above.

    This one passes against unfixed code on purpose. Its job is to fail if the
    fix starts refusing to record prints that really did deliver a document,
    which would silently break the whole Print Hub staleness feature.
    """
    assert _tracker_rows(sql, 'college_standings') == []

    response = client.get(STANDINGS_PRINT_URL)
    assert response.status_code == 200, response.status_code

    assert len(_tracker_rows(sql, 'college_standings')) == 1, (
        "a print that returned a document was not recorded")


@pytest.mark.sev3
def test_the_ala_membership_report_can_actually_be_produced_as_a_pdf(
        client, sql):
    """The ALA filing has to leave the building as a PDF.

    generate_ala_pdf imports reportlab, reportlab is not in requirements.txt,
    so the Railway image does not have it either. The Download button has a
    workaround (the HTML page below renders fine and the operator can print to
    PDF from the browser). The "Email to ALA" button does not: attaching the
    PDF is its only function, and the association simply never receives the
    filing.
    """
    # Control. The data layer and the HTML page are fine, so a failure below
    # is the PDF renderer and not the report itself.
    html = client.get(ALA_HTML_URL)
    assert html.status_code == 200, html.status_code
    assert ALA_REAL_ATTENDEE in html.get_data(as_text=True), (
        "the ALA report rendered no attendees, so the PDF assertion below "
        "would be vacuous")

    response = client.get(ALA_PDF_URL)
    assert response.status_code == 200, (
        f"the ALA PDF route returned {response.status_code}; the Email to ALA "
        f"button shares this code path and has no workaround")
    assert response.mimetype == 'application/pdf', response.mimetype

    body = response.get_data()
    assert body.startswith(b'%PDF-'), body[:40]
    assert len(body) > 2000, f"only {len(body)} bytes, which is not a report"

    # The other half of the record_print contract: a print that really did
    # deliver bytes must be recorded.
    assert len(_tracker_rows(sql, 'ala_report')) == 1


# ---------------------------------------------------------------------------
# 19. Flight-start SMS resolves competitors in a single integer namespace
#     routes/scheduling/flights.py:830-840
#
#     Found while adversarially gating the flights candidates, not filed in the
#     original audit. Same class as CONFIRMED SEV1 item 2: ProCompetitor and
#     CollegeCompetitor have overlapping primary keys (21 of 64 college ids are
#     below 49), and this path keys a dict by the bare integer. Last write wins,
#     the flight's heats are iterated with no ORDER BY, and the college heats
#     come back last, so a pro standing in a pro heat is relabelled college and
#     dropped from the notify list. The college branch is a documented no-op
#     because CollegeCompetitor has no phone column, so he is simply not texted.
#
#     Live today only because zero pros in the mirror have opted in. The moment
#     one does, this is a real person who does not get his heads-up.
# ---------------------------------------------------------------------------

SMS_TRIGGER_FLIGHT_ID = 11        # flight_number 4
SMS_TARGET_FLIGHT_NUMBER = 7      # 4 + SMS_NOTIFY_FLIGHTS_AHEAD (3)
SMS_START_URL = f"/scheduling/{TID}/flights/{SMS_TRIGGER_FLIGHT_ID}/start"

# Real pros standing in real PRO heats of flight 7 whose integer id is also a
# real college competitor id in the same flight. Measured, not assumed: the
# pro-heat id set and the college-heat id set of flight 7 intersect in exactly
# these three integers, so this dict is the whole population, not a sample.
#
# Completeness here is load-bearing, and it is the thing this list originally
# got wrong. It named 29 and 37, because those are the two the unfixed route
# drops on the row order this database happens to return. Id 33 is masked in
# exactly the same way and survived only because his pro heat was read after
# his college twin's. A first-write-wins mutant, which is the other obvious
# way somebody might "fix" a last-write-wins dict, loses 33 and keeps 29 and
# 37, so it passed a battery run against the two-name version of this list.
# Naming all three is what makes the assertion independent of row order.
SMS_MASKED_PROS = {
    29: "Dwight Severson",   # college id 29 is Greer Swoboda
    33: "Jack Love",         # college id 33 is Brad Applegate
    37: "Karson Wilson",     # college id 37 is Zach Cardenas
}

# A real pro in the same flight with no college twin. He must be texted before
# and after the fix. Without him every assertion below could pass vacuously on
# a path that sends nothing at all.
SMS_CLEAN_PRO = (9, "Gillian Shannon")

# A real pro who is NOT in flight 7 at all, whose id belongs to a college
# competitor who IS. He must never be texted. This is the trap on the other
# side: resolving the id against both tables would text the wrong man.
SMS_GHOST_PRO = (32, "Ian Wilson")

# Nothing here is invented except the opt-in flag. All four carry the phone
# numbers they really submitted.


@pytest.fixture()
def sms_outbox(app, monkeypatch):
    """Capture what the flight-start route would have texted.

    is_configured() is False in every environment this suite runs in, because
    twilio is not installed, so the notify path is a no-op unless it is forced
    open. submit_job is intercepted so no job ever runs and no network call is
    ever attempted.
    """
    import routes.scheduling.flights as flights_route
    import services.sms_notify as sms

    sent: list = []

    monkeypatch.setattr(sms, "is_configured", lambda: True)
    monkeypatch.setattr(
        flights_route, "submit_job",
        lambda label, fn, *a, **k: sent.append((label, a)))
    return sent


def _opt_in(app, pro_ids):
    """Turn on SMS consent for real pros who already carry a real phone."""
    with app.app_context():
        from database import db
        from models.competitor import ProCompetitor
        for pid in pro_ids:
            comp = ProCompetitor.query.filter_by(
                id=pid, tournament_id=TID).first()
            assert comp is not None, f"pro {pid} is missing from the mirror"
            assert comp.phone, (
                f"pro {pid} has no phone in production, so opting him in "
                f"would prove nothing")
            comp.phone_opted_in = True
        db.session.commit()


def _texted(outbox):
    return {label.split("sms:", 1)[1] for label, _ in outbox if
            label.startswith("sms:")}


@pytest.mark.sev3
def test_a_pro_is_still_texted_when_a_college_competitor_shares_his_id(
        client, app, sms_outbox):
    """Dwight Severson, Jack Love and Karson Wilson are standing in flight 7.

    College competitors carry the same integer ids (Greer Swoboda is 29,
    Brad Applegate is 33, Zach Cardenas is 37), so a bare-int type map keeps
    only the last label written for an id. A pro is relabelled as college,
    and so never notified, when his college twin's heat is read AFTER his
    own. Which of the three that hits is decided by row order the query does
    not fix, so this test asserts on the whole measured population in
    SMS_MASKED_PROS, not a sample of it.
    """
    _opt_in(app, list(SMS_MASKED_PROS) + [SMS_CLEAN_PRO[0]])

    response = client.post(SMS_START_URL)
    assert response.status_code in (200, 302), response.status_code

    texted = _texted(sms_outbox)

    # Vacuity guard. If the notify path is dead the assertions below mean
    # nothing, so prove it is alive with a pro who has no id twin.
    assert SMS_CLEAN_PRO[1] in texted, (
        f"the notify path sent nothing to {SMS_CLEAN_PRO[1]}, who has no "
        f"college twin, so this test cannot say anything about the ones who "
        f"do. sent: {sorted(texted)}")

    for pid, name in SMS_MASKED_PROS.items():
        assert name in texted, (
            f"{name} (pro id {pid}) is in a pro heat in flight "
            f"{SMS_TARGET_FLIGHT_NUMBER} and opted in, but was not texted "
            f"because college id {pid} is in the same flight. sent: "
            f"{sorted(texted)}")


@pytest.mark.sev3
def test_a_pro_who_is_not_in_the_flight_is_never_texted(
        client, app, sms_outbox):
    """The other half of the namespace contract.

    Ian Wilson is pro id 32 and is not in flight 7. College id 32 is Toby
    Bartsch and he is. Resolving the id against both tables, which is the
    obvious way to stop losing the masked pros above, would send Ian a heads
    up for a flight he is not in. This test passes against unfixed code on
    purpose.
    """
    _opt_in(app, [SMS_GHOST_PRO[0], SMS_CLEAN_PRO[0]])

    response = client.post(SMS_START_URL)
    assert response.status_code in (200, 302), response.status_code

    texted = _texted(sms_outbox)

    assert SMS_CLEAN_PRO[1] in texted, (
        f"the notify path sent nothing at all, so the assertion below is "
        f"vacuous. sent: {sorted(texted)}")
    assert SMS_GHOST_PRO[1] not in texted, (
        f"{SMS_GHOST_PRO[1]} (pro id {SMS_GHOST_PRO[0]}) is not in flight "
        f"{SMS_TARGET_FLIGHT_NUMBER}. He was texted because college id "
        f"{SMS_GHOST_PRO[0]} is. sent: {sorted(texted)}")


@pytest.mark.sev3
def test_nobody_is_texted_when_nobody_opted_in(client, sms_outbox):
    """Consent is the gate and it stays the gate.

    Every pro in the mirror has phone_opted_in False. Starting the flight must
    send zero messages. This test passes against unfixed code on purpose; its
    job is to fail if a fix widens the recipient set instead of correcting the
    lookup.
    """
    response = client.post(SMS_START_URL)
    assert response.status_code in (200, 302), response.status_code
    assert sms_outbox == [], (
        f"messages were queued for competitors who never opted in: "
        f"{sorted(_texted(sms_outbox))}")


@pytest.mark.sev3
def test_starting_a_flight_still_marks_it_in_progress(
        client, sql, sms_outbox):
    """The route's actual job, guarded.

    Notification is a side effect. If a fix raises inside the notify helper
    the flight never flips and the booth loses its status board, so assert the
    primary effect separately. Passes against unfixed code on purpose.
    """
    before = sql("SELECT status FROM flights WHERE id = :f",
                 f=SMS_TRIGGER_FLIGHT_ID)[0][0]
    assert before != 'in_progress', before

    response = client.post(SMS_START_URL)
    assert response.status_code in (200, 302), response.status_code

    after = sql("SELECT status FROM flights WHERE id = :f",
                f=SMS_TRIGGER_FLIGHT_ID)[0][0]
    assert after == 'in_progress', after


# The college half of the notify path has no observable output today:
# CollegeCompetitor has no `phone` column, so its branch queries the database
# and then does nothing with the rows. That deadness hides mistakes. A fix that
# tags the pro side correctly and leaves the college side keyed on bare ints
# passes every assertion above, because the wrong college lookup produces no
# text either way. Measured on the mirror, that mistake hands the college table
# 22 ids that came out of PRO heats, 9 of which are real college competitors
# who are not in this flight at all: Mateo Angel, Teagan Wigen, Maria Pyeatt,
# John Nelson, Trevor Norris, Cooper Driskell, Aiden Springer, Trustin Norick,
# Ellana Schreifels. Same ghost-recipient defect as Ian Wilson below, just
# parked on the dead side of the branch until somebody adds a phone column.
#
# So this one watches the actual SQL. It is not a peek at my own
# implementation: whatever shape the notify path takes, it has no business
# asking the college table about an id it only ever saw in a pro heat.

@pytest.fixture()
def college_lookup_params(app):
    """Record the bound parameters of every college_competitors query."""
    from sqlalchemy import event

    from database import db

    seen: list = []

    def _capture(conn, cursor, statement, parameters, context, executemany):
        if "college_competitors" in statement and parameters:
            seen.append(parameters)

    engine = db.engine
    event.listen(engine, "before_cursor_execute", _capture)
    yield seen
    event.remove(engine, "before_cursor_execute", _capture)


def _flight_id_sets(app, flight_number):
    """The pro-heat and college-heat id sets for a flight, read from the db."""
    with app.app_context():
        from models import Event, Flight

        flight = Flight.query.filter_by(
            tournament_id=TID, flight_number=flight_number).first()
        assert flight is not None, f"flight {flight_number} is not in the mirror"
        heats = list(flight.heats.all())
        events = {
            e.id: e
            for e in Event.query.filter(
                Event.id.in_({h.event_id for h in heats})).all()
        }
        pro_ids, col_ids = set(), set()
        for heat in heats:
            event_row = events.get(heat.event_id)
            if not event_row:
                continue
            for cid in heat.get_competitors():
                target = pro_ids if event_row.event_type == "pro" else col_ids
                target.add(int(cid))
        return pro_ids, col_ids


@pytest.mark.sev3
def test_the_college_lookup_is_never_handed_an_id_from_a_pro_heat(
        client, app, sms_outbox, college_lookup_params):
    """The college table must only be asked about college-heat ids.

    Flight 7 has 25 ids in pro heats and 11 in college heats, overlapping on
    exactly three (29, 33, 37). An id that appears only in a pro heat must
    never reach a college_competitors lookup, or the notify path is resolving
    people by an integer that means two different competitors.
    """
    pro_ids, col_ids = _flight_id_sets(app, SMS_TARGET_FLIGHT_NUMBER)
    pro_only = pro_ids - col_ids
    assert pro_only, (
        "the mirror no longer has any pro-only id in this flight, so this "
        "test cannot discriminate anything")

    _opt_in(app, [SMS_CLEAN_PRO[0]])
    del college_lookup_params[:]      # ignore the setup traffic above

    # Deliberately NOT following the redirect. The flight_list page issues 18
    # further college lookups spanning every flight in the tournament, and an
    # id that is pro-only in flight 7 is an ordinary college-heat id in some
    # other flight. Following the redirect made this test read those as leaks
    # and fail against correct code. Only the POST is the notify path.
    resp = client.post(SMS_START_URL, follow_redirects=False)
    assert resp.status_code == 302

    assert SMS_CLEAN_PRO[1] in _texted(sms_outbox), (
        "the flight-start notify path did not run at all, so watching its "
        "queries proves nothing")

    leaked = set()
    for params in college_lookup_params:
        rows = params if isinstance(params, (list, tuple)) else [params]
        for row in rows:
            values = row.values() if isinstance(row, dict) else row
            for value in values:
                if isinstance(value, int) and value in pro_only:
                    leaked.add(value)
                elif isinstance(value, (list, tuple)):
                    leaked |= {v for v in value if v in pro_only}

    assert not leaked, (
        f"ids {sorted(leaked)} appear only in PRO heats of flight "
        f"{SMS_TARGET_FLIGHT_NUMBER}, but the notify path asked the college "
        f"table about them. Those integers name different people in the two "
        f"tables.")


# ---------------------------------------------------------------------------
# c19. Partnered Axe score entry reports success on work it did not do.
#      services/partnered_axe.py record_prelim_result / record_final_result,
#      routes/partnered_axe.py:148-176 and :215-241.
#
# Two doors, one failure mode. record_prelim_result has no stage guard, so a
# prelim recorded after the bracket has been run overwrites the published
# EventResult rows of the pairs that already placed. Neither writer checks
# that the pair_id it was handed exists, so a typo is a silent no-op that the
# route still flashes as a success.
#
# Sibling of item 17. That one guarded advance_to_finals. These are the other
# two writers on the same state machine, reached from the same stale tab.
# ---------------------------------------------------------------------------

PAT_FINAL_HITS = [15, 13, 11, 9]

_REFUSAL_CATS = ("danger", "error", "warning")

_ER_ROWS = ("SELECT competitor_id, result_value, final_position, status "
            "FROM event_results WHERE event_id = :e ORDER BY competitor_id")


def _assert_refused_out_loud(said, why):
    """The operator must be told the entry did not land, and not told it did.

    Shaped on flash CATEGORY, not on message wording. The first version of
    this check flagged any message containing 'recorded', which also caught
    the refusal 'Nothing was recorded.' That made the assertion a test of my
    own phrasing rather than of behaviour. Any implementation that refuses
    under a refusal category, and claims nothing under a neutral one, passes,
    whatever words it uses.
    """
    assert said, f"{why}: no flash at all. Silence reads as success."
    claims = [(c, m) for c, m in said if c not in _REFUSAL_CATS]
    assert not claims, f"{why}: the app reported this as done: {claims}"
    refusals = [(c, m) for c, m in said if c in _REFUSAL_CATS]
    assert refusals, (
        f"{why}: nothing in {said} tells the operator the entry was refused")


def _er(sql):
    """{competitor_id: (result_value, final_position, status)} for event 40."""
    return {r[0]: (r[1], r[2], r[3]) for r in sql(_ER_ROWS, e=PAT_EVENT)}


def _build_pat_complete(client, sql):
    """Prelims, the cut, and all four finals scores. Returns the state dict.

    Ends with stage 'completed', final_position assigned on all four finalists
    and EventResult rows published by _save_event_results.
    """
    _build_pat_prelims(client, sql)
    r = client.post(f"{PAT_BASE}/advance-to-finals")
    assert r.status_code in (200, 302), r.data[:400]

    finalists = _pat_state(sql).get("finalists", [])
    assert len(finalists) == 4, finalists
    for pair, hits in zip(finalists, PAT_FINAL_HITS):
        r = client.post(f"{PAT_BASE}/finals/record",
                        data={"pair_id": str(pair["pair_id"]), "hits": str(hits)})
        assert r.status_code in (200, 302), r.data[:400]

    state = _pat_state(sql)
    assert state.get("stage") == "completed", (
        f"the harness could not finish the bracket; stage is "
        f"{state.get('stage')!r}, so this test is not checking what it claims")
    return state


@pytest.mark.sev3
def test_a_published_axe_champion_keeps_his_score_when_a_prelim_is_re_recorded(
        client, sql):
    """The event is over and published. One prelim entry rewrites the winner.

    record_prelim_result has no stage guard, and it ends by calling
    _sync_prelim_to_event_results, which writes pair['prelim_score'] straight
    over EventResult.result_value. Those rows already hold the FINALS score
    and the placing, written by _save_event_results when the bracket finished.
    final_position is left alone, so the row does not revert cleanly to a
    prelim row either: it comes out a hybrid, first place holding whatever
    number was just typed into the prelim box.

    Measured on the real event 40 roster: Trevor Baker and Kate Page finish
    first on 15 finals hits, a prelim re-record of 3 leaves them at position 1
    with 3.00, below every pair they beat, while event_state still says 15.
    """
    state = _build_pat_complete(client, sql)
    winner = state["final_results"][0]
    members = [winner["competitor1"]["id"], winner["competitor2"]["id"]]
    runner_up = state["final_results"][1]
    runners = [runner_up["competitor1"]["id"], runner_up["competitor2"]["id"]]

    before = _er(sql)
    assert all(before[c] == (PAT_FINAL_HITS[0], 1, "completed") for c in members), (
        f"the bracket did not publish the winner the way this test assumes: "
        f"{ {c: before.get(c) for c in members} }")

    r = client.post(f"{PAT_BASE}/prelims/record",
                    data={"pair_id": str(winner["pair_id"]), "hits": "3"})
    assert r.status_code in (200, 302), r.data[:400]

    after = _er(sql)

    # Vacuity guard: the pairs nobody touched must be exactly where they were.
    # If these moved too, something other than this defect is rewriting rows
    # and the assertion below would be reading the wrong cause.
    assert all(after[c] == before[c] for c in runners), (
        f"the runner-up rows moved as well: "
        f"{ {c: (before.get(c), after.get(c)) for c in runners} }")

    for cid in members:
        assert after[cid] == before[cid], (
            f"competitor {cid} finished first on {before[cid][0]} finals hits "
            f"and the published row now reads {after[cid][0]} at position "
            f"{after[cid][1]}. A prelim entry rewrote a published final "
            f"result. event_state still says "
            f"{winner['final_score']}, so the results page and the scoring "
            f"page now disagree with nothing to say which is right.")


@pytest.mark.sev3
def test_a_prelim_recorded_after_the_cut_does_not_report_success(client, sql, flashes):
    """The operator has to be told the entry did not land.

    Same press as the test above, watched from the operator's side. Silence is
    not an answer either: somebody typed a score into a box and pressed a
    button at a live show, and if the page comes back clean they will believe
    the number is in.
    """
    state = _build_pat_complete(client, sql)
    winner = state["final_results"][0]
    flashes()

    r = client.post(f"{PAT_BASE}/prelims/record",
                    data={"pair_id": str(winner["pair_id"]), "hits": "3"})
    assert r.status_code in (200, 302), r.data[:400]
    said = flashes()

    _assert_refused_out_loud(
        said,
        "a prelim entry on a completed bracket (the pair has already placed, "
        "and this entry lands on a published result)")


@pytest.mark.sev3
def test_a_prelim_recorded_between_the_cut_and_the_finals_is_refused(
        client, sql, flashes):
    """Stage 'finals': cut made, no finals score in yet. Also has to refuse.

    This is the window a guard written as ``stage == 'completed'`` misses, and
    that is the obvious way to write this guard if you are only looking at the
    corruption in the test above. Nothing is published yet at this stage, so
    EventResult survives, but state['finalists'] was seeded from a snapshot of
    the prelim standings and does not move. Change a prelim score now and the
    standings the operator reads no longer agree with who is actually in the
    final, with no way to tell from either page which one is the record.

    Asserted on the standings, not on a flash category, so it is a check on
    the data and not on my wording.
    """
    _build_pat_prelims(client, sql)
    r = client.post(f"{PAT_BASE}/advance-to-finals")
    assert r.status_code in (200, 302), r.data[:400]

    before = _pat_state(sql)
    assert before.get("stage") == "finals", before.get("stage")
    finalist_ids = [p["pair_id"] for p in before["finalists"]]
    assert len(finalist_ids) == 4, finalist_ids

    # The pair that missed the cut. Give it the best prelim score in the event.
    missed = [p for p in before["pairs"] if p["pair_id"] not in finalist_ids]
    assert len(missed) == 1, missed
    flashes()

    r = client.post(f"{PAT_BASE}/prelims/record",
                    data={"pair_id": str(missed[0]["pair_id"]), "hits": "99"})
    assert r.status_code in (200, 302), r.data[:400]
    said = flashes()

    after = _pat_state(sql)
    top4 = [p["pair_id"] for p in after["prelim_results"][:4]]
    assert top4 == finalist_ids, (
        f"the prelim standings now put pairs {top4} in the top four while the "
        f"finals bracket holds {finalist_ids}. A prelim entry after the cut "
        f"moved the seeding record out from under a bracket that was already "
        f"seeded.")
    _assert_refused_out_loud(
        said, "a prelim entry after the cut but before the finals")


@pytest.mark.sev3
def test_the_service_refuses_a_late_prelim_when_called_directly(client, sql):
    """The guard has to sit in the service, not in the route.

    Not implementation-peeking: PartneredAxeThrow.record_prelim_result is a
    real entry point with callers outside the web layer (tests/
    test_partnered_axe_state.py and tests/test_axe_throw_qualifiers.py drive it
    directly, 18 sites between them), and a route-level guard leaves every one
    of those callers able to corrupt a published bracket. The HTTP tests above
    cannot tell the two placements apart, which is exactly why this one exists.

    Also pins the escape hatch: reset() rebuilds the stage from a literal, so
    it must clear the guard. A guard that cannot be cleared strands an event.
    """
    from services.partnered_axe import get_or_create_partnered_axe_throw

    _build_pat_complete(client, sql)

    pat = get_or_create_partnered_axe_throw(TID)
    assert pat.get_stage() == "completed", pat.get_stage()
    pair_id = pat.state["pairs"][0]["pair_id"]

    with pytest.raises(ValueError) as caught:
        pat.record_prelim_result(pair_id, 3)
    assert "completed" in str(caught.value), (
        f"the refusal does not tell the caller what stage blocked it: "
        f"{caught.value}")

    # reset() wipes the pairs along with the stage, so the call below still
    # raises. What matters is WHICH refusal: the stage is no longer what is
    # stopping it.
    pat.reset()
    assert pat.get_stage() == "prelims"
    with pytest.raises(ValueError) as after_reset:
        pat.record_prelim_result(pair_id, 12)
    assert "completed" not in str(after_reset.value), (
        f"reset() did not clear the stage guard, so a wrongly seeded bracket "
        f"has no way back: {after_reset.value}")


@pytest.mark.sev3
def test_a_prelim_score_for_a_pair_that_does_not_exist_is_refused(
        client, sql, flashes):
    """A mistyped pair number is announced as recorded and stored nowhere.

    record_prelim_result walks state['pairs'] looking for the id and simply
    falls out of the loop when it is not there. recorded_pair stays None, the
    method returns without raising, and the route flashes
    'Prelim result recorded for Pair 999'. At a live show that is a score the
    judge believes is on the board.
    """
    _build_pat_prelims(client, sql)
    before_state = _pat_state(sql)
    before_rows = _er(sql)
    flashes()

    r = client.post(f"{PAT_BASE}/prelims/record",
                    data={"pair_id": "999", "hits": "17"})
    assert r.status_code in (200, 302), r.data[:400]
    said = flashes()

    # The no-op half is not the bug. Storing nothing is correct for a pair
    # that does not exist. Assert it so a fix cannot buy the flash by
    # inventing a pair.
    assert _pat_state(sql) == before_state, (
        "recording against pair 999 changed the event state")
    assert _er(sql) == before_rows, (
        "recording against pair 999 changed the published rows")

    _assert_refused_out_loud(
        said, "a prelim entry for pair 999, which does not exist")


@pytest.mark.sev3
def test_a_finals_score_for_a_pair_that_does_not_exist_is_refused(
        client, sql, flashes):
    """Same door on the finals side, where the number decides the placings."""
    _build_pat_prelims(client, sql)
    r = client.post(f"{PAT_BASE}/advance-to-finals")
    assert r.status_code in (200, 302), r.data[:400]

    before_state = _pat_state(sql)
    assert len(before_state.get("finalists", [])) == 4
    flashes()

    r = client.post(f"{PAT_BASE}/finals/record",
                    data={"pair_id": "999", "hits": "19"})
    assert r.status_code in (200, 302), r.data[:400]
    said = flashes()

    assert _pat_state(sql) == before_state, (
        "recording a final against pair 999 changed the event state")

    _assert_refused_out_loud(
        said, "a finals entry for pair 999, which is not in the bracket")


@pytest.mark.sev3
def test_a_prelim_score_can_still_be_recorded_during_prelims(client, sql, flashes):
    """Positive control. The ordinary path, which is most of the event.

    Passes before the fix and has to pass after. A guard that refuses too much
    takes prelim scoring away entirely, which is worse than the defect.
    """
    for c1, c2 in PAT_PAIRS:
        r = client.post(f"{PAT_BASE}/register-pair",
                        data={"competitor1_id": str(c1), "competitor2_id": str(c2)})
        assert r.status_code in (200, 302), r.data[:400]
    pairs = _pat_state(sql).get("pairs", [])
    assert len(pairs) == len(PAT_PAIRS), pairs

    first = pairs[0]
    members = [first["competitor1"]["id"], first["competitor2"]["id"]]
    flashes()

    r = client.post(f"{PAT_BASE}/prelims/record",
                    data={"pair_id": str(first["pair_id"]), "hits": "20"})
    assert r.status_code in (200, 302), r.data[:400]
    said = flashes()

    scored = {p["pair_id"]: p["prelim_score"]
              for p in _pat_state(sql).get("pairs", [])}
    assert scored[first["pair_id"]] == 20, (
        f"an ordinary prelim entry did not land: {scored}")

    rows = _er(sql)
    for cid in members:
        assert rows[cid][0] == 20, (
            f"competitor {cid} did not get the prelim score on his "
            f"EventResult row: {rows[cid]}")

    refused = [(cat, msg) for cat, msg in said if cat in ("danger", "error")]
    assert not refused, (
        f"an ordinary prelim entry was refused: {refused}")


@pytest.mark.sev3
def test_a_prelim_score_can_still_be_corrected_before_the_cut(client, sql, flashes):
    """Positive control. Judges mis-hear hit counts; re-entry is routine.

    This is the reason the guard has to be on the STAGE and not on 'this pair
    already has a prelim score'. Both readings stop the defect. Only one of
    them leaves the operator able to fix a number before the bracket is cut.
    """
    state = _build_pat_prelims(client, sql)
    target = state["pairs"][0]
    members = [target["competitor1"]["id"], target["competitor2"]["id"]]
    assert target["prelim_score"] == PAT_PRELIM_HITS[0]
    flashes()

    r = client.post(f"{PAT_BASE}/prelims/record",
                    data={"pair_id": str(target["pair_id"]), "hits": "19"})
    assert r.status_code in (200, 302), r.data[:400]
    said = flashes()

    scored = {p["pair_id"]: p["prelim_score"]
              for p in _pat_state(sql).get("pairs", [])}
    assert scored[target["pair_id"]] == 19, (
        f"a prelim correction before the cut was dropped: {scored}")
    rows = _er(sql)
    for cid in members:
        assert rows[cid][0] == 19, (
            f"competitor {cid} kept the old prelim score on his EventResult "
            f"row after a correction: {rows[cid]}")

    refused = [(cat, msg) for cat, msg in said if cat in ("danger", "error")]
    assert not refused, (
        f"a prelim correction during prelims was refused: {refused}")


@pytest.mark.sev3
def test_a_finals_score_can_still_be_corrected_after_the_bracket_completes(
        client, sql, flashes):
    """Positive control, and it pins a deliberate asymmetry.

    The prelim writer must refuse once the bracket is cut. The FINALS writer
    must not: re-entering a finals score is the only in-app way to fix a
    mis-heard number on the deciding throw, and it already works correctly,
    re-sorting the placings and republishing every EventResult row. A guard
    written as 'refuse any score entry once stage is completed' would kill it
    and leave /reset, which wipes the pairs and the prelims too, as the only
    way back.
    """
    state = _build_pat_complete(client, sql)
    last = state["final_results"][-1]
    winner = state["final_results"][0]
    last_members = [last["competitor1"]["id"], last["competitor2"]["id"]]
    win_members = [winner["competitor1"]["id"], winner["competitor2"]["id"]]
    assert last["final_position"] == 4, last
    flashes()

    # The judge misheard the last pair: 22, not 9. That is now the best score
    # in the final, so the placings have to turn over.
    r = client.post(f"{PAT_BASE}/finals/record",
                    data={"pair_id": str(last["pair_id"]), "hits": "22"})
    assert r.status_code in (200, 302), r.data[:400]
    said = flashes()

    after = _pat_state(sql)
    placings = {p["pair_id"]: p["final_position"]
                for p in after.get("final_results", [])}
    assert placings.get(last["pair_id"]) == 1, (
        f"the corrected finals score did not re-rank the bracket: {placings}")
    assert placings.get(winner["pair_id"]) == 2, (
        f"the previous winner was not moved down: {placings}")

    rows = _er(sql)
    for cid in last_members:
        assert rows[cid] == (22, 1, "completed"), (
            f"competitor {cid} did not get the corrected finals score "
            f"published: {rows[cid]}")
    for cid in win_members:
        assert rows[cid][1] == 2, (
            f"competitor {cid} kept position 1 on his published row after "
            f"being beaten: {rows[cid]}")

    refused = [(cat, msg) for cat, msg in said if cat in ("danger", "error")]
    assert not refused, (
        f"a finals correction on a completed bracket was refused: {refused}")


# ---------------------------------------------------------------------------
# c21. A cascade scratch that empties a heat leaves it status='pending'.
#      The heats-page scratch of the same competitor marks it 'completed'.
#      services/scratch_cascade.py execute_cascade, heat loop ~461-505,
#      against routes/scheduling/heats.py scratch_competitor ~455-500.
#
#      An empty pending heat is not cosmetic. It is the only heat status the
#      operator has no way to clear: POST to an empty heat's enter page
#      redirects back to itself and leaves the status alone (measured). So it
#      pins all_heats_complete False forever, next_unscored_heat serves the
#      judge an empty stand, and next_incomplete_event never advances past the
#      event.
# ---------------------------------------------------------------------------

_C21_SOLO_EVENT = 12        # Standing Block Hard Hit, college F
_C21_SOLO_HEAT = 381        # holds exactly one competitor: college 29
_C21_SOLO_COMP = 29
_C21_PAIR_EVENT = 42        # Pole Climb, pro
_C21_PAIR_HEAT = 449        # [23, 31]
_C21_LIVE_HEAT = 450        # [41, 49]
_C21_SHIPPED_EMPTY = 392    # event 21, ships empty and pending on real data


def _c21_loads(value):
    return json.loads(value) if isinstance(value, str) else value


def _c21_heat(sql, heat_id):
    """(status, [competitor ids]) straight out of the heats row."""
    row = sql("SELECT status, competitors FROM heats WHERE id = :h",
              h=heat_id)[0]
    return row[0], (_c21_loads(row[1]) or [])


def _c21_scratch(client, competitor_id, competitor_type):
    """Drive the real preview -> confirm cascade a judge drives.

    competitor_type is always sent: college and pro ids collide on this data
    and the resolver aborts 400 rather than guess.
    """
    r = client.get(
        f"/scoring/{TID}/competitor/{competitor_id}/scratch-preview"
        f"?format=json&competitor_type={competitor_type}")
    body = r.get_json()
    assert body is not None, (
        f"scratch-preview for {competitor_type} {competitor_id} returned "
        f"{r.status_code} and no JSON: {r.data[:300]}")
    effects = body["effects"]
    form = {"effect_count": str(len(effects)),
            "competitor_type": competitor_type}
    for i, e in enumerate(effects):
        form[f"effect_type_{i}"] = e["effect_type"]
        form[f"affected_entity_id_{i}"] = str(e["affected_entity_id"])
        form[f"affected_entity_type_{i}"] = e["affected_entity_type"]
        form[f"effect_checked_{i}"] = "on"
    p = client.post(
        f"/scoring/{TID}/competitor/{competitor_id}/scratch-confirm", data=form)
    assert p.status_code in (200, 302), p.data[:400]
    return len(effects)


def _c21_undo(client, competitor_id, competitor_type):
    p = client.post(
        f"/scoring/{TID}/competitor/{competitor_id}/scratch-undo"
        f"?competitor_type={competitor_type}")
    assert p.status_code in (200, 302), p.data[:400]


def _c21_score_heat(client, sql, heat_id, seconds="30.00"):
    """Score every competitor in a heat through the real enter-heat POST."""
    _, ids = _c21_heat(sql, heat_id)
    ver = sql("SELECT version_id FROM heats WHERE id = :h", h=heat_id)[0][0]
    form = {"heat_version": str(ver)}
    for cid in ids:
        form[f"t1_run1_{cid}"] = seconds
        form[f"t2_run1_{cid}"] = seconds
        form[f"status_{cid}"] = "completed"
    r = client.post(f"/scoring/{TID}/heat/{heat_id}/enter", data=form)
    assert r.status_code in (200, 302), r.data[:400]
    return ids


@pytest.mark.sev3
def test_a_cascade_scratch_that_empties_a_heat_marks_it_complete(
        client, sql, flashes):
    """The defect, at its smallest. Heat 381 holds exactly one competitor.

    Closing the heat silently is only half a fix. The heats-page scratch of
    this same competitor tells the judge the heat is gone, and a heat that
    drops off the running order without a word is how someone stands at a
    stand waiting for a competitor who was scratched ten minutes ago.
    """
    before_status, before_ids = _c21_heat(sql, _C21_SOLO_HEAT)
    assert before_ids == [_C21_SOLO_COMP], (
        f"anchor moved: heat {_C21_SOLO_HEAT} holds {before_ids}, "
        f"expected exactly [{_C21_SOLO_COMP}]")
    assert before_status == "pending", before_status
    flashes()

    _c21_scratch(client, _C21_SOLO_COMP, "college")
    said = flashes()

    after_status, after_ids = _c21_heat(sql, _C21_SOLO_HEAT)
    # Vacuity guards: the scratch really happened, both in the heat and on the
    # competitor. Without these the status assertion could pass on a no-op.
    assert after_ids == [], (
        f"the cascade did not empty heat {_C21_SOLO_HEAT}: {after_ids}")
    assert sql("SELECT status FROM college_competitors WHERE id = :c",
               c=_C21_SOLO_COMP)[0][0] == "scratched"
    assert after_status == "completed", (
        f"heat {_C21_SOLO_HEAT} was emptied by a cascade scratch and left "
        f"status={after_status!r}. The heats-page scratch of the same "
        f"competitor marks it 'completed' and says so out loud.")
    # Asserted on the event the operator reads off the screen, not on my
    # wording. A cascade closes heats across several events at once, so a
    # bare heat number would not identify which board went quiet.
    event_name = sql("SELECT name FROM events WHERE id = :e",
                     e=_C21_SOLO_EVENT)[0][0]
    told = [msg for _, msg in said if "empty" in msg.lower()]
    assert told, (
        f"heat {_C21_SOLO_HEAT} was closed and the operator was told nothing "
        f"about it. Flashes: {said}")
    assert any(event_name in msg for msg in told), (
        f"the operator was told a heat closed but not which event it was in, "
        f"and this scratch closes heats in four different events. "
        f"Said: {told}")


@pytest.mark.sev3
def test_a_cascade_emptied_heat_is_not_offered_to_the_judge_as_next(client, sql):
    """next_unscored_heat filters on status='pending', so it serves the hole."""
    _c21_scratch(client, _C21_SOLO_COMP, "college")
    _, ids = _c21_heat(sql, _C21_SOLO_HEAT)
    assert ids == [], f"vacuity: heat {_C21_SOLO_HEAT} was not emptied: {ids}"

    r = client.get(f"/scoring/{TID}/event/{_C21_SOLO_EVENT}/next-heat")
    location = r.headers.get("Location") or ""
    assert f"/heat/{_C21_SOLO_HEAT}/" not in location, (
        f"next-heat for event {_C21_SOLO_EVENT} sends the judge to "
        f"{location}, which is the heat the cascade just emptied. There is "
        f"nobody at that stand and no POST that clears it.")


@pytest.mark.sev3
def test_an_event_finalizes_when_a_cascade_emptied_its_only_other_heat(
        client, sql):
    """The measured race-day harm: the event can never auto-finalize.

    Event 42 is two heats of two. Scratch both climbers out of heat 449, then
    score heat 450. Every competitor still in the event has now been scored,
    so the event must publish.
    """
    _, pair_ids = _c21_heat(sql, _C21_PAIR_HEAT)
    assert len(pair_ids) == 2, f"anchor moved: heat {_C21_PAIR_HEAT} {pair_ids}"
    for cid in pair_ids:
        _c21_scratch(client, cid, "pro")

    emptied_status, emptied_ids = _c21_heat(sql, _C21_PAIR_HEAT)
    assert emptied_ids == [], f"vacuity: heat {_C21_PAIR_HEAT} {emptied_ids}"

    scored_ids = _c21_score_heat(client, sql, _C21_LIVE_HEAT)
    live_status, _ = _c21_heat(sql, _C21_LIVE_HEAT)
    assert live_status == "completed", (
        f"vacuity: the surviving heat did not score: {live_status}")

    finalized, ev_status = sql(
        "SELECT is_finalized, status FROM events WHERE id = :e",
        e=_C21_PAIR_EVENT)[0]
    assert finalized, (
        f"event {_C21_PAIR_EVENT} did not finalize after every live heat was "
        f"scored. Heat {_C21_PAIR_HEAT} is empty and still "
        f"status={emptied_status!r}, so all_heats_complete "
        f"(services/scoring_workflow.py:414) is False forever.")

    placed = dict(sql(
        "SELECT competitor_id, final_position FROM event_results "
        "WHERE event_id = :e AND status = 'completed'", e=_C21_PAIR_EVENT))
    for cid in scored_ids:
        assert placed.get(cid) is not None, (
            f"competitor {cid} was scored and left unplaced: {placed}")


@pytest.mark.sev3
def test_an_undo_restores_the_heat_status_the_cascade_changed(client, sql):
    """Undo must put back everything the scratch touched, status included.

    Without this, an undo hands the competitor back into a heat marked
    'completed'. next_unscored_heat will never serve it, and all_heats_complete
    goes true with a competitor who has never been timed, which publishes the
    event with that competitor at position None.
    """
    before_status, before_ids = _c21_heat(sql, _C21_SOLO_HEAT)
    assert before_ids == [_C21_SOLO_COMP], before_ids

    _c21_scratch(client, _C21_SOLO_COMP, "college")
    _c21_undo(client, _C21_SOLO_COMP, "college")

    after_status, after_ids = _c21_heat(sql, _C21_SOLO_HEAT)
    assert after_ids == [_C21_SOLO_COMP], (
        f"vacuity: the undo did not put the competitor back: {after_ids}")
    assert sql("SELECT status FROM college_competitors WHERE id = :c",
               c=_C21_SOLO_COMP)[0][0] == "active"
    assert after_status == before_status, (
        f"the undo restored the competitor into heat {_C21_SOLO_HEAT} but "
        f"left it status={after_status!r} instead of {before_status!r}. "
        f"He is entered in a heat no judge will ever be sent to.")


@pytest.mark.sev3
def test_a_cascade_scratch_that_leaves_someone_behind_does_not_close_the_heat(
        client, sql):
    """Positive control. Only an EMPTY heat is complete."""
    _, pair_ids = _c21_heat(sql, _C21_PAIR_HEAT)
    assert len(pair_ids) == 2, pair_ids

    _c21_scratch(client, pair_ids[0], "pro")

    status, ids = _c21_heat(sql, _C21_PAIR_HEAT)
    assert ids == [pair_ids[1]], (
        f"vacuity: expected only {pair_ids[1]} left in heat "
        f"{_C21_PAIR_HEAT}, got {ids}")
    assert status == "pending", (
        f"heat {_C21_PAIR_HEAT} still holds competitor {pair_ids[1]}, who has "
        f"not climbed, and it was marked {status!r}.")


@pytest.mark.sev3
def test_a_heat_that_already_shipped_empty_is_not_touched_by_a_scratch(
        client, sql):
    """Positive control, and it pins the scope of this fix.

    Nineteen heats ship EMPTY and pending in the 2026 database, including two
    in event 21 and six in the Birling bracket, which scores through
    services/birling_bracket.py and not through these rows at all. Those are a
    separate defect with a separate blast radius. A scratch in another event
    must not reach across and close them.
    """
    before_status, before_ids = _c21_heat(sql, _C21_SHIPPED_EMPTY)
    assert before_ids == [], (
        f"anchor moved: heat {_C21_SHIPPED_EMPTY} is no longer empty")
    assert before_status == "pending", before_status

    _c21_scratch(client, _C21_SOLO_COMP, "college")

    after_status, _ = _c21_heat(sql, _C21_SHIPPED_EMPTY)
    assert after_status == "pending", (
        f"a scratch in event {_C21_SOLO_EVENT} changed heat "
        f"{_C21_SHIPPED_EMPTY} in event 21 to {after_status!r}")


@pytest.mark.sev3
def test_an_undo_does_not_reopen_a_heat_the_operator_scored_afterwards(
        client, sql):
    """Positive control on the undo, and it is the sharp one.

    Scratch one of a pair, score the survivor, then undo. The heat is
    'completed' because the JUDGE completed it, not because the cascade did.
    A restore that blindly writes the snapshotted status back would reopen a
    scored heat and throw away the completion the operator earned.
    """
    _, pair_ids = _c21_heat(sql, _C21_PAIR_HEAT)
    assert len(pair_ids) == 2, pair_ids
    scratched, survivor = pair_ids[0], pair_ids[1]

    _c21_scratch(client, scratched, "pro")
    _c21_score_heat(client, sql, _C21_PAIR_HEAT)
    scored_status, scored_ids = _c21_heat(sql, _C21_PAIR_HEAT)
    assert scored_status == "completed", (
        f"vacuity: the judge's own scoring did not complete heat "
        f"{_C21_PAIR_HEAT}: {scored_status}")
    assert scored_ids == [survivor], scored_ids

    _c21_undo(client, scratched, "pro")

    after_status, after_ids = _c21_heat(sql, _C21_PAIR_HEAT)
    assert scratched in after_ids, (
        f"vacuity: the undo did not restore competitor {scratched}: "
        f"{after_ids}")
    assert after_status == "completed", (
        f"the undo reopened heat {_C21_PAIR_HEAT} to {after_status!r}. The "
        f"judge completed that heat after the scratch and the undo threw it "
        f"away.")


# ---------------------------------------------------------------------------
# c22. Woodboss block counts and lottery cards disagree on a cross-gender entry
#
#      services/woodboss.py::_count_competitors routes a PRO enrollment by the
#      EVENT's gender.  services/woodboss.py::_list_competitors, which feeds
#      get_lottery_view, routes the same enrollment by the COMPETITOR's gender.
#      Two spellings of one rule.  They only disagree when a pro is entered in
#      a gendered pro event that is not their own gender, and the real 2026
#      data has exactly one such entry.
#
#      Kate Page (pro 2, F) is entered in event 32, Underhand MEN, and in
#      event 33, Underhand Women.  Both events are named "Underhand".
#
#      The block table orders a 13in Western White Pine for her men's entry
#      and an 11in for her women's entry.  The lottery card deck writes both
#      of her cards into the WOMEN's pile.  Net effect on block-turning day:
#      one 13in block turned with no name on it, and one 11in card in the
#      women's stack with no block behind it.
#
#      The totals balance (39 either way), which is why nobody caught it.
# ---------------------------------------------------------------------------

_C22_KATE = "Kate Page"
_C22_MEN = "block_underhand_pro_M"      # Western White Pine 13in
_C22_WOMEN = "block_underhand_pro_F"    # Western White Pine 11in

# Every block category on the shipped 2026 data whose count and card deck
# ALREADY agree before the fix.  These are the positive control: a fix that
# reroutes the lottery view must not move any of them.
_C22_UNTOUCHED = {
    "block_underhand_college_M": 29,
    "block_underhand_college_F": 26,
    "block_standing_college_M": 17,
    "block_standing_college_F": 7,
    "block_springboard_college_M": 0,
    "block_springboard_college_F": 0,
    "block_standing_pro_M": 0,
    "block_standing_pro_F": 10,
    "block_springboard_pro": 9,
    "block_1board_pro": 10,
    "block_3board_pro": 0,
    "block_relay_underhand": 3,
    "block_relay_standing": 3,
}


def _c22_blocks(app):
    """config_key -> number of blocks the wood order says to turn."""
    from services import woodboss
    with app.app_context():
        return {
            b["config_key"]: (b.get("competitor_count") or 0)
            for b in woodboss.calculate_blocks(TID)
        }


def _c22_cards(app):
    """config_key -> list of competitor names on the lottery note cards."""
    from services import woodboss
    with app.app_context():
        label_to_key = {v: k for k, v in woodboss.BLOCK_CONFIG_LABELS.items()}
        out = {}
        for column in woodboss.get_lottery_view(TID):
            for section in column["sections"]:
                key = label_to_key.get(
                    section["config_label"], section["config_label"])
                out.setdefault(key, []).extend(
                    c["name"] for c in section["competitors"])
        return out


@pytest.mark.sev3
def test_every_block_that_gets_turned_has_a_lottery_card_behind_it(app):
    """The wood order and the card deck must describe the same pile of wood.

    This is the whole defect in one assertion. calculate_blocks says how many
    blocks to turn per category; get_lottery_view says whose name goes on
    them. If those two numbers differ for any category, somebody stands at a
    block with no card or holds a card with no block.
    """
    blocks = _c22_blocks(app)
    cards = _c22_cards(app)

    mismatched = {
        key: (count, len(cards.get(key, [])))
        for key, count in blocks.items()
        if count != len(cards.get(key, []))
    }
    assert not mismatched, (
        "the block order and the lottery card deck disagree per category "
        f"(config_key: blocks vs cards): {mismatched}. Totals still balance, "
        f"blocks={sum(blocks.values())} cards="
        f"{sum(len(v) for v in cards.values())}, which is why this is "
        f"invisible on the summary line.")


@pytest.mark.sev3
def test_a_woman_entered_in_the_mens_underhand_gets_a_mens_block_card(app):
    """The wood is turned for the EVENT, not for the person.

    Event 32 is Underhand Men and it chops a 13in block. Anyone standing in
    event 32 chops a 13in block. Kate Page is entered in it, so the men's
    card deck owes her a card.
    """
    cards = _c22_cards(app)
    men = cards.get(_C22_MEN, [])
    assert _C22_KATE in men, (
        f"{_C22_KATE} is entered in event 32, Underhand Men, which turns a "
        f"13in block, and she has no card in the {_C22_MEN} deck. The block "
        f"gets turned for her either way: calculate_blocks counts her. "
        f"{len(men)} cards for {_c22_blocks(app).get(_C22_MEN)} blocks.")


@pytest.mark.sev3
def test_she_is_not_double_carded_in_the_womens_pile(app):
    """One card per enrollment, in the pile that entry actually belongs to.

    Both of her Underhand entries resolve to the event NAME "Underhand", so
    routing both by her own gender writes her name into the women's pile
    twice with nothing to tell the two cards apart.
    """
    cards = _c22_cards(app)
    women = cards.get(_C22_WOMEN, [])
    assert women.count(_C22_KATE) == 1, (
        f"{_C22_KATE} appears {women.count(_C22_KATE)} times in the "
        f"{_C22_WOMEN} deck (11in). She is entered in the women's Underhand "
        f"once. The duplicate is her MEN's entry, misrouted by her own "
        f"gender instead of the event's.")


@pytest.mark.sev3
def test_the_lottery_page_prints_her_under_the_mens_underhand_heading(client):
    """Not just the service. The page the block crew actually reads."""
    resp = client.get(f"/woodboss/{TID}/lottery")
    assert resp.status_code == 200, resp.status_code
    html = resp.get_data(as_text=True)

    men_head = "Underhand — Pro Men"
    women_head = "Underhand — Pro Women"
    assert men_head in html, f"{men_head!r} section missing from the page"

    men_block = html.split(men_head, 1)[1]
    # Cut at the next section heading so we only read the men's card list.
    for nxt in (women_head, "Underhand — College", "Standing Block — "):
        if nxt in men_block:
            men_block = men_block.split(nxt, 1)[0]
    assert _C22_KATE in men_block, (
        f"the printed lottery page has no {_C22_KATE} card under "
        f"{men_head!r}. That is the sheet the block crew works off.")


@pytest.mark.sev3
def test_no_other_block_category_moves(app):
    """Positive control.

    Thirteen of the fifteen categories already agree on the shipped data.
    A routing change must leave every one of them exactly where it was, in
    both the block order and the card deck. This is the test that fails if
    the fix "agrees" by breaking everything equally.
    """
    blocks = _c22_blocks(app)
    cards = _c22_cards(app)
    for key, expected in sorted(_C22_UNTOUCHED.items()):
        assert blocks.get(key, 0) == expected, (
            f"{key}: block order moved from {expected} to {blocks.get(key, 0)}")
        assert len(cards.get(key, [])) == expected, (
            f"{key}: card deck moved from {expected} to "
            f"{len(cards.get(key, []))}")


@pytest.mark.sev3
def test_the_underhand_pro_block_order_itself_is_unchanged(app):
    """Positive control on the two categories the fix DOES touch.

    calculate_blocks is the path that is already right. 26 men's blocks and
    13 women's is what the wood order says today and what it must still say
    after the lottery view is repaired. Only the cards move.
    """
    blocks = _c22_blocks(app)
    assert blocks.get(_C22_MEN) == 26, blocks.get(_C22_MEN)
    assert blocks.get(_C22_WOMEN) == 13, blocks.get(_C22_WOMEN)


@pytest.mark.sev3
def test_a_college_competitor_is_still_routed_by_her_own_gender(app):
    """Positive control on the branch that must NOT change.

    College competitors store event NAMES, and "Underhand Hard Hit" is the
    name of both the men's and the women's college event. The college branch
    therefore cannot know which event was meant and has to route by the
    competitor. That is a real limitation, not a second copy of this bug,
    and the fix must leave it alone.
    """
    from services import woodboss
    with app.app_context():
        counts = woodboss._count_competitors(TID)
    college_underhand = {
        k: v for k, v in counts.items()
        if k[1] == "college" and "underhand" in k[0]
    }
    assert college_underhand, "no college underhand enrollment counted at all"
    genders = {k[2] for k in college_underhand}
    # Both piles must survive.  A "consistency" fix that routes college by the
    # event's gender too collapses the women's college underhand to nothing,
    # because the stored NAME resolves to whichever of the two same-named
    # college events the lookup map happened to keep.  `<= {"M", "F"}` was too
    # loose to catch that: it passed with the F key gone entirely.
    assert genders == {"M", "F"}, (
        f"college underhand routing collapsed to {genders}. College must "
        f"route by the competitor: counts={dict(college_underhand)}")
    assert all(v > 0 for v in college_underhand.values()), \
        dict(college_underhand)


# ---------------------------------------------------------------------------
# c23. The stand-conflict danger banner on the schedule panel is unreachable.
#
#      services/schedule_status.py::_count_cookie_standing_simultaneous groups
#      heats by (flight_id, flight_position) and calls a group with both stand
#      types in it a conflict. flight_position is the 1-based ORDER of a heat
#      WITHIN its flight, so that key is unique by construction: no two heats
#      ever share it. Measured on the real 2026 flights, zero duplicate
#      (flight_id, flight_position) pairs exist. The function returns 0 for
#      every input and the danger banner has never rendered.
#
#      What the flight builder actually enforces is a GAP. Heats in a flight
#      run one after another, so two events that share physical stands are not
#      "simultaneous", they are a changeover with no break. flight_builder
#      declares _CONFLICTING_STANDS (cookie/standing, stock saw/hand saw/hot
#      saw, obstacle pole/speed climb) and _STAND_CONFLICT_GAP = 8, and
#      _calculate_heat_score returns -1 to block a placement inside that gap.
#      The block is not absolute: when every remaining candidate is blocked the
#      builder re-scores with the check disabled, and college spillover is
#      inserted afterwards by a code path that never consults the rule at all.
#
#      So violations reach the built schedule, and the panel that exists to
#      report them cannot. On the real 2026 flights there are 11, and the
#      status panel emits ZERO warnings of any kind.
# ---------------------------------------------------------------------------


def _flight_ordered_heats(sql):
    """The built show order: every flighted heat, in the order it runs."""
    return sql("""
        SELECT f.flight_number, h.flight_position, h.id, e.stand_type, e.name
          FROM heats h
          JOIN flights f ON f.id = h.flight_id
          JOIN events e ON e.id = h.event_id
         WHERE h.flight_id IS NOT NULL
         ORDER BY f.flight_number, h.flight_position, h.id
    """)


def _status(app):
    from models.tournament import Tournament
    from services.schedule_status import build_schedule_status
    with app.test_request_context():
        return build_schedule_status(Tournament.query.get(TID))


def _expected_conflict_count(sql):
    """Recompute the violations independently, from the builder's own rule.

    The panel's number is operator-facing: it is what a judge counts off
    against the run sheet. Tying it to a recomputation here rather than to a
    literal means the detector cannot quietly disagree, in either direction,
    with the rule it claims to be reporting on.
    """
    from services.flight_builder import (
        _CONFLICTING_STANDS,
        _STAND_CONFLICT_GAP,
    )

    last_pos: dict[str, int] = {}
    n = 0
    for pos, row in enumerate(_flight_ordered_heats(sql)):
        stand_type = row[3]
        for other in _CONFLICTING_STANDS.get(stand_type, ()):
            prev = last_pos.get(other)
            if prev is not None and (pos - prev) < _STAND_CONFLICT_GAP:
                n += 1
        if stand_type:
            last_pos[stand_type] = pos
    return n


def _stand_conflict_warnings(status):
    """Warnings that are about two events sharing physical stands.

    Matched on content, not on a title string, so the test does not pin the
    exact wording of a message a human will read.
    """
    out = []
    for w in status["warnings"]:
        blob = f"{w.get('title', '')} {w.get('detail', '')}".lower()
        if "stand" in blob and ("conflict" in blob or "share" in blob
                                or "shares" in blob):
            out.append(w)
    return out


@pytest.mark.sev3
def test_the_flight_position_key_the_detector_groups_on_never_collides(sql):
    """The premise the dead detector rests on, stated as data.

    If two heats could share (flight_id, flight_position) this test fails and
    the detector was merely wrong about the 2026 data rather than unreachable.
    """
    dupes = sql("""
        SELECT flight_id, flight_position, count(*)
          FROM heats
         WHERE flight_id IS NOT NULL
         GROUP BY flight_id, flight_position
        HAVING count(*) > 1
    """)
    assert dupes == [], (
        f"(flight_id, flight_position) is not unique after all: {dupes}. "
        f"The detector's grouping key would then be meaningful and this "
        f"whole analysis needs redoing.")


@pytest.mark.sev3
def test_the_built_2026_schedule_really_does_share_stands_between_events(sql):
    """The defect has to be worth reporting before the reporter is fixed.

    Walks the built show order with the builder's OWN rule table, imported
    rather than copied, and counts placements inside the conflict gap.
    """
    from services.flight_builder import (
        _CONFLICTING_STANDS,
        _STAND_CONFLICT_GAP,
    )

    rows = _flight_ordered_heats(sql)
    assert len(rows) > 50, f"only {len(rows)} flighted heats; wrong fixture?"

    last_pos: dict[str, int] = {}
    found = []
    for pos, row in enumerate(rows):
        stand_type = row[3]
        for other in _CONFLICTING_STANDS.get(stand_type, ()):
            prev = last_pos.get(other)
            if prev is not None and (pos - prev) < _STAND_CONFLICT_GAP:
                found.append((pos - prev, other, stand_type, row[2]))
        if stand_type:
            last_pos[stand_type] = pos

    assert found, (
        "the real 2026 flight order has no stand conflicts at all, so there "
        "is nothing for the panel to report and this cycle is pointless")
    # Measured 2026-07-30 against proam_prod_mirror_p0: 11 violations, the
    # tightest a gap of 1 (obstacle pole immediately followed by speed climb,
    # which share Pole 2).
    assert min(g for g, _, _, _ in found) == 1, sorted(found)
    assert len(found) >= 11, sorted(found)


@pytest.mark.sev3
def test_the_schedule_panel_tells_the_operator_the_stands_are_double_booked(app, sql):
    """The whole point. Eleven real conflicts, and the panel says nothing.

    Before the fix build_schedule_status returns an empty warnings list and
    overall_severity 'info', which reads as "no problems found".

    The count it prints must be the count the builder's own rule produces, so
    an off-by-one on the gap comparison cannot hide behind "it warned about
    something".
    """
    status = _status(app)
    hits = _stand_conflict_warnings(status)
    assert hits, (
        "the schedule status panel raised no stand-conflict warning at all. "
        f"It returned {len(status['warnings'])} warning(s) "
        f"({[w.get('title') for w in status['warnings']]}) and overall "
        f"severity {status['overall_severity']!r}, while the built flight "
        f"order double-books physical stands 11 times.")

    expected = _expected_conflict_count(sql)
    headline = " ".join(w.get("title", "") for w in hits)
    numbers = {int(n) for n in re.findall(r"\d+", headline)}
    assert expected in numbers, (
        f"the warning headline says {sorted(numbers)} but the builder's own "
        f"rule finds {expected} violations in the built order: {headline!r}")


@pytest.mark.sev3
def test_the_warning_names_the_poles_and_not_just_the_cookie_blocks(app):
    """Nine of the eleven are obstacle pole against speed climb, sharing Pole 2.

    The old query filtered stand_type to cookie_stack and standing_block, so
    even a working version of it would have missed every pole clash. The
    replacement must read the builder's rule table rather than a hardcoded
    pair.
    """
    hits = _stand_conflict_warnings(_status(app))
    assert hits, "no stand-conflict warning to inspect"
    blob = " ".join(f"{w.get('title', '')} {w.get('detail', '')}"
                    for w in hits).lower()
    assert "pole" in blob, (
        f"the stand-conflict warning never mentions the poles: {blob!r}")


@pytest.mark.sev3
def test_the_warning_does_not_offer_a_rebuild_that_does_not_clear_it(app):
    """Measured: rebuilding flights takes the count from 11 to 10.

    The old banner's call to action was a rebuild_flights POST and its detail
    line read "Rebuild flights to resolve". A remedy button that provably does
    not remedy is worse than no button, because the operator presses it,
    watches the seven-step progress bar, and gets the same red banner back.
    """
    hits = _stand_conflict_warnings(_status(app))
    assert hits, "no stand-conflict warning to inspect"
    for w in hits:
        assert w.get("submit_action") != "rebuild_flights", (
            f"stand-conflict warning still offers a rebuild: {w}")


@pytest.mark.sev3
def test_the_panel_does_not_go_permanently_red_over_a_changeover(app):
    """Severity has to be liveable.

    _overall turns the entire Current Schedule card red the moment any warning
    carries severity 'danger'. These eleven cannot be cleared by any button on
    the page, so 'danger' would mean the card is red from now until race day
    and every genuinely fatal condition after it reads the same. This is a
    crew-briefing item: warn, do not scream.
    """
    status = _status(app)
    hits = _stand_conflict_warnings(status)
    assert hits, "no stand-conflict warning to inspect"
    for w in hits:
        assert w.get("severity") == "warning", (
            f"stand-conflict warning severity is {w.get('severity')!r}: {w}")
    assert status["overall_severity"] != "danger", (
        f"the schedule card went red: {status['overall_label']!r}")


# --- positive controls -----------------------------------------------------


@pytest.mark.sev3
def test_the_show_order_is_not_touched(sql):
    """CONTROL. This cycle changes what the operator is TOLD, nothing else.

    Any fix that reorders heats to make the warning go away reshuffles the
    Saturday show, which is a decision for Alex and not for a detector fix.
    Snapshot is the exact shipped order, pinned by heat id.
    """
    rows = _flight_ordered_heats(sql)
    order = [(r[0], r[1], r[2]) for r in rows]
    assert len(order) == 75, f"{len(order)} flighted heats, expected 75"
    assert order[0] == (1, 1, 461), order[:3]
    assert order[8] == (1, 10, 380), order[6:10]
    assert order[43] == (5, 6, 449), order[41:45]
    assert order[55] == (6, 9, 376), order[53:57]
    assert order[-1] == (7, 20, 475), order[-3:]
    assert len({r[2] for r in rows}) == 75, "a heat id appears twice"


@pytest.mark.sev3
def test_the_other_schedule_warnings_are_left_alone(app):
    """CONTROL. Exactly one warning is added and nothing else changes.

    On the real 2026 data the panel currently raises none, so after the fix it
    must raise exactly one, and that one must be the stand-conflict warning.
    A fix that trips the college/pro "no heats yet" banners or the gear-sharing
    banner has broken something on its way past.
    """
    status = _status(app)
    others = [w for w in status["warnings"]
              if w not in _stand_conflict_warnings(status)]
    assert others == [], (
        f"unrelated warnings appeared: {[w.get('title') for w in others]}")


@pytest.mark.sev3
def test_a_tournament_with_no_flights_built_reports_nothing(app, sql):
    """CONTROL. The detector must be quiet before the flights exist.

    Un-flighting every heat is the state the page is in right after heats are
    generated and before Build Flights is pressed. A detector that fires there
    would tell the operator to fix a schedule that has not been built.
    """
    from database import db
    db.session.execute(db.text(
        "UPDATE heats SET flight_id = NULL, flight_position = NULL"))
    db.session.commit()
    status = _status(app)
    assert _stand_conflict_warnings(status) == [], (
        "stand-conflict warning fired with no flights built: "
        f"{_stand_conflict_warnings(status)}")


def _stage_gap(sql, gap):
    """Put exactly one conflicting pair `gap` slots apart, alone in one flight.

    Clears every flight assignment, then lays out: the earlier conflicting heat
    at index 0, `gap - 1` filler heats on stands that conflict with nothing, and
    the later conflicting heat at index `gap`. The detector walks rows in show
    order, so the index IS the position it measures on.

    Returns the two conflicting stand types, or None if the clone has no usable
    heats for the layout.
    """
    from database import db
    from services.flight_builder import _CONFLICTING_STANDS

    pair = sorted(
        (a, b) for a, others in _CONFLICTING_STANDS.items() for b in others
    )[0]
    inert = list(_CONFLICTING_STANDS)

    def _heats_on(stand_type, limit):
        return [r[0] for r in sql(
            "SELECT h.id FROM heats h JOIN events e ON e.id = h.event_id "
            "WHERE e.stand_type = :st ORDER BY h.id LIMIT :n",
            st=stand_type, n=limit)]

    first = _heats_on(pair[0], 1)
    second = _heats_on(pair[1], 1)
    fillers = [r[0] for r in sql(
        "SELECT h.id FROM heats h JOIN events e ON e.id = h.event_id "
        "WHERE e.stand_type IS NULL OR e.stand_type NOT IN :inert "
        "ORDER BY h.id LIMIT :n", inert=tuple(inert), n=gap - 1)]
    if not first or not second or len(fillers) < gap - 1:
        return None

    flight_id = sql("SELECT id FROM flights ORDER BY flight_number LIMIT 1")[0][0]
    db.session.execute(db.text(
        "UPDATE heats SET flight_id = NULL, flight_position = NULL"))
    placed = [first[0]] + fillers + [second[0]]
    for position, heat_id in enumerate(placed, start=1):
        db.session.execute(
            db.text("UPDATE heats SET flight_id = :f, flight_position = :p "
                    "WHERE id = :h"),
            {"f": flight_id, "p": position, "h": heat_id})
    db.session.commit()
    return pair


@pytest.mark.sev3
def test_a_pair_exactly_the_builders_gap_apart_is_not_reported(app, sql):
    """CONTROL, upper boundary. The builder ALLOWS a gap of exactly
    _STAND_CONFLICT_GAP: _calculate_heat_score blocks only when the distance is
    strictly less than it. A detector that reports at equality contradicts the
    rule it imports and cries wolf at a legal changeover.

    The 2026 data happens to contain no pair sitting exactly on the boundary,
    so this case is staged rather than found. Without it an off-by-one in the
    comparison is invisible.
    """
    from services.flight_builder import _STAND_CONFLICT_GAP

    pair = _stage_gap(sql, _STAND_CONFLICT_GAP)
    assert pair is not None, "clone has no heats to stage the boundary with"
    hits = _stand_conflict_warnings(_status(app))
    assert hits == [], (
        f"{pair[0]} and {pair[1]} exactly {_STAND_CONFLICT_GAP} slots apart is "
        f"legal by the builder's own rule, but the panel reported it: {hits}")


@pytest.mark.sev3
def test_a_pair_one_slot_inside_the_gap_is_reported_once(app, sql):
    """CONTROL, lower boundary. One slot tighter than the rule allows is a
    violation, and exactly one. Pins the other side of the comparison so the
    detector cannot buy the test above by simply reporting less.
    """
    from services.flight_builder import _STAND_CONFLICT_GAP

    pair = _stage_gap(sql, _STAND_CONFLICT_GAP - 1)
    assert pair is not None, "clone has no heats to stage the boundary with"
    hits = _stand_conflict_warnings(_status(app))
    assert len(hits) == 1, (
        f"{pair[0]} and {pair[1]} only {_STAND_CONFLICT_GAP - 1} slots apart "
        f"breaks the builder's rule, panel said: {hits}")
    assert "1 " in hits[0].get("title", ""), (
        f"one staged violation, headline says {hits[0].get('title')!r}")


# ---------------------------------------------------------------------------
# 25. Tournament clone drops the tournament's configuration
#     routes/main.py:615-724, clone_tournament
#
# The clone copies events, teams and competitors and nothing else. Measured on
# the real 2026 tournament: 16 hand-entered wood configs become 0, and the
# whole schedule_config (including a hand-ordered 29-event Friday running
# order) becomes NULL. The operator is told "Update the name and dates before
# use", which is the only instruction they get and is false.
#
# schedule_config stores SOURCE event ids in four keys. The clone builds new
# events with new ids, so a verbatim copy would drag dead ids into the copy.
# That is the same hazard clone_tournament already documents and guards for
# `payouts` on stateful events. The ids must be remapped, not copied.
# ---------------------------------------------------------------------------

CLONE_URL = f"/tournament/{TID}/clone"

# Every schedule_config key whose value is (or contains) event ids.
EVENT_ID_KEYS = (
    "friday_pro_event_ids",
    "saturday_college_event_ids",
    "friday_event_order",
    "saturday_event_order",
)


def _do_clone(client, sql):
    """POST the real clone route and return the new tournament id."""
    before = {r[0] for r in sql("SELECT id FROM tournaments")}
    resp = client.post(CLONE_URL, follow_redirects=False)
    assert resp.status_code == 302, f"clone POST returned {resp.status_code}"
    after = {r[0] for r in sql("SELECT id FROM tournaments")}
    new = sorted(after - before)
    assert len(new) == 1, f"clone created {len(new)} tournament(s): {new}"
    return new[0]


def _wood(sql, tid):
    return {
        r[0]: (r[1], r[2], r[3], r[4], r[5])
        for r in sql(
            "SELECT config_key, species, size_value, size_unit, notes, "
            "count_override FROM wood_configs WHERE tournament_id=:t", t=tid)
    }


def _cfg(sql, tid):
    raw = sql("SELECT schedule_config FROM tournaments WHERE id=:t", t=tid)[0][0]
    if raw is None:
        return None
    return json.loads(raw)


def _event_identity(sql, tid):
    """event id -> (name, gender, event_type) for one tournament."""
    return {
        r[0]: (r[1], r[2], r[3])
        for r in sql("SELECT id, name, gender, event_type FROM events "
                     "WHERE tournament_id=:t", t=tid)
    }


@pytest.mark.sev3
def test_the_source_tournament_really_does_carry_hand_entered_wood_configs(sql):
    """CONTROL. Stated as data, not as prose. If 2026 held no wood configs
    there would be nothing to lose and this cycle would be chasing a defect
    that does not exist on the operator's real tournament.
    """
    rows = _wood(sql, TID)
    assert len(rows) >= 10, f"source holds only {len(rows)} wood config(s)"
    specced = [k for k, v in rows.items() if v[0] and v[1]]
    assert len(specced) >= 10, (
        f"only {len(specced)} of {len(rows)} wood configs carry both a species "
        f"and a size, so little real work is at stake")


@pytest.mark.sev3
def test_the_source_tournament_really_does_carry_a_hand_ordered_schedule(sql):
    """CONTROL. Same purpose for the other half. The Friday running order is
    29 events long and every id in it belongs to this tournament, which is what
    makes a verbatim copy into a different tournament wrong.
    """
    cfg = _cfg(sql, TID)
    assert cfg, "source tournament has no schedule_config"
    order = cfg.get("friday_event_order") or []
    assert len(order) >= 20, f"Friday order holds only {len(order)} event(s)"
    own = set(_event_identity(sql, TID))
    assert set(order) <= own, (
        f"source order references ids outside its own events: "
        f"{sorted(set(order) - own)}")


@pytest.mark.sev3
def test_the_clone_carries_the_wood_configs(client, sql):
    """DEFECT. 16 rows of hand-entered species and dimensions, silently zero
    in the copy, with nothing on any page saying so.
    """
    src = _wood(sql, TID)
    new = _do_clone(client, sql)
    got = _wood(sql, new)
    assert got == src, (
        f"clone carried {len(got)} of the source's {len(src)} wood config(s); "
        f"missing {sorted(set(src) - set(got))}")


@pytest.mark.sev3
def test_the_clone_carries_the_schedule_settings(client, sql):
    """DEFECT. Flight sizing, placement mode and feature notes are operator
    decisions, not derived state, and none of them survive the clone.
    """
    src = _cfg(sql, TID)
    new = _do_clone(client, sql)
    got = _cfg(sql, new)
    assert got is not None, "clone's schedule_config is NULL"
    scalars = {k: v for k, v in src.items() if k not in EVENT_ID_KEYS}
    assert scalars, "source carries no non-id schedule settings to check"
    for key, want in scalars.items():
        assert got.get(key) == want, (
            f"schedule_config[{key!r}] is {got.get(key)!r} in the copy, "
            f"{want!r} in the source")


@pytest.mark.sev3
def test_the_cloned_running_order_points_at_the_clones_own_events(client, sql):
    """DEFECT, and the reason a verbatim copy is not the fix. Every event id
    stored in schedule_config must be remapped to the copy's own event, and the
    running order must survive as the same events in the same sequence.
    """
    src_cfg = _cfg(sql, TID)
    src_ident = _event_identity(sql, TID)
    new = _do_clone(client, sql)
    got = _cfg(sql, new) or {}
    new_ident = _event_identity(sql, new)

    for key in EVENT_ID_KEYS:
        if key not in src_cfg:
            continue
        ids = got.get(key)
        assert ids is not None, f"schedule_config[{key!r}] did not carry"
        stale = [i for i in ids if i not in new_ident]
        assert not stale, (
            f"schedule_config[{key!r}] carries id(s) {stale} that do not exist "
            f"in the cloned tournament")
        assert [new_ident[i] for i in ids] == [src_ident[i] for i in src_cfg[key]], (
            f"schedule_config[{key!r}] does not name the same events in the "
            f"same order as the source")


@pytest.mark.sev3
def test_the_clone_flash_does_not_say_the_name_and_dates_are_all_that_is_left(
        client, sql, flashes):
    """DEFECT. The flash is the operator's only instruction after a clone and
    it is the reason the silent drops stay silent.
    """
    _do_clone(client, sql)
    msgs = [m for _cat, m in flashes()]
    assert msgs, "clone raised no flash at all"
    text = " ".join(msgs)
    assert not re.search(r"update the name and dates before use", text, re.I), (
        f"flash still tells the operator the name and the dates are the whole "
        f"job: {text!r}")
    assert re.search(r"result|heat|entr", text, re.I), (
        f"flash does not say what did NOT carry: {text!r}")


@pytest.mark.sev3
def test_the_clone_still_copies_events_teams_and_competitors(client, sql):
    """CONTROL. The parts that already worked must keep working."""
    def counts(tid):
        return {
            t: sql(f"SELECT count(*) FROM {t} WHERE tournament_id=:x", x=tid)[0][0]
            for t in ("events", "teams", "college_competitors", "pro_competitors")
        }

    src = counts(TID)
    new = _do_clone(client, sql)
    assert counts(new) == src


@pytest.mark.sev3
def test_the_clone_still_carries_no_heats_results_or_entries(client, sql):
    """CONTROL. A clone is a template for next year, not a copy of the show.
    Carrying config must not start carrying the run itself.
    """
    new = _do_clone(client, sql)
    heats = sql("SELECT count(*) FROM heats h JOIN events e ON e.id=h.event_id "
                "WHERE e.tournament_id=:t", t=new)[0][0]
    flights = sql("SELECT count(*) FROM flights WHERE tournament_id=:t", t=new)[0][0]
    entered = sql("SELECT DISTINCT events_entered FROM pro_competitors "
                  "WHERE tournament_id=:t", t=new)
    assert heats == 0, f"clone carried {heats} heat(s)"
    assert flights == 0, f"clone carried {flights} flight(s)"
    assert [r[0] for r in entered] == ["[]"], (
        f"clone carried event entries: {[r[0] for r in entered][:3]}")


@pytest.mark.sev3
def test_the_clone_still_wipes_stateful_event_payouts(client, sql):
    """CONTROL. clone_tournament already resets `payouts` on Partnered Axe,
    Pro-Am Relay and birling because it holds source competitor ids. Carrying
    schedule_config must not weaken that guard.
    """
    new = _do_clone(client, sql)
    rows = sql("SELECT name, payouts FROM events WHERE tournament_id=:t AND "
               "(name IN ('Partnered Axe Throw', 'Pro-Am Relay') "
               " OR lower(coalesce(stand_type,''))='birling')", t=new)
    assert rows, "no stateful events in the clone to check"
    for name, payouts in rows:
        assert payouts in ("{}", None), f"{name} carried state: {payouts!r}"


@pytest.mark.sev3
def test_the_clone_does_not_modify_the_source_tournament(client, sql):
    """CONTROL. An id remap that rewrote the source's own config in place would
    destroy the live 2026 schedule while appearing to succeed.
    """
    before_cfg = _cfg(sql, TID)
    before_wood = _wood(sql, TID)
    _do_clone(client, sql)
    assert _cfg(sql, TID) == before_cfg, "clone rewrote the SOURCE schedule_config"
    assert _wood(sql, TID) == before_wood, "clone modified the SOURCE wood configs"


@pytest.mark.sev3
def test_a_tournament_with_nothing_configured_still_clones(client, sql):
    """CONTROL. A fresh tournament has no wood configs and a NULL
    schedule_config. Cloning one must not become a 500.
    """
    from database import db

    db.session.execute(db.text("DELETE FROM wood_configs WHERE tournament_id=:t"),
                       {"t": TID})
    db.session.execute(db.text("UPDATE tournaments SET schedule_config=NULL "
                               "WHERE id=:t"), {"t": TID})
    db.session.commit()
    new = _do_clone(client, sql)
    assert _wood(sql, new) == {}
    assert sql("SELECT count(*) FROM events WHERE tournament_id=:t", t=new)[0][0] > 0


@pytest.mark.sev3
def test_a_config_naming_a_deleted_event_does_not_leak_a_stale_id(client, sql):
    """CONTROL, and the boundary of the remap. An id that cannot be mapped is
    dropped, not carried and not turned into a null. Staged rather than found:
    the real 2026 config happens to reference only live events, so without this
    a remap that silently passed unknown ids through would be invisible.
    """
    from database import db

    cfg = _cfg(sql, TID) or {}
    real = list(cfg.get("friday_event_order") or [])
    assert len(real) >= 3, "source order too short to stage against"
    cfg["friday_event_order"] = [real[0], 999999, real[1]]
    db.session.execute(db.text("UPDATE tournaments SET schedule_config=:c "
                               "WHERE id=:t"), {"c": json.dumps(cfg), "t": TID})
    db.session.commit()

    new = _do_clone(client, sql)
    got = (_cfg(sql, new) or {}).get("friday_event_order")
    new_ident = _event_identity(sql, new)
    assert got is not None, "friday_event_order did not carry"
    assert 999999 not in got, f"stale source id survived the clone: {got}"
    assert None not in got, f"unmappable id became a null: {got}"
    assert len(got) == 2, f"expected the two mappable events, got {got}"
    src_ident = _event_identity(sql, TID)
    assert [new_ident[i] for i in got] == [src_ident[real[0]], src_ident[real[1]]]


@pytest.mark.sev3
def test_every_wood_config_column_carries_not_just_the_ones_2026_uses(client, sql):
    """CONTROL, staged. The real 2026 wood configs leave `notes` NULL on all 16
    rows and `size_unit` at its 'in' default on all 16, so a copy that silently
    dropped either field would be indistinguishable from a correct one on this
    data. Mutation M-e proved exactly that: omitting notes= from the copy
    survived the whole battery. Stage both fields so the columns are pinned by
    the test rather than by what the operator happened to type.
    """
    from database import db

    db.session.execute(db.text(
        "UPDATE wood_configs SET notes=:n, size_unit='mm', size_value=330 "
        "WHERE tournament_id=:t AND config_key=:k"),
        {"n": "green larch, cut Thursday", "t": TID, "k": "log_general"})
    db.session.commit()

    src = _wood(sql, TID)
    assert src["log_general"] == ("Western Larch", 330.0, "mm",
                                  "green larch, cut Thursday", None), src["log_general"]
    new = _do_clone(client, sql)
    got = _wood(sql, new)
    assert got == src, (
        f"log_general carried as {got.get('log_general')!r}, "
        f"source has {src['log_general']!r}")


# ---------------------------------------------------------------------------
# c26. The two bulk heat-assignment sweepers count every heat they WALK as a
#      heat they REPAIRED, and rewrite every row to do it.
#      services/schedule_generation.py:17-36 (preflight autofix)
#      routes/scheduling/heats.py:898-915   (per-event sync-fix)
#
# Neither one compares anything. Both delete every HeatAssignment row for
# every heat and reinsert it from the Heat.competitors JSON, then report the
# walk count as a repair count. On the real 2026 data zero heats are out of
# sync, so the operator is told 172 heats were synced while 379 rows are
# destroyed and recreated for no effect.
#
# The predicate they need already exists one function above the second site:
# heat_sync_check at routes/scheduling/heats.py:880 computes the mismatch set
# and does not act on it. And run_preflight_autofix already carries a comment
# saying no-op HeatAssignment writes "churn the DB for no effect", which is
# why it skips Pro-Am Relay, and only Pro-Am Relay.
# ---------------------------------------------------------------------------

BULK_SYNC_TARGET_EVENT = 43        # Cookie Stack, the event with the most rows


def _ha_rows(sql, tournament_id=TID):
    """row id -> (heat_id, competitor_id, competitor_type, stand_number)."""
    rows = sql(
        "SELECT ha.id, ha.heat_id, ha.competitor_id, ha.competitor_type, ha.stand_number "
        "  FROM heat_assignments ha "
        "  JOIN heats h ON h.id = ha.heat_id "
        "  JOIN events e ON e.id = h.event_id "
        " WHERE e.tournament_id = :t", t=tournament_id)
    return {r[0]: (r[1], r[2], r[3], r[4]) for r in rows}


def _heat_content(sql, tournament_id=TID):
    """heat id -> sorted list of (competitor_id, competitor_type, stand_number)."""
    out = {}
    for _rid, (hid, cid, ctype, stand) in _ha_rows(sql, tournament_id).items():
        out.setdefault(hid, []).append((cid, ctype, stand))
    return {hid: sorted(v) for hid, v in out.items()}


def _desynced_heats(sql, tournament_id=TID, event_id=None):
    """Heats whose HeatAssignment rows do not match the authoritative JSON.

    Pro-Am Relay is excluded because its heats are synthesized and the
    preflight sweeper deliberately skips them.
    """
    heats = sql(
        "SELECT h.id, h.competitors, h.stand_assignments, e.event_type, e.name "
        "  FROM heats h JOIN events e ON e.id = h.event_id "
        " WHERE e.tournament_id = :t", t=tournament_id)
    have = _heat_content(sql, tournament_id)
    out = []
    for hid, comps, stands, etype, ename in heats:
        if ename == 'Pro-Am Relay':
            continue
        if event_id is not None and hid not in _event_heat_ids(sql, event_id):
            continue
        sa = json.loads(stands or "{}")
        want = sorted((c, etype, sa.get(str(c))) for c in json.loads(comps or "[]"))
        if have.get(hid, []) != want:
            out.append((hid, ename))
    return out


def _event_heat_ids(sql, event_id):
    return {r[0] for r in sql("SELECT id FROM heats WHERE event_id = :e", e=event_id)}


def _autofix(client, flashes):
    r = client.post(f"/scheduling/{TID}/preflight", data={"action": "autofix"},
                    follow_redirects=False)
    assert r.status_code == 302, r.status_code
    msgs = [m for _cat, m in flashes()]
    assert msgs, "autofix flashed nothing"
    return msgs[0]


def _autofix_audit(sql):
    rows = sql("SELECT details_json FROM audit_logs "
               " WHERE action = 'preflight_autofix_applied' ORDER BY id DESC LIMIT 1")
    assert rows, "autofix wrote no audit row"
    raw = rows[0][0]
    return json.loads(raw) if isinstance(raw, str) else (raw or {})


@pytest.mark.sev3
def test_the_real_2026_heat_assignments_are_already_in_sync(sql):
    """CONTROL. Stated as data, not prose.

    Everything below turns on this: the operator's tournament is NOT desynced,
    so every heat the sweepers touch is a heat that needed nothing. If this
    ever goes non-empty the rest of this block is measuring the wrong thing.
    """
    total_heats = sql("SELECT count(*) FROM heats h JOIN events e ON e.id = h.event_id "
                      " WHERE e.tournament_id = :t", t=TID)[0][0]
    rows = _ha_rows(sql)
    assert total_heats > 100, total_heats
    assert len(rows) > 300, len(rows)
    assert _desynced_heats(sql) == [], _desynced_heats(sql)[:10]


@pytest.mark.sev3
def test_preflight_autofix_does_not_claim_to_have_repaired_correct_heats(client, sql, flashes):
    """DEFECT. Zero heats are out of sync. The flash says 172 were synced.

    The number is not merely wrong, it is a constant: it reads 172 whether the
    table is perfect or catastrophically broken, so it cannot tell the operator
    the one thing they are clicking the button to learn.
    """
    assert _desynced_heats(sql) == []
    msg = _autofix(client, flashes)
    assert "synced 172 heats" not in msg, msg
    assert re.search(r"repaired 0 of 17\d heat assignment sets", msg), msg


@pytest.mark.sev3
def test_preflight_autofix_leaves_already_correct_rows_untouched(client, sql, flashes):
    """DEFECT. 379 rows deleted and reinserted to change nothing.

    Row identity is the measurement: same count, same content, brand new ids.
    Race-day this is a full-table rewrite under the write lock, fired by a
    button the preflight page invites the operator to press.
    """
    before = _ha_rows(sql)
    assert len(before) > 300
    _autofix(client, flashes)
    after = _ha_rows(sql)
    survived = set(before) & set(after)
    assert len(survived) == len(before), (
        f"{len(before) - len(survived)} of {len(before)} rows were destroyed and "
        f"recreated by an autofix that changed nothing")
    assert after == before


@pytest.mark.sev3
def test_the_autofix_audit_row_does_not_record_repairs_that_did_not_happen(client, sql, flashes):
    """DEFECT. The false count is durable, not just a flash.

    log_action stores it, so the audit trail claims 172 repairs on a run that
    made none.
    """
    _autofix(client, flashes)
    details = _autofix_audit(sql)
    assert details.get("heats_fixed") == 0, details
    assert details.get("heats_checked", 0) > 100, details


@pytest.mark.sev3
def test_preflight_autofix_still_repairs_a_heat_whose_rows_were_deleted(client, sql, flashes):
    """CONTROL, staged. The sweeper must still do its job.

    A fix that reports honest numbers by never repairing anything would pass
    every test above this one.
    """
    from database import db

    hid = sorted(_event_heat_ids(sql, BULK_SYNC_TARGET_EVENT))[0]
    want = _heat_content(sql)[hid]
    assert want, hid
    db.session.execute(db.text("DELETE FROM heat_assignments WHERE heat_id = :h"), {"h": hid})
    db.session.commit()
    assert [h for h, _n in _desynced_heats(sql)] == [hid]

    msg = _autofix(client, flashes)
    assert _heat_content(sql).get(hid) == want, "the deleted rows were not rebuilt"
    assert re.search(r"repaired 1 of 17\d heat assignment sets", msg), msg
    assert _autofix_audit(sql).get("heats_fixed") == 1


@pytest.mark.sev3
def test_a_drifted_stand_number_counts_as_a_repair(client, sql, flashes):
    """CONTROL, staged. Stand number is half of what the sync writes.

    A comparison on competitor ids alone would call this heat in sync and walk
    away from a competitor standing at the wrong block.
    """
    from database import db

    hid = sorted(_event_heat_ids(sql, BULK_SYNC_TARGET_EVENT))[0]
    want = _heat_content(sql)[hid]
    db.session.execute(db.text(
        "UPDATE heat_assignments SET stand_number = 99 WHERE heat_id = :h"), {"h": hid})
    db.session.commit()
    assert [h for h, _n in _desynced_heats(sql)] == [hid]

    msg = _autofix(client, flashes)
    assert _heat_content(sql).get(hid) == want, _heat_content(sql).get(hid)
    assert re.search(r"repaired 1 of 17\d heat assignment sets", msg), msg


@pytest.mark.sev3
def test_a_duplicate_assignment_row_counts_as_a_repair(client, sql, flashes):
    """CONTROL, staged. Zero duplicate rows exist on the real 2026 data.

    Same shape as c25's M-e: a condition that never occurs in production
    cannot be tested by production data. A comparison on SETS would collapse
    the duplicate and report the heat in sync forever.
    """
    from database import db

    hid = sorted(_event_heat_ids(sql, BULK_SYNC_TARGET_EVENT))[0]
    want = _heat_content(sql)[hid]
    db.session.execute(db.text(
        "INSERT INTO heat_assignments (heat_id, competitor_id, competitor_type, stand_number) "
        "SELECT heat_id, competitor_id, competitor_type, stand_number "
        "  FROM heat_assignments WHERE heat_id = :h LIMIT 1"), {"h": hid})
    db.session.commit()
    assert [h for h, _n in _desynced_heats(sql)] == [hid]

    msg = _autofix(client, flashes)
    assert _heat_content(sql).get(hid) == want, _heat_content(sql).get(hid)
    assert re.search(r"repaired 1 of 17\d heat assignment sets", msg), msg


@pytest.mark.sev3
def test_a_wrong_competitor_type_counts_as_a_repair(client, sql, flashes):
    """CONTROL, staged. Every row on the real data already matches its event.

    competitor_type is the third column the sync writes, and it is what tells
    the reader whether competitor 42 is pro 42 or college 42. Those id spaces
    overlap, so a comparison that ignores this column leaves a row pointing at
    the wrong person and calls it clean.
    """
    from database import db

    hid = sorted(_event_heat_ids(sql, BULK_SYNC_TARGET_EVENT))[0]
    want = _heat_content(sql)[hid]
    assert {t for _c, t, _s in want} == {"pro"}, want
    db.session.execute(db.text(
        "UPDATE heat_assignments SET competitor_type = 'college' WHERE heat_id = :h"), {"h": hid})
    db.session.commit()
    assert [h for h, _n in _desynced_heats(sql)] == [hid]

    msg = _autofix(client, flashes)
    assert _heat_content(sql).get(hid) == want, _heat_content(sql).get(hid)
    assert re.search(r"repaired 1 of 17\d heat assignment sets", msg), msg


@pytest.mark.sev3
def test_the_empty_heats_are_not_counted_as_repairs(client, sql, flashes):
    """CONTROL. 19 heats ship with no competitors and no assignment rows.

    They are already consistent: nothing wanted, nothing stored. A sweeper
    that counted them would report repairs on heats it is not allowed to fix
    and would hide the c21 empty-heat finding behind a success message.
    """
    empty = sql(
        "SELECT h.id FROM heats h JOIN events e ON e.id = h.event_id "
        " WHERE e.tournament_id = :t AND NOT EXISTS "
        "   (SELECT 1 FROM heat_assignments ha WHERE ha.heat_id = h.id)", t=TID)
    assert len(empty) >= 19, len(empty)
    _autofix(client, flashes)
    assert _autofix_audit(sql).get("heats_fixed") == 0


@pytest.mark.sev3
def test_the_autofix_still_reports_its_other_summary_numbers(client, sql, flashes):
    """CONTROL. Only the heat clause is wrong. The rest of the sentence stays."""
    msg = _autofix(client, flashes)
    assert msg.startswith("Auto-fix complete:"), msg
    assert re.search(r"assigned \d+ pairs", msg), msg
    assert re.search(r"integrated \d+ spillover heats", msg), msg


@pytest.mark.sev3
def test_heat_sync_fix_does_not_claim_to_have_repaired_correct_heats(client, sql, flashes):
    """DEFECT. The per-event route has the same defect as the tournament sweep.

    Same inlined copy of the rebuild, same walk count reported as a repair
    count, sitting directly below a checker that already computes the answer.
    """
    ev = BULK_SYNC_TARGET_EVENT
    assert _desynced_heats(sql, event_id=ev) == []
    n = len(_event_heat_ids(sql, ev))
    r = client.post(f"/scheduling/{TID}/event/{ev}/heats/sync-fix", follow_redirects=False)
    assert r.status_code == 302, r.status_code
    msg = [m for _c, m in flashes()][0]
    assert f"synced for {n} heats" not in msg, msg
    assert re.search(rf"checked {n} heats: 0 repaired", msg), msg


@pytest.mark.sev3
def test_heat_sync_fix_leaves_already_correct_rows_untouched(client, sql, flashes):
    """DEFECT. Same full rewrite, scoped to one event."""
    ev = BULK_SYNC_TARGET_EVENT
    heat_ids = _event_heat_ids(sql, ev)
    before = {rid: v for rid, v in _ha_rows(sql).items() if v[0] in heat_ids}
    assert len(before) > 20, len(before)
    client.post(f"/scheduling/{TID}/event/{ev}/heats/sync-fix", follow_redirects=False)
    after = {rid: v for rid, v in _ha_rows(sql).items() if v[0] in heat_ids}
    assert set(before) & set(after) == set(before), (
        f"{len(set(before) - set(after))} of {len(before)} rows recreated for nothing")
    assert after == before


@pytest.mark.sev3
def test_heat_sync_fix_still_repairs_a_genuinely_broken_event(client, sql, flashes):
    """CONTROL, staged. The per-event route must still repair."""
    from database import db

    ev = BULK_SYNC_TARGET_EVENT
    hid = sorted(_event_heat_ids(sql, ev))[0]
    want = _heat_content(sql)[hid]
    db.session.execute(db.text("DELETE FROM heat_assignments WHERE heat_id = :h"), {"h": hid})
    db.session.commit()

    n = len(_event_heat_ids(sql, ev))
    client.post(f"/scheduling/{TID}/event/{ev}/heats/sync-fix", follow_redirects=False)
    msg = [m for _c, m in flashes()][0]
    assert _heat_content(sql).get(hid) == want, "the deleted rows were not rebuilt"
    assert re.search(rf"checked {n} heats: 1 repaired", msg), msg


@pytest.mark.sev3
def test_a_rebuilt_heat_keeps_its_rows_in_running_order(client, sql, flashes):
    """CONTROL, staged. The rebuild must insert in JSON order, not sorted order.

    The compare-first guard sorts, because sorting is how you compare two
    unordered row sets. Rebuilding FROM that sorted list is the easy mistake:
    it is right there, it is already computed, and on the real data it is
    usually identical because most heats are entered in ascending id order.
    It would silently reorder HeatAssignment rows and their autoincrement ids
    relative to the heat's running order, which nothing asked for. This heat
    is staged into descending order so sorted and JSON order cannot coincide.
    """
    from database import db

    ev = BULK_SYNC_TARGET_EVENT
    hid = sorted(_event_heat_ids(sql, ev))[0]
    comps = json.loads(sql("SELECT competitors FROM heats WHERE id = :h", h=hid)[0][0])
    assert len(comps) >= 2, comps
    reversed_order = sorted(comps, reverse=True)
    assert reversed_order != sorted(comps), "heat too small to distinguish the orders"

    db.session.execute(db.text("UPDATE heats SET competitors = :c WHERE id = :h"),
                       {"c": json.dumps(reversed_order), "h": hid})
    db.session.execute(db.text("DELETE FROM heat_assignments WHERE heat_id = :h"), {"h": hid})
    db.session.commit()

    client.post(f"/scheduling/{TID}/event/{ev}/heats/sync-fix", follow_redirects=False)

    rebuilt = [r[0] for r in sql(
        "SELECT competitor_id FROM heat_assignments WHERE heat_id = :h ORDER BY id", h=hid)]
    assert rebuilt == reversed_order, (
        f"rows were reinserted in {rebuilt}, JSON running order is {reversed_order}")


# ---------------------------------------------------------------------------
# 19. The snake-draft placement walk gives up before it has looked at every
#     heat, and a unit it cannot place is discarded without a word
#     services/heat_generator.py:648-694 — both placement loops are bounded by
#     `for _ in range(num_heats)`, i.e. by STEPS. _advance_snake_index BOUNCES
#     at both boundaries: from (num_heats-1, +1) it returns (num_heats-1, -1),
#     the same index. Every bounce burns one of the steps without examining a
#     new heat, so a walk that bounces examines fewer heats than exist and can
#     exhaust itself while a heat still has room. When the fallback loop runs
#     out that way, `placed` stays False, no gear violation is recorded, and
#     the unit is dropped on the floor.
# ---------------------------------------------------------------------------

DB_EVENT = 38                    # Men's Double Buck: 12 real pairs, 3 heats, 4 stands
DB_DROPPED = [39, 40]            # Mike Johnson + Owen Vredenburg, one pair


def _generate_heats(client, event_id):
    r = client.post(
        f"/scheduling/{TID}/event/{event_id}/generate-heats",
        data={"confirm": "true"},
        follow_redirects=False,
    )
    assert r.status_code in (302, 303), r.status_code
    return r


def _heat_rosters(sql, event_id):
    """Stored heat rosters for an event, in running order."""
    rows = sql(
        "SELECT competitors FROM heats WHERE event_id = :e "
        " ORDER BY heat_number, run_number", e=event_id)
    return [json.loads(c) if isinstance(c, str) else (c or []) for (c,) in rows]


@pytest.mark.sev3
def test_regenerating_double_buck_leaves_no_entrant_out_of_the_event(client, sql):
    """DEFECT. Two competitors vanish from the event on a plain regenerate.

    The 2026 rows ship with all 24 entrants placed. Press Regenerate on the
    heats page with the code as it stands and 22 come back. The pair that
    disappears is 39 + 40, whose gear map puts them in conflict with the only
    heat that still had a free stand by the time the walk reached them.
    """
    before = sorted(c for roster in _heat_rosters(sql, DB_EVENT) for c in roster)
    assert len(before) == 24, before
    assert all(c in before for c in DB_DROPPED), before

    _generate_heats(client, DB_EVENT)

    after = sorted(c for roster in _heat_rosters(sql, DB_EVENT) for c in roster)
    missing = sorted(set(before) - set(after))
    assert missing == [], f"regenerate dropped competitor(s) {missing} from event {DB_EVENT}"
    assert after == before


@pytest.mark.sev3
def test_regenerating_double_buck_fills_the_third_heat(client, sql):
    """DEFECT. 12 pairs over 3 heats of 4 stands is 4/4/4, not 4/4/3.

    The filed symptom was an unbalanced field. The balance is a shadow of the
    real event: the missing stand is missing because the pair that belonged on
    it was thrown away, not because the draft distributed badly.
    """
    _generate_heats(client, DB_EVENT)
    sizes = [len(r) for r in _heat_rosters(sql, DB_EVENT)]
    assert sizes == [8, 8, 8], sizes


@pytest.mark.sev3
def test_a_pair_that_cannot_dodge_a_gear_conflict_is_flagged_not_deleted(client, sql, flashes):
    """DEFECT. The fallback exists to place a unit ANYWAY and warn the judge.

    services/heat_generator.py already carries that machinery: the fallback
    records every forced gear-sharing conflict in gear_violations and the
    generate-heats route turns it into a WARNING flash. The operator never
    sees it, because the walk gives up before reaching the heat with room, so
    nothing is placed and nothing is recorded. Silence here reads as success.
    """
    _generate_heats(client, DB_EVENT)

    placed = [c for roster in _heat_rosters(sql, DB_EVENT) for c in roster]
    assert all(c in placed for c in DB_DROPPED), (
        f"{[c for c in DB_DROPPED if c not in placed]} still not in any heat")

    msgs = [m for _c, m in flashes()]
    assert any("gear-sharing conflict" in m for m in msgs), msgs


# --- CONTROLS: the snake draft itself must not move ------------------------
# _advance_snake_index's bounce is not a typo, it is what makes a snake draft
# a snake draft: 0,1,2,2,1,0,0,1,2. "Fixing" the bounce reshuffles every heat
# in the tournament. These four events are pinned to the placement the code
# produces today, in order, so any change to the walk ORDER fails here.

@pytest.mark.sev3
def test_the_underhand_snake_draft_is_unchanged(client, sql):
    """CONTROL. 25 pros, 5 heats of 5, one gear conflict dodged in the draft."""
    _generate_heats(client, 32)
    assert _heat_rosters(sql, 32) == [
        [1, 30, 32, 43, 44],
        [20, 28, 33, 41, 45],
        [21, 27, 34, 40, 47],
        [23, 26, 36, 39, 49],
        [24, 25, 37, 38, 48],
    ]


@pytest.mark.sev3
def test_the_cookie_stack_snake_draft_is_unchanged(client, sql):
    """CONTROL. Seven heats is the most bounces of any pro event on the card."""
    _generate_heats(client, 43)
    assert _heat_rosters(sql, 43) == [
        [1, 26, 27, 42, 43],
        [8, 24, 29, 40, 47],
        [14, 23, 30, 38, 48],
        [6, 25, 28, 44],
        [15, 22, 31, 37],
        [18, 21, 32, 36],
        [19, 20, 33, 34],
    ]


@pytest.mark.sev3
def test_the_single_buck_partial_heats_still_close_the_event(client, sql):
    """CONTROL. 22 pros, 6 heats: 4/4/4/4/3/3 with the short heats LAST.

    _move_partial_heats_to_end runs off the same stands_used bookkeeping the
    walk maintains, so a change to placement shows up here as a reordering as
    well as a rebalancing.
    """
    _generate_heats(client, 36)
    assert _heat_rosters(sql, 36) == [
        [3, 32, 35, 48],
        [20, 29, 36, 47],
        [23, 27, 39, 45],
        [24, 26, 40, 44],
        [1, 33, 34],
        [21, 28, 38],
    ]


@pytest.mark.sev3
def test_gear_conflict_avoidance_still_steers_the_small_events(client, sql):
    """CONTROL. Both of these regenerate differently with the conflict check
    disabled, so they pin the fact that the check still fires and still moves
    people. Pole Climb runs 2 heats of 2, Women's Single Buck 4 + 3."""
    _generate_heats(client, 42)
    assert _heat_rosters(sql, 42) == [[23, 41], [31, 49]]
    _generate_heats(client, 37)
    assert _heat_rosters(sql, 37) == [[5, 11, 13, 19], [8, 9, 15]]


# --- STAGED: the walk itself ----------------------------------------------
# Two scenarios driven straight through _generate_standard_heats with the gear
# predicate stubbed. Nothing about them is invented data: they are the two
# distinct ways a step-bounded walk loses a heat, and the second one cannot be
# reproduced from the 2026 card because it needs five heats and the biggest
# conflicted pro event has three.

def _synthetic(n):
    return [{"id": i, "name": f"C{i}"} for i in range(n)]


@pytest.mark.sev3
def test_a_unit_is_placed_while_any_heat_still_has_a_free_stand(monkeypatch):
    """DEFECT, staged. The event 38 shape with the data taken out of it.

    12 units, 3 heats, 4 stands. By the last unit the pointer sits on heat 0
    heading down, so the first pass bounces (h0, h0, h1) and the fallback
    bounces the other way (h2, h2, h1). Heat 0 has a free stand the whole
    time and neither walk ever looks at it again.
    """
    import services.heat_generator as hg

    safe = {0, 5, 6}
    monkeypatch.setattr(
        hg, "_has_gear_sharing_conflict",
        lambda comp, members, event: comp["id"] == 11
        and any(m["id"] in safe for m in members))

    violations = []
    heats = hg._generate_standard_heats(
        _synthetic(12), 3, 4, event=None, gear_violations=violations)

    placed = sorted(c["id"] for h in heats for c in h)
    assert placed == list(range(12)), f"unit(s) {sorted(set(range(12)) - set(placed))} discarded"
    assert sorted(len(h) for h in heats) == [4, 4, 4]
    assert [v["comp_id"] for v in violations] == [11], violations


@pytest.mark.sev3
def test_a_unit_is_not_forced_into_a_conflicting_heat_while_a_clean_one_has_room(monkeypatch):
    """DEFECT, staged. The half-fix that looks like it works.

    14 units, 5 heats, 4 stands. The last unit starts its walk on heat 3
    heading up, so the first pass spends its five steps on h3, h4, h4, h3, h2
    and never reaches h1 or h0. Heats 0 and 1 both have room; heat 1 conflicts
    and heat 0 does not.

    Repairing only the FALLBACK loop places this unit in heat 1 and records a
    gear-sharing conflict that did not have to happen, and it still clears
    every assertion that the real event 38 can make. The conflict-avoiding
    pass is the loop that has to be able to finish.
    """
    import services.heat_generator as hg

    safe = {0, 9, 10}
    monkeypatch.setattr(
        hg, "_has_gear_sharing_conflict",
        lambda comp, members, event: comp["id"] == 13
        and any(m["id"] not in safe for m in members))

    violations = []
    heats = hg._generate_standard_heats(
        _synthetic(14), 5, 4, event=None, gear_violations=violations)

    placed = sorted(c["id"] for h in heats for c in h)
    assert placed == list(range(14)), f"unit(s) {sorted(set(range(14)) - set(placed))} discarded"
    landed = next(sorted(c["id"] for c in h) for h in heats if any(c["id"] == 13 for c in h))
    assert landed == [0, 9, 10, 13], (
        f"unit 13 landed with {landed}; the conflict-free heat was {sorted(safe)}")
    assert violations == [], violations


# ---------------------------------------------------------------------------
# 20. A short heat opens the event instead of closing it whenever no heat
#     lands exactly on the cap
#     services/heat_generator.py:563 — _move_partial_heats_to_end splits the
#     heats into "full" (size >= max_per_heat) and "partial" and moves the
#     partials to the end. When NO heat reaches the cap, `full_idx` is empty,
#     the `if not partial_idx or not full_idx` guard fires, and the function
#     returns the snake-draft order untouched. Snake draft leaves the short
#     heat FIRST, so the event opens on the short heat, which is precisely the
#     convention the helper's own docstring says it exists to enforce:
#     "the leftover competitor or partial heat closes out the event rather
#     than starting it."
#
#     Measured on the 2026 card by instrumenting the helper and regenerating
#     every event: 36 of 37 events are correct. Events 9 and 10, both
#     Underhand Speed, both 11 entrants over a cap of 5, generate 3/4/4 and
#     take the all-partial no-op branch. Events 11 and 33 are structurally
#     identical events at the same cap with 13 entrants, generate 5/4/4, hit
#     the cap on one heat, and are reordered correctly. The difference is
#     entrant arithmetic, nothing else.
#
#     SEV4. Presentation order only. Conservation holds on both events
#     (lost=[] on the real POST), no score or fee is affected.
# ---------------------------------------------------------------------------


def _heat_sizes(sql, event_id):
    return [len(r) for r in _heat_rosters(sql, event_id)]


def _opens_short(sizes):
    """True when a LARGER heat runs after a SMALLER one."""
    return any(b > a for a, b in zip(sizes, sizes[1:]))


@pytest.mark.sev3
def test_mens_underhand_speed_does_not_open_on_the_short_heat(client, sql):
    """DEFECT. 11 cutters over 5 stands generates 3/4/4 and ships it that way.

    The event opens with a three-man heat and then runs two full ones. The
    rosters themselves are correct and nobody is lost; only the running order
    violates the rule. 13 cutters at the same cap would have produced 5/4/4
    and been reordered, because one heat would have touched the cap.
    """
    _generate_heats(client, 9)
    rosters = _heat_rosters(sql, 9)
    assert sorted(c for r in rosters for c in r) == sorted(
        [30, 42, 44, 32, 41, 58, 77, 38, 40, 61, 67]), rosters
    assert not _opens_short([len(r) for r in rosters]), (
        f"event 9 runs {[len(r) for r in rosters]}, short heat first")
    assert rosters == [
        [32, 41, 58, 77],
        [38, 40, 61, 67],
        [30, 42, 44],
    ]


@pytest.mark.sev3
def test_womens_underhand_speed_does_not_open_on_the_short_heat(client, sql):
    """DEFECT. The women's half of the same event, same shape, same cause."""
    _generate_heats(client, 10)
    rosters = _heat_rosters(sql, 10)
    assert sorted(c for r in rosters for c in r) == sorted(
        [34, 71, 72, 48, 70, 83, 92, 54, 65, 88, 91]), rosters
    assert not _opens_short([len(r) for r in rosters]), (
        f"event 10 runs {[len(r) for r in rosters]}, short heat first")
    assert rosters == [
        [48, 70, 83, 92],
        [54, 65, 88, 91],
        [34, 71, 72],
    ]


# --- STAGED: the helper itself ---------------------------------------------
# Driven straight through _move_partial_heats_to_end. The three-level shape
# below cannot be produced by the 2026 card, where every all-partial event is
# uniform (17, 18, 30, 35) except 9 and 10, which are two-level. It is the
# only thing that separates a correct descending sort from the obvious
# alternative fix of keeping the two-bucket split and moving the threshold
# from max_per_heat down to max(sizes).

@pytest.mark.sev3
def test_three_distinct_heat_sizes_all_under_the_cap_run_largest_first():
    """DEFECT, staged. 5/3/4 under a cap of 6 must run 5/4/3.

    Re-pointing the threshold at max(sizes) gives full=[0] and partial=[1,2]
    and returns 5/3/4 unchanged, which still leaves a 3 ahead of a 4. Only a
    full ordering by size fixes this shape.
    """
    import services.heat_generator as hg

    heats = [["a"], ["b"], ["c"]]
    out, mapping = hg._move_partial_heats_to_end(heats, [5, 3, 4], 6)
    assert out == [["a"], ["c"], ["b"]], out
    assert mapping == {0: 0, 1: 2, 2: 1}, mapping


@pytest.mark.sev3
def test_the_all_partial_reorder_reports_where_each_heat_moved():
    """DEFECT, staged. The mapping is the contract, not a convenience.

    Callers remap gear_violations off `old_to_new`. If the all-partial branch
    starts reordering while still returning the identity mapping, every judge
    warning recorded on the moved heat points at the wrong heat.
    """
    import services.heat_generator as hg

    heats = [["a"], ["b"], ["c"]]
    out, mapping = hg._move_partial_heats_to_end(heats, [3, 4, 4], 5)
    assert out == [["b"], ["c"], ["a"]], out
    assert mapping == {0: 2, 1: 0, 2: 1}, mapping

    violations = [{"heat_index": 0, "comp_id": 30}]
    hg._remap_violation_heat_indices(violations, mapping)
    assert violations[0]["heat_index"] == 2, violations


@pytest.mark.sev3
def test_an_over_capacity_heat_still_pins_the_whole_order():
    """CONTROL, staged. The springboard LH-overflow guard.

    A heat over the cap is deliberate overflow that must stay where the
    generator put it, at the end. A size-ordered reorder without this bail
    would move it to the FRONT, which is the exact inversion of the rule.
    """
    import services.heat_generator as hg

    heats = [["a"], ["b"], ["c"]]
    out, mapping = hg._move_partial_heats_to_end(heats, [4, 4, 6], 4)
    assert out == heats, out
    assert mapping == {0: 0, 1: 1, 2: 2}, mapping


@pytest.mark.sev3
def test_uniform_and_single_heat_shapes_are_left_alone():
    """CONTROL, staged. No reorder where there is nothing to order."""
    import services.heat_generator as hg

    for sizes in ([4], [3, 3, 3], [4, 4, 4], [2, 2]):
        heats = [[f"h{i}"] for i in range(len(sizes))]
        out, mapping = hg._move_partial_heats_to_end(heats, list(sizes), 4)
        assert out == heats, (sizes, out)
        assert mapping == {i: i for i in range(len(sizes))}, (sizes, mapping)


@pytest.mark.sev3
def test_the_cap_hitting_events_keep_their_shipped_order(client, sql):
    """CONTROL. Every event on the card where the reorder already fires.

    These are the shapes where a two-bucket partition and a full size ordering
    agree, so the fix must not move a single competitor in any of them. 7, 11
    and 33 are the two-level underhand and standing block fields, 43 is the
    seven-heat Cookie Stack, 20 and 25 carry a single leftover competitor.
    """
    _generate_heats(client, 7)
    assert _heat_rosters(sql, 7) == [
        [32, 50, 51, 80, 85], [33, 43, 59, 79, 86],
        [37, 42, 60, 78], [38, 39, 61, 74]]

    _generate_heats(client, 11)
    assert _heat_rosters(sql, 11) == [
        [35, 59, 66, 86, 87], [44, 58, 67, 85], [50, 51, 78, 79]]

    _generate_heats(client, 33)
    assert _heat_rosters(sql, 33) == [
        [16, 9, 11, 18, 19], [2, 8, 12, 17], [5, 7, 13, 15]]

    _generate_heats(client, 43)
    assert _heat_rosters(sql, 43) == [
        [1, 26, 27, 42, 43], [8, 24, 29, 40, 47], [14, 23, 30, 38, 48],
        [6, 25, 28, 44], [15, 22, 31, 37], [18, 21, 32, 36],
        [19, 20, 33, 34]]

    _generate_heats(client, 20)
    assert _heat_rosters(sql, 20) == [
        [35, 80], [37, 79], [38, 77], [41, 76], [43, 74],
        [44, 69], [46, 66], [51, 62], [58, 59], [30]]

    _generate_heats(client, 25)
    assert _heat_rosters(sql, 25) == [
        [31, 92], [55, 89], [57, 71], [29]]


@pytest.mark.sev3
def test_the_uniform_all_partial_events_are_untouched(client, sql):
    """CONTROL. The other four events that take the all-partial branch.

    17 and 18 are Double Buck, 30 is Springboard, 35 is Hot Saw. All four are
    uniform, so the branch becoming live must produce no change at all here.
    """
    for eid, expect in ((17, [3, 3, 3]), (18, [3, 3]),
                        (30, [3, 3, 3]), (35, [3, 3, 3])):
        _generate_heats(client, eid)
        rosters = _heat_rosters(sql, eid)
        sizes = [len(r) for r in rosters]
        assert not _opens_short(sizes), (eid, sizes)
        assert sizes == expect or sorted(sizes, reverse=True) == sizes, (
            eid, sizes)


@pytest.mark.sev3
def test_a_full_heat_does_not_stop_the_shorter_ones_being_ordered():
    """DEFECT, staged. 5/3/4 at a cap of 5, so heat 0 IS full.

    The narrow repair is to leave the full-vs-partial split alone and only
    reach for an ordering when no heat reaches the cap. That fixes events 9
    and 10 and still returns 5/3/4 here, because the split throws every heat
    under the cap into one undifferentiated bucket. The 2026 card has no
    three-level event, so nothing on it can tell the two apart.
    """
    import services.heat_generator as hg

    heats = [["a"], ["b"], ["c"]]
    out, mapping = hg._move_partial_heats_to_end(heats, [5, 3, 4], 5)
    assert out == [["a"], ["c"], ["b"]], out
    assert mapping == {0: 0, 1: 2, 2: 1}, mapping


@pytest.mark.sev3
def test_the_order_is_taken_from_the_capacity_sizes_not_the_roster_length():
    """DEFECT, staged. `sizes` is stand-units, len(heat) is people.

    For a partnered event a heat of 4 stands holds 8 competitors, so ordering
    on len(heat) is ordering on a different quantity that happens to agree
    everywhere on the 2026 card, where every partnered event is uniform. Here
    the two disagree: heat 0 holds five people in three stand-units, heat 1
    holds one person in four.
    """
    import services.heat_generator as hg

    heats = [["a", "b", "c", "d", "e"], ["f"]]
    out, mapping = hg._move_partial_heats_to_end(heats, [3, 4], 5)
    assert out == [["f"], ["a", "b", "c", "d", "e"]], out
    assert mapping == {0: 1, 1: 0}, mapping
