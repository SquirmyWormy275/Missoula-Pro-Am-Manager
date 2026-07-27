"""
SEV1 regression tests: the four race-day-fatal bugs not covered by
test_sev1_race_day.py. Backlog items 4, 6, 7, and 9.

Same contract as every other file in this suite. Every test asserts CORRECT
behavior, so every test in here FAILS against v2026.final.

These four were carved out of the main SEV1 file because each one needs a
multi-step precondition built through real routes before the bug is reachable:
Partnered Axe Throw was never played in the app, no event was ever finalized,
and no relay total time was ever entered, all for the same reason. The 2026
show was run on paper.

INVENTED VALUES, exhaustively:
  item 4  Partnered Axe hit counts (flows.PAT_PRELIM_HITS, PAT_FINAL_HITS)
  item 7  three relay total times
  item 9  four Pole Climb stopwatch times and one payout structure

Everything else is production: the competitors, the pairings drawn from real
event entries, the relay teams the lottery actually drew in April, the heats
the scheduler actually built, and the payout template the operator saved.
"""

import json

import flows
import pytest
import rig

TID = rig.TOURNAMENT_ID

RELAY_EVENT = 44
PAT_EVENT = 40

# The payout template the operator actually saved in production.
MISSOULA_STANDARD_TEMPLATE_ID = 1


def _loads(value):
    return json.loads(value) if isinstance(value, str) else value


def _relay_state(sql):
    raw = sql("SELECT event_state FROM events WHERE id = :e", e=RELAY_EVENT)[0][0]
    return _loads(raw) or {}


# ---------------------------------------------------------------------------
# 4. The generic Finalize button re-ranks Partnered Axe by raw result_value
#    routes/scoring.py finalize_event -> scoring_engine.calculate_positions
# ---------------------------------------------------------------------------

@pytest.mark.sev1
def test_generic_finalize_does_not_promote_eliminated_axe_pairs(client, sql):
    """A pair knocked out in prelims must not finish ahead of a finalist.

    Partnered Axe Throw runs its own state machine: five pairs throw prelims,
    the top four advance, and the finals score decides the podium. The finals
    score is written over result_value on the same EventResult rows, so after
    the bracket completes the table holds finals scores for four pairs and a
    PRELIM score for the pair that was eliminated. Prelim hits and finals hits
    are different scales.

    The generic Finalize button is still rendered for this event. It sorts the
    whole table by raw result_value, which mixes the two scales, so a pair that
    lost in prelims can outscore a pair that made the final.
    """
    state = flows.build_partnered_axe_bracket(client, sql, complete_finals=True)
    assert state.get("stage") == "completed", state.get("stage")

    finalist_ids = {p["pair_id"] for p in state["finalists"]}
    by_pair = {}
    for pair in state["pairs"]:
        by_pair[pair["pair_id"]] = (
            pair["competitor1"]["id"], pair["competitor2"]["id"])

    eliminated = {pid: ids for pid, ids in by_pair.items()
                  if pid not in finalist_ids}
    assert eliminated, (
        "the bracket produced no eliminated pair, so this test cannot tell a "
        "correct ranking from a broken one")

    r = client.post(f"/scoring/{TID}/event/{PAT_EVENT}/finalize")
    assert r.status_code in (200, 302), r.data[:400]

    rows = sql("""
        SELECT competitor_id, competitor_name, result_value, final_position
        FROM event_results
        WHERE event_id = :e AND final_position IS NOT NULL
    """, e=PAT_EVENT)
    assert rows, "finalize assigned no positions at all"

    position = {cid: pos for cid, _n, _v, pos in rows}
    name = {cid: n for cid, n, _v, _p in rows}

    worst_finalist = max(
        (position[cid] for pid in finalist_ids for cid in by_pair[pid]
         if cid in position),
        default=None)
    assert worst_finalist is not None, "no finalist was given a position"

    promoted = []
    for pid, ids in eliminated.items():
        for cid in ids:
            pos = position.get(cid)
            if pos is not None and pos < worst_finalist:
                promoted.append((name.get(cid, cid), pos))

    assert not promoted, (
        f"pairs eliminated in prelims were ranked ahead of pairs that reached "
        f"the final: {promoted}. The worst finalist placed {worst_finalist}. "
        f"Finalize sorted the whole table by raw result_value, which holds "
        f"finals hits for the four finalists and PRELIM hits for the pair that "
        f"was knocked out. Two different scales, one sort."
    )


# ---------------------------------------------------------------------------
# 6. A failed relay redraw destroys the drawn teams before validating
#    services/proam_relay.py redraw_lottery saves the wipe, then runs lottery
# ---------------------------------------------------------------------------

@pytest.mark.sev1
def test_failed_relay_redraw_leaves_the_announced_teams_intact(client, sql):
    """A redraw that cannot succeed must not delete what is already drawn.

    redraw_lottery blanks relay_data and calls _save_relay_data(), which
    commits, and only then calls run_lottery, which raises ValueError when a
    gender bucket is short. The route catches the ValueError and flashes it.
    The teams are already gone by then, and the shuffle is unseeded, so the
    exact rosters that were announced cannot be reproduced.

    This runs against the three teams the lottery actually drew for the 2026
    show, which are still sitting in the production mirror.
    """
    before = _relay_state(sql)
    teams_before = before.get("teams", [])
    assert len(teams_before) >= 1, (
        "the mirror holds no drawn relay teams, so this test cannot tell a "
        "survived roster from an empty one")

    # Qualify each id by division. Pro and college ids come from separate
    # tables and do collide, so a bare id list would fingerprint two different
    # people as the same roster slot.
    def _roster(teams):
        return {
            t["team_number"]: sorted(
                [("pro", m["id"]) for m in t["pro_members"]]
                + [("college", m["id"]) for m in t["college_members"]])
            for t in teams
        }

    roster_before = _roster(teams_before)

    # Ask for a draw the eligible pool cannot possibly fill. 99 teams needs
    # 198 pro men; the tournament has 25 eligible pros in total.
    r = client.post(f"/tournament/{TID}/proam-relay/redraw",
                    data={"num_teams": "99"})
    assert r.status_code in (200, 302), r.data[:400]

    after = _relay_state(sql)
    teams_after = after.get("teams", [])
    roster_after = _roster(teams_after)

    assert roster_after == roster_before, (
        f"a redraw that failed validation destroyed the drawn teams. "
        f"Before: {len(teams_before)} team(s), status "
        f"{before.get('status')!r}. After: {len(teams_after)} team(s), status "
        f"{after.get('status')!r}. redraw_lottery commits the wipe before "
        f"run_lottery validates the pool, and the shuffle is unseeded, so the "
        f"announced rosters are unrecoverable."
    )


# ---------------------------------------------------------------------------
# 7. The public relay results page 500s as soon as a total time exists
#    templates/portal/relay_results.html:53 formats "%.2f" on None
# ---------------------------------------------------------------------------

RELAY_TOTAL_TIMES = {1: "412.50", 2: "398.10", 3: "430.75"}


@pytest.mark.sev1
def test_public_relay_results_page_survives_recorded_total_times(app, client, sql):
    """The spectator page must render once the announcer has a total time.

    record_total_time writes team['total_time'] and leaves every per-event
    result at None, which is the normal way this event is scored: one total
    per team, no splits. get_results() then returns those teams, and the
    template formats "%.2f" against four None per-event results.

    The page is public. Nobody has to be logged in to hit the 500.
    """
    # TESTING=True re-raises into the test client. Production returns a 500
    # page, so turn propagation off to observe what a spectator observes.
    app.config["PROPAGATE_EXCEPTIONS"] = False

    teams = _relay_state(sql).get("teams", [])
    assert teams, "the mirror holds no drawn relay teams"

    for team in teams:
        number = team["team_number"]
        seconds = RELAY_TOTAL_TIMES.get(number, "420.00")
        r = client.post(f"/tournament/{TID}/proam-relay/results",
                        data={"team_number": str(number),
                              "time_seconds": seconds})
        assert r.status_code in (200, 302), r.data[:400]

    recorded = [t.get("total_time") for t in _relay_state(sql).get("teams", [])]
    assert all(t is not None for t in recorded), (
        f"the harness failed to record total times: {recorded}")

    # No session. This is the public spectator view.
    anon = app.test_client()
    r = anon.get(f"/portal/spectator/{TID}/relay")

    assert r.status_code < 500, (
        f"the public relay results page returned {r.status_code} once total "
        f"times existed. record_total_time leaves every per-event result at "
        f"None, and templates/portal/relay_results.html formats '%.2f' "
        f"against those None values. The page worked all day right up until "
        f"the first team crossed the line."
    )


# ---------------------------------------------------------------------------
# 9. Bulk payout apply and clear leave paid money stale on finalized events
#    routes/scoring.py tournament_payout_manager, both branches skip the
#    is_finalized -> calculate_positions recalc that configure_payouts does
# ---------------------------------------------------------------------------

@pytest.mark.sev1
def test_bulk_payout_apply_recalculates_a_finalized_event(client, sql):
    """Applying a template to a finalized event must repay the results.

    configure_payouts does this correctly: it checks event.is_finalized and
    re-runs calculate_positions so payout_amount follows the new structure.
    The tournament-level bulk_apply branch writes event.payouts and stops.

    The settlement desk reads payout_amount. The payout manager screen reads
    event.payouts. After a bulk apply those two disagree, and nothing in the
    app says so.
    """
    paid_before = flows.finalize_pole_climb(client, sql)
    template = sql("SELECT payouts FROM payout_templates WHERE id = :t",
                   t=MISSOULA_STANDARD_TEMPLATE_ID)
    assert template, "the Missoula Standard payout template is missing"
    template_payouts = _loads(template[0][0]) or {}
    assert template_payouts, "the payout template holds no positions"
    # Guard against a vacuous pass: if the template happened to match the purse
    # already on the event, a stale payout_amount would look correct.
    assert {str(k): float(v) for k, v in template_payouts.items()} != \
        {str(k): float(v) for k, v in flows.BIG_PURSE.items()}, (
        "the template matches the configured purse, so this test cannot tell a "
        "recalculation from a no-op")

    r = client.post(f"/scoring/{TID}/pro/payout-manager",
                    data={"action": "bulk_apply",
                          "template_id": str(MISSOULA_STANDARD_TEMPLATE_ID),
                          "event_ids": [str(flows.POLE_CLIMB_EVENT)]})
    assert r.status_code in (200, 302), r.data[:400]

    stored = _loads(sql("SELECT payouts FROM events WHERE id = :e",
                        e=flows.POLE_CLIMB_EVENT)[0][0]) or {}
    assert stored == template_payouts, (
        f"the harness did not actually apply the template: {stored}")

    rows = sql("""
        SELECT competitor_id, final_position, payout_amount
        FROM event_results WHERE event_id = :e ORDER BY final_position
    """, e=flows.POLE_CLIMB_EVENT)
    actual = {pos: float(amount) for _c, pos, amount in rows}
    expected = {int(k): float(v) for k, v in template_payouts.items()
                if int(k) in actual}

    assert actual == expected, (
        f"a bulk template apply left the finalized results paying the old "
        f"purse. Positions now owe {actual}, the applied template says "
        f"{expected}. Before the apply the rows paid {paid_before}. "
        f"configure_payouts re-runs calculate_positions when is_finalized is "
        f"true; the bulk_apply branch does not."
    )


@pytest.mark.sev1
def test_clearing_payouts_on_a_finalized_event_clears_the_money(client, sql):
    """Clearing an event's payouts must stop it owing money.

    clear_event calls set_payouts({}) and commits. Every EventResult keeps its
    payout_amount and every ProCompetitor keeps that money in total_earnings,
    so the payout manager shows a $0 purse while the results page and the
    competitor earnings totals still owe the full purse.
    """
    paid_before = flows.finalize_pole_climb(client, sql)
    purse = sum(paid_before.values())
    assert purse > 0, "the harness finalized without paying anything"

    earnings_before = dict(sql("""
        SELECT id, total_earnings FROM pro_competitors WHERE id IN :ids
    """, ids=tuple(flows.POLE_CLIMB_COMPETITORS)))

    r = client.post(f"/scoring/{TID}/pro/payout-manager",
                    data={"action": "clear_event",
                          "event_id": str(flows.POLE_CLIMB_EVENT)})
    assert r.status_code in (200, 302), r.data[:400]

    stored = _loads(sql("SELECT payouts FROM events WHERE id = :e",
                        e=flows.POLE_CLIMB_EVENT)[0][0]) or {}
    assert not stored, f"the harness did not actually clear the payouts: {stored}"

    still_owed = {cid: float(a) for cid, _p, a in sql("""
        SELECT competitor_id, final_position, payout_amount
        FROM event_results WHERE event_id = :e AND payout_amount > 0
    """, e=flows.POLE_CLIMB_EVENT)}

    earnings_after = dict(sql("""
        SELECT id, total_earnings FROM pro_competitors WHERE id IN :ids
    """, ids=tuple(flows.POLE_CLIMB_COMPETITORS)))
    unchanged = {cid: float(earnings_after[cid]) for cid in earnings_after
                 if float(earnings_after[cid]) == float(earnings_before[cid])
                 and float(earnings_before[cid]) > 0}

    assert not still_owed, (
        f"clearing the purse left ${sum(still_owed.values()):,.2f} owed on the "
        f"results rows: {still_owed}. Competitor total_earnings also did not "
        f"move for {sorted(unchanged)}. The payout manager now shows a $0 "
        f"purse for this event while the settlement desk is still holding the "
        f"old one."
    )
