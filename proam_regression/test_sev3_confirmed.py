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
