"""
Read-only diagnostic: classify every active college + pro competitor
against the Current Schedule panel's "Placed" metric. Prints the same
breakdown the V2.14.15 events.html panel will show once deployed.

Usage (from a machine with DATABASE_URL pointing at the target DB):

    # Against prod (via Railway SSH into the deployed container):
    railway ssh "python3 scripts/diagnose_unplaced_competitors.py"

    # Against local instance/proam.db:
    DATABASE_URL=sqlite:///instance/proam.db python scripts/diagnose_unplaced_competitors.py

Read-only. Does not modify any row.
"""

import json
import os
import re
import sys

from sqlalchemy import create_engine, text

LIST_ONLY = {"axethrow", "peaveylogroll", "cabertoss", "pulptoss"}
STATE_MACHINE_PRO = {"partneredaxethrow", "proamrelay"}


def norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())


def classify_event(ev_name: str, ev_type: str, scoring_type: str | None) -> str:
    n = norm(ev_name)
    if ev_type == "college" and n in LIST_ONLY:
        return "list_only"
    if scoring_type == "bracket":
        return "bracket"
    if ev_type == "pro" and n in STATE_MACHINE_PRO:
        return "state_machine"
    return "heat"


def main() -> int:
    db_url = os.environ.get("DATABASE_URL", "")
    if not db_url:
        print("NO DATABASE_URL in env")
        return 1
    db_url = db_url.replace("postgres://", "postgresql://")

    eng = create_engine(db_url)
    with eng.connect() as c:
        tours = c.execute(
            text("SELECT id, name, year FROM tournaments ORDER BY id DESC LIMIT 3")
        ).all()
        print("TOURNAMENTS (most recent):")
        for r in tours:
            print(f"  id={r[0]}  {r[1]}  year={r[2]}")
        if not tours:
            print("no tournaments found")
            return 0
        tid = tours[0][0]
        print(f"\nUsing tournament id={tid}\n")

        for ctype, ctable in (
            ("college", "college_competitors"),
            ("pro", "pro_competitors"),
        ):
            print(f"===== {ctype.upper()} =====")
            total = c.execute(
                text(
                    f"SELECT COUNT(*) FROM {ctable} WHERE tournament_id=:t AND status=:s"
                ),
                {"t": tid, "s": "active"},
            ).scalar()
            print(f"  total active competitors: {total}")

            evs = c.execute(
                text(
                    "SELECT id, name, scoring_type FROM events "
                    "WHERE tournament_id=:t AND event_type=:et ORDER BY name"
                ),
                {"t": tid, "et": ctype},
            ).all()
            print(f"  configured events: {len(evs)}")

            heat_event_ids = set()
            non_heat_event_ids = set()
            for r in evs:
                klass = classify_event(r[1], ctype, r[2])
                if klass == "heat":
                    heat_event_ids.add(r[0])
                else:
                    non_heat_event_ids.add(r[0])
            print(f"    heat events:     {len(heat_event_ids)}")
            print(
                f"    non-heat events: {len(non_heat_event_ids)} (list-only / bracket / state-machine)"
            )

            # Events without heats
            evs_with_heats = c.execute(
                text(
                    "SELECT DISTINCT h.event_id FROM heats h JOIN events e ON e.id=h.event_id "
                    "WHERE e.tournament_id=:t AND e.event_type=:et"
                ),
                {"t": tid, "et": ctype},
            ).all()
            with_heats = {r[0] for r in evs_with_heats}
            heat_events_missing_heats = heat_event_ids - with_heats
            if heat_events_missing_heats:
                print(
                    f"    !! HEAT EVENTS WITH NO HEATS: {len(heat_events_missing_heats)}"
                )
                for r in evs:
                    if r[0] in heat_events_missing_heats:
                        print(f"       ev{r[0]}  {r[1]}")

            # Competitors placed
            placed = set()
            for row in c.execute(
                text(
                    "SELECT h.competitors FROM heats h JOIN events e ON e.id=h.event_id "
                    "WHERE e.tournament_id=:t AND e.event_type=:et"
                ),
                {"t": tid, "et": ctype},
            ).all():
                try:
                    cids = (
                        json.loads(row[0])
                        if isinstance(row[0], str)
                        else (row[0] or [])
                    )
                except Exception:
                    cids = []
                for cid in cids:
                    try:
                        placed.add(int(cid))
                    except (TypeError, ValueError):
                        pass

            rows = c.execute(
                text(
                    f"SELECT id, name, events_entered FROM {ctable} "
                    f"WHERE tournament_id=:t AND status=:s ORDER BY name"
                ),
                {"t": tid, "s": "active"},
            ).all()

            name_to_klass = {norm(r[1]): classify_event(r[1], ctype, r[2]) for r in evs}

            in_heat = list_only_or_bracket = no_events = missing = 0
            missing_sample = []
            for r in rows:
                ee = r[2] or "[]"
                try:
                    lst = json.loads(ee) if isinstance(ee, str) else ee
                except Exception:
                    lst = []
                lst = lst or []

                if r[0] in placed:
                    in_heat += 1
                    continue
                if not lst:
                    no_events += 1
                    continue
                classes = {name_to_klass.get(norm(x), "unknown") for x in lst}
                if "heat" in classes:
                    missing += 1
                    if len(missing_sample) < 15:
                        missing_sample.append((r[0], r[1], lst))
                else:
                    list_only_or_bracket += 1

            print("  BREAKDOWN:")
            print(f"    in at least one heat:               {in_heat}")
            print(f"    only list-only / bracket signup:    {list_only_or_bracket}")
            print(f"    no events entered:                  {no_events}")
            print(f"    *** entered heat event but missing: {missing}")
            if missing_sample:
                print("    MISSING FROM HEATS (sample):")
                for pid, pname, pevs in missing_sample:
                    print(f"       id={pid}  {pname[:35]:35s}  {json.dumps(pevs)}")
            print()

    return 0


if __name__ == "__main__":
    sys.exit(main())
