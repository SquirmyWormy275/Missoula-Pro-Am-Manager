"""
Shared multi-step flows driven entirely through real routes.

These exist because two features in the app ship with EMPTY state in the
production mirror: Partnered Axe Throw was never played in the app, and no
event was ever finalized, because the 2026 show was run on paper. A test that
wants to observe finalize behavior has to reach that state first.

Everything here posts to real endpoints with real competitor ids. The only
invented values are times and hit counts, which no operator ever entered.
"""

import json

TID = 2

PAT_BASE = f"/tournament/{TID}/partnered-axe"

# Real pros, really entered in event 40 (Partnered Axe Throw).
PAT_PAIRS = [(1, 2), (4, 5), (8, 11), (12, 13), (14, 15)]
PAT_PRELIM_HITS = [20, 18, 16, 14, 12]
PAT_FINAL_HITS = [15, 13, 11, 9]


def _loads(value):
    return json.loads(value) if isinstance(value, str) else value


def pat_state(sql, event_id=40):
    raw = sql("SELECT event_state FROM events WHERE id = :e", e=event_id)[0][0]
    return _loads(raw) if raw else {}


def build_partnered_axe_bracket(client, sql, complete_finals=True):
    """Register pairs, score prelims, advance, and optionally score finals."""
    for c1, c2 in PAT_PAIRS:
        r = client.post(f"{PAT_BASE}/register-pair",
                        data={"competitor1_id": str(c1), "competitor2_id": str(c2)})
        assert r.status_code in (200, 302), r.data[:400]

    pairs = pat_state(sql).get("pairs", [])
    assert len(pairs) == len(PAT_PAIRS), f"registered {len(pairs)} pairs"

    for pair, hits in zip(pairs, PAT_PRELIM_HITS):
        r = client.post(f"{PAT_BASE}/prelims/record",
                        data={"pair_id": str(pair["pair_id"]), "hits": str(hits)})
        assert r.status_code in (200, 302), r.data[:400]

    r = client.post(f"{PAT_BASE}/advance-to-finals")
    assert r.status_code in (200, 302), r.data[:400]

    state = pat_state(sql)
    finalists = state.get("finalists", [])
    assert len(finalists) == 4, f"expected 4 finalists, got {finalists}"

    if complete_finals:
        for pair, hits in zip(finalists, PAT_FINAL_HITS):
            r = client.post(f"{PAT_BASE}/finals/record",
                            data={"pair_id": str(pair["pair_id"]), "hits": str(hits)})
            assert r.status_code in (200, 302), r.data[:400]

    return pat_state(sql)


def score_heat(client, sql, heat_id, times, dual_timer=True):
    """Enter times for one heat through the real heat-entry route."""
    version = sql("SELECT version_id FROM heats WHERE id = :h", h=heat_id)[0][0]
    data = {"heat_version": str(version)}
    for cid, t in times.items():
        data[f"t1_run1_{cid}"] = t
        if dual_timer:
            data[f"t2_run1_{cid}"] = t
        data[f"status_{cid}"] = "completed"
    r = client.post(f"/scoring/{TID}/heat/{heat_id}/enter", data=data)
    assert r.status_code in (200, 302), r.data[:400]


# Pole Climb (event 42) is the smallest pro event in the shipped 2026 data:
# two heats, four competitors, solo, timed. It is the cheapest real event that
# can be carried all the way to a finalized state with assigned positions and
# real payout_amount values, which is the precondition every payout test needs.
POLE_CLIMB_EVENT = 42
POLE_CLIMB_HEATS = {
    449: {23: "20.10", 31: "22.40"},   # Cameron Pilgreen, Henry Norwood
    450: {41: "18.75", 49: "25.30"},   # Quentin Lawrence, Cole Schlenker
}
POLE_CLIMB_COMPETITORS = (23, 31, 41, 49)

# The purse the settlement desk is told to pay. Positions 1 through 4, which is
# exactly the field size, so no row is left unpaid.
BIG_PURSE = {"1": 500.0, "2": 300.0, "3": 150.0, "4": 100.0}


def finalize_pole_climb(client, sql, payouts=None):
    """Score, configure a purse, and finalize event 42 through real routes.

    Returns {competitor_id: payout_amount} as stored after finalization.

    The times are invented. Nothing else is. No event in the 2026 mirror was
    ever finalized, because the show was run on paper, so no amount of reading
    production data can produce a finalized event. The times only have to be
    distinct; every assertion downstream is about money, not about seconds.
    """
    payouts = payouts or BIG_PURSE

    for heat_id, times in POLE_CLIMB_HEATS.items():
        score_heat(client, sql, heat_id, times)

    form = {f"payout_{k}": str(v) for k, v in payouts.items()}
    r = client.post(f"/scoring/{TID}/event/{POLE_CLIMB_EVENT}/payouts", data=form)
    assert r.status_code in (200, 302), r.data[:400]

    r = client.post(f"/scoring/{TID}/event/{POLE_CLIMB_EVENT}/finalize")
    assert r.status_code in (200, 302), r.data[:400]

    finalized = sql("SELECT is_finalized FROM events WHERE id = :e",
                    e=POLE_CLIMB_EVENT)[0][0]
    assert finalized, (
        "the harness could not finalize Pole Climb, so no payout test below "
        "can mean anything")

    rows = sql("""
        SELECT competitor_id, final_position, payout_amount
        FROM event_results WHERE event_id = :e ORDER BY final_position
    """, e=POLE_CLIMB_EVENT)
    assert [r[1] for r in rows] == [1, 2, 3, 4], (
        f"expected positions 1 through 4, got {rows}")

    paid = {cid: float(amount) for cid, _pos, amount in rows}
    assert sum(paid.values()) == sum(payouts.values()), (
        f"finalize did not pay the configured purse: {paid}")
    return paid
