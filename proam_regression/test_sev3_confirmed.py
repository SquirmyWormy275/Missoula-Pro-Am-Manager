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
# real college competitor id in the same flight. Measured, not assumed.
SMS_MASKED_PROS = {29: "Dwight Severson", 37: "Karson Wilson"}

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
    """Dwight Severson and Karson Wilson are standing in flight 7.

    College competitors carry the same integer ids (Greer Swoboda is 29,
    Zach Cardenas is 37) and their heats are read last, so the type map
    relabels both pros as college and neither one is notified.
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
