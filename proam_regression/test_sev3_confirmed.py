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
