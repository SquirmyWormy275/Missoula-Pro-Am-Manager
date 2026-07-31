"""
O2: reseed college competitor ids into a range disjoint from pro ids.

ProCompetitor and CollegeCompetitor are separate tables with independent
autoincrement keys over one integer namespace. On the 2026 production data
ids 29 through 49 name a real person in BOTH tables: 21 live collisions,
the root of the c29 SMS masking bug and the reason services/entity_key.py
exists. This script moves every college id (and every stored reference to
one) up by OFFSET, and pins the sequence above the new range, so a college
id and a pro id can never again be the same integer.

Usage:
    python scripts/reseed_college_ids.py --check   # measure, change nothing
    python scripts/reseed_college_ids.py --apply   # reseed, then re-check

DATABASE_URL selects the target. --apply runs in ONE transaction: any
failed post-check rolls the whole thing back.

Reference surfaces, measured on the production mirror (c38), not assumed:

    college_competitors.id            the rows themselves, plus the sequence
    event_results.competitor_id       WHERE competitor_type = 'college'
    heat_assignments.competitor_id    WHERE competitor_type = 'college'
    users.competitor_id               WHERE competitor_type = 'college'
    heats.competitors                 JSON id lists, college-event heats
    heats.stand_assignments           JSON {id: stand}, college-event heats
    events.payouts                    college bracket events: competitors[].id,
                                      seeding[], pre_seedings{}, placements{},
                                      every bracket match's competitor1/
                                      competitor2/winner/loser and falls[]
    events.event_state                Pro-Am Relay: eligible_college[].id,
                                      drawn_college[].id,
                                      teams[].college_members[].id

Deliberately NOT remapped: audit_logs and print/email logs. They are
append-only history; rewriting their entity ids would falsify the record.
This is documented behavior, not an oversight.
"""

import argparse
import json
import os
import sys

from sqlalchemy import create_engine, text

OFFSET = 100000


def _engine():
    url = os.environ.get("DATABASE_URL")
    if not url:
        sys.exit("DATABASE_URL is not set")
    return create_engine(url)


# ---------------------------------------------------------------------------
# JSON remappers, pure functions so they are unit-testable
# ---------------------------------------------------------------------------

def remap_heat_competitors(raw, mapping):
    ids = json.loads(raw or "[]")
    return json.dumps([mapping.get(int(c), int(c)) for c in ids])


def remap_stand_assignments(raw, mapping):
    stands = json.loads(raw or "{}")
    return json.dumps(
        {str(mapping.get(int(k), int(k))): v for k, v in stands.items()})


def remap_bracket_payouts(raw, mapping):
    d = json.loads(raw or "{}")

    def rid(v):
        return mapping.get(v, v) if isinstance(v, int) else v

    for comp in d.get("competitors") or []:
        comp["id"] = rid(comp.get("id"))
    if d.get("seeding"):
        d["seeding"] = [rid(x) for x in d["seeding"]]
    for key in ("pre_seedings", "placements"):
        if d.get(key):
            d[key] = {
                str(mapping.get(int(k), int(k))) if str(k).isdigit() else k: v
                for k, v in d[key].items()}

    def remap_match(m):
        if not isinstance(m, dict):
            return
        for f in ("competitor1", "competitor2", "winner", "loser",
                  "eliminated"):
            if f in m:
                m[f] = rid(m[f])
        if m.get("falls"):
            m["falls"] = [rid(x) for x in m["falls"]]

    bracket = d.get("bracket") or {}
    for side in ("winners", "losers"):
        for rnd in bracket.get(side) or []:
            for m in rnd:
                remap_match(m)
    for single in ("finals", "true_finals"):
        remap_match(bracket.get(single))
    return json.dumps(d)


def remap_relay_state(raw, mapping):
    d = json.loads(raw or "{}")
    for key in ("eligible_college", "drawn_college"):
        for entry in d.get(key) or []:
            if isinstance(entry, dict) and isinstance(entry.get("id"), int):
                entry["id"] = mapping.get(entry["id"], entry["id"])
    for team in d.get("teams") or []:
        for member in team.get("college_members") or []:
            if isinstance(member, dict) and isinstance(member.get("id"), int):
                member["id"] = mapping.get(member["id"], member["id"])
    return json.dumps(d)


# ---------------------------------------------------------------------------
# Checker: the invariants that define "the reseed is complete and safe"
# ---------------------------------------------------------------------------

def check(conn) -> dict:
    """Measure identity invariants. Returns a dict of named results; the
    caller decides which are defects. Read-only."""
    out = {}
    out["collisions"] = conn.execute(text(
        "SELECT count(*) FROM college_competitors c "
        "JOIN pro_competitors p ON p.id = c.id")).scalar()

    out["orphan_heat_ids"] = conn.execute(text("""
        SELECT count(*) FROM (
            SELECT jsonb_array_elements_text(h.competitors::jsonb)::int AS cid
            FROM heats h JOIN events e ON e.id = h.event_id
            WHERE e.event_type = 'college'
        ) x WHERE cid NOT IN (SELECT id FROM college_competitors)""")).scalar()

    out["orphan_stand_keys"] = conn.execute(text("""
        SELECT count(*) FROM (
            SELECT jsonb_object_keys(h.stand_assignments::jsonb)::int AS cid
            FROM heats h JOIN events e ON e.id = h.event_id
            WHERE e.event_type = 'college'
              AND h.stand_assignments IS NOT NULL
              AND h.stand_assignments != '{}'
        ) x WHERE cid NOT IN (SELECT id FROM college_competitors)""")).scalar()

    out["orphan_results"] = conn.execute(text(
        "SELECT count(*) FROM event_results r WHERE r.competitor_type = "
        "'college' AND r.competitor_id NOT IN "
        "(SELECT id FROM college_competitors)")).scalar()

    out["orphan_assignments"] = conn.execute(text(
        "SELECT count(*) FROM heat_assignments a WHERE a.competitor_type = "
        "'college' AND a.competitor_id NOT IN "
        "(SELECT id FROM college_competitors)")).scalar()

    out["orphan_users"] = conn.execute(text(
        "SELECT count(*) FROM users u WHERE u.competitor_type = 'college' "
        "AND u.competitor_id IS NOT NULL AND u.competitor_id NOT IN "
        "(SELECT id FROM college_competitors)")).scalar()

    # Bracket and relay JSON: walk in Python with the identity mapping; any
    # college id that fails to resolve is an orphan.
    valid = {r[0] for r in conn.execute(text(
        "SELECT id FROM college_competitors"))}
    orphan_json = 0
    for (payouts,) in conn.execute(text(
            "SELECT payouts FROM events WHERE event_type = 'college' "
            "AND scoring_type = 'bracket' AND payouts IS NOT NULL")):
        d = json.loads(payouts or "{}")
        ids = [c.get("id") for c in d.get("competitors") or []]
        ids += list(d.get("seeding") or [])
        orphan_json += sum(1 for i in ids
                           if isinstance(i, int) and i not in valid)
    out["orphan_bracket_ids"] = orphan_json

    orphan_relay = 0
    for (state,) in conn.execute(text(
            "SELECT event_state FROM events WHERE event_state IS NOT NULL")):
        d = json.loads(state or "{}")
        for key in ("eligible_college", "drawn_college"):
            for entry in d.get(key) or []:
                if isinstance(entry, dict) and entry.get("id") not in valid:
                    orphan_relay += 1
        for team in d.get("teams") or []:
            for m in team.get("college_members") or []:
                if isinstance(m, dict) and m.get("id") not in valid:
                    orphan_relay += 1
    out["orphan_relay_ids"] = orphan_relay

    out["max_college_id"] = conn.execute(text(
        "SELECT coalesce(max(id), 0) FROM college_competitors")).scalar()
    out["sequence_next"] = conn.execute(text(
        "SELECT last_value + 1 FROM college_competitors_id_seq")).scalar()
    return out


def print_report(r, label):
    print(f"--- {label} ---")
    for k, v in r.items():
        print(f"  {k:22s} {v}")


def apply_reseed(conn):
    mapping = {old: old + OFFSET for (old,) in conn.execute(text(
        "SELECT id FROM college_competitors"))}
    if not mapping:
        sys.exit("no college competitors found; wrong database?")
    if max(mapping) >= OFFSET:
        sys.exit(f"a college id is already >= {OFFSET}; refusing to double-reseed")

    conn.execute(text(
        f"UPDATE college_competitors SET id = id + {OFFSET}"))
    conn.execute(text(
        f"UPDATE event_results SET competitor_id = competitor_id + {OFFSET} "
        f"WHERE competitor_type = 'college'"))
    conn.execute(text(
        f"UPDATE heat_assignments SET competitor_id = competitor_id + {OFFSET} "
        f"WHERE competitor_type = 'college'"))
    conn.execute(text(
        f"UPDATE users SET competitor_id = competitor_id + {OFFSET} "
        f"WHERE competitor_type = 'college' AND competitor_id IS NOT NULL"))

    heats = conn.execute(text(
        "SELECT h.id, h.competitors, h.stand_assignments FROM heats h "
        "JOIN events e ON e.id = h.event_id "
        "WHERE e.event_type = 'college'")).fetchall()
    for hid, comps, stands in heats:
        conn.execute(
            text("UPDATE heats SET competitors = :c, stand_assignments = :s "
                 "WHERE id = :i"),
            {"c": remap_heat_competitors(comps, mapping),
             "s": remap_stand_assignments(stands, mapping), "i": hid})

    brackets = conn.execute(text(
        "SELECT id, payouts FROM events WHERE event_type = 'college' "
        "AND scoring_type = 'bracket' AND payouts IS NOT NULL")).fetchall()
    for eid, payouts in brackets:
        conn.execute(text("UPDATE events SET payouts = :p WHERE id = :i"),
                     {"p": remap_bracket_payouts(payouts, mapping), "i": eid})

    states = conn.execute(text(
        "SELECT id, event_state FROM events "
        "WHERE event_state IS NOT NULL")).fetchall()
    for eid, state in states:
        conn.execute(text("UPDATE events SET event_state = :p WHERE id = :i"),
                     {"p": remap_relay_state(state, mapping), "i": eid})

    new_max = max(mapping.values())
    conn.execute(text(
        f"SELECT setval('college_competitors_id_seq', {new_max})"))
    return mapping


def main():
    ap = argparse.ArgumentParser()
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    eng = _engine()
    if args.check:
        with eng.connect() as conn:
            print_report(check(conn), "check (read-only)")
        return

    with eng.begin() as conn:
        before = check(conn)
        print_report(before, "before")
        mapping = apply_reseed(conn)
        after = check(conn)
        print_report(after, "after")
        defects = []
        if after["collisions"] != 0:
            defects.append("pro/college collisions remain")
        # Reference invariants: everything that resolved before must resolve
        # after, and the reseed must not mint a single new orphan. Orphans
        # that PRE-EXIST are a different defect with a different owner: the
        # 2026 mirror carries first-era college ids (1-28, a roster that was
        # deleted and re-imported as 29+) inside the birling brackets and
        # the relay state. Those are O6 cleanup material, measured and
        # reported by --check; this migration preserves them bit-for-bit
        # rather than silently "fixing" history it cannot verify.
        for k in ("orphan_heat_ids", "orphan_stand_keys", "orphan_results",
                  "orphan_assignments", "orphan_users", "orphan_bracket_ids",
                  "orphan_relay_ids"):
            if after[k] != before[k]:
                defects.append(
                    f"{k} changed {before[k]} -> {after[k]}; the reseed "
                    f"must never create or destroy orphans")
        if after["sequence_next"] <= after["max_college_id"]:
            defects.append("sequence not pinned above the new id range")
        if defects:
            print("POST-CHECK FAILED, rolling back:")
            for d in defects:
                print("  -", d)
            raise SystemExit(1)
        print(f"reseeded {len(mapping)} college competitors by +{OFFSET}; "
              f"all post-checks passed")


if __name__ == "__main__":
    main()
