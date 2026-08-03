"""Stage the multi-tournament oracle template (tranche 2, c37).

Clones tournament 2 into a staged 2027 tournament inside
proam_prod_mirror_mt: new ids everywhere, FKs and JSON remapped
(heat rosters, stand keys, events_entered with its c22 mixed shapes,
partners, gear_sharing), heat_assignments rows cloned alongside the JSON
they mirror (c57), competitor names IDENTICAL on purpose, because
returning competitors are the name-collision hazard the oracle exists to
expose.

Rebuild after a container swap (see C32 recovery doc):

    su postgres -c "createdb proam_prod_mirror_mt -T proam_prod_mirror_p0 -O proam"
    PYTHONPATH=/tmp/proam_f .venv/bin/python proam_regression/stage_multitournament.py

Idempotence: none by design. Run it exactly once per fresh template; a
second run stages a second clone. The test module's oracle self-check
counts populations and will fail loudly on a double-staged template.
"""
import json
import os
import sys

sys.path.insert(0, "/tmp/proam_f")
os.environ["DATABASE_URL"] = os.environ.get(
    "PROAM_MT_URL", "postgresql://proam:proam@localhost:5432/proam_prod_mirror_mt")
os.environ.setdefault("SECRET_KEY", "stage-" + "x" * 58)

from app import create_app
from database import db
from services import reference_gate

SRC_T = 2

app = create_app()
with app.app_context():
    # c56: the write-time reference gate (c51) postdates this script (c37) and
    # the two have never run together. The gate refuses the Event clones below:
    # events.payouts is copied verbatim at a point where T3 has no competitors
    # yet, so every bracket reference in it resolves against the T2 pool and
    # reads as cross_kind. 16 findings on the first flush.
    #
    # Remapping the payouts would be the wrong repair. This template's entire
    # value is that it carries the real 2026 damage bit-for-bit; rewriting the
    # blobs would move the standing oracle numbers in
    # proam_regression/RUNBOOK.md and stop the oracle lane from being a
    # faithful copy. So stage with the gate disarmed, the same license
    # tests/conftest.py::reference_gate_disarmed grants the repair and audit
    # suites. This is a staging fixture, not a production path. Open question 17.
    reference_gate.uninstall(db.session)

    from models import Tournament
    from models.competitor import CollegeCompetitor, ProCompetitor
    from models.competitor_identity import Competitor
    from models.event import Event
    from models.heat import Flight, Heat, HeatAssignment
    from models.team import Team
    from models.wood_config import WoodConfig

    src = db.session.get(Tournament, SRC_T)
    t3 = Tournament(
        name="Missoula Pro Am 2027 (staged oracle)",
        year=2027,
        college_date=src.college_date,
        pro_date=src.pro_date,
        friday_feature_date=src.friday_feature_date,
        status=src.status,
        providing_shirts=src.providing_shirts,
        schedule_config=src.schedule_config,
    )
    db.session.add(t3)
    db.session.flush()
    T3 = t3.id
    print("T3 id:", T3)

    # Teams
    team_map = {}
    for tm in Team.query.filter_by(tournament_id=SRC_T).all():
        data = {c.name: getattr(tm, c.name) for c in Team.__table__.columns
                if c.name not in ("id", "tournament_id")}
        clone = Team(tournament_id=T3, **data)
        db.session.add(clone)
        db.session.flush()
        team_map[tm.id] = clone.id

    # Events (before competitors so entered-id remaps can use event_map)
    event_map = {}
    for ev in Event.query.filter_by(tournament_id=SRC_T).all():
        data = {c.name: getattr(ev, c.name) for c in Event.__table__.columns
                if c.name not in ("id", "tournament_id")}
        clone = Event(tournament_id=T3, **data)
        db.session.add(clone)
        db.session.flush()
        event_map[ev.id] = clone.id

    event_name_by_id = {ev.id: ev.name for ev in
                        Event.query.filter_by(tournament_id=SRC_T).all()}

    def remap_events_entered(raw):
        """events_entered mixes names (strings) and event ids (numbers), c22.
        Names stay. Numeric ids become the event's NAME rather than the
        remapped T3 id, deliberately: name-form entries are what real
        registration data carries (c22 measured 19 of them) and they are the
        form a tournament-blind name match can leak on, so the oracle must
        hold the adversarial shape, not the convenient one."""
        try:
            lst = json.loads(raw or "[]")
        except (TypeError, ValueError):
            return raw
        out = []
        for x in lst:
            if isinstance(x, (int, float)) and int(x) in event_name_by_id:
                out.append(event_name_by_id[int(x)])
            elif isinstance(x, str) and x.isdigit() and int(x) in event_name_by_id:
                out.append(event_name_by_id[int(x)])
            else:
                out.append(x)
        return json.dumps(out)

    def remap_keyed_json(raw):
        """partners / gear_sharing: {event_id_str: value}."""
        try:
            d = json.loads(raw or "{}")
        except (TypeError, ValueError):
            return raw
        out = {}
        for k, v in d.items():
            nk = k
            if str(k).isdigit() and int(k) in event_map:
                nk = str(event_map[int(k)])
            out[nk] = v
        return json.dumps(out)

    # Competitors, both kinds, identical names, new uids
    pro_map, col_map = {}, {}
    pro_uid, col_uid = {}, {}
    for comp in ProCompetitor.query.filter_by(tournament_id=SRC_T).all():
        ident = Competitor(kind="pro", tournament_id=T3)
        # Contact is no longer in ProCompetitor.__table__.columns, so the `data`
        # comprehension below cannot pick it up. Copy it onto the identity
        # directly or the staged tournament loses every phone number and the
        # oracle lane stops being a faithful copy.
        ident.address = comp.address
        ident.phone = comp.phone
        ident.email = comp.email
        ident.phone_opted_in = comp.phone_opted_in
        db.session.add(ident)
        db.session.flush()
        data = {c.name: getattr(comp, c.name) for c in ProCompetitor.__table__.columns
                if c.name not in ("id", "tournament_id", "uid")}
        data["events_entered"] = remap_events_entered(data.get("events_entered"))
        data["partners"] = remap_keyed_json(data.get("partners"))
        data["gear_sharing"] = remap_keyed_json(data.get("gear_sharing"))
        clone = ProCompetitor(tournament_id=T3, uid=ident.uid, **data)
        db.session.add(clone)
        db.session.flush()
        pro_map[comp.id] = clone.id
        pro_uid[comp.id] = ident.uid
    for comp in CollegeCompetitor.query.filter_by(tournament_id=SRC_T).all():
        ident = Competitor(kind="college", tournament_id=T3)
        ident.address = comp.address
        ident.phone = comp.phone
        ident.email = comp.email
        ident.phone_opted_in = comp.phone_opted_in
        db.session.add(ident)
        db.session.flush()
        data = {c.name: getattr(comp, c.name) for c in CollegeCompetitor.__table__.columns
                if c.name not in ("id", "tournament_id", "uid", "team_id")}
        data["events_entered"] = remap_events_entered(data.get("events_entered"))
        data["partners"] = remap_keyed_json(data.get("partners"))
        data["gear_sharing"] = remap_keyed_json(data.get("gear_sharing"))
        clone = CollegeCompetitor(tournament_id=T3, uid=ident.uid,
                                  team_id=team_map[comp.team_id], **data)
        db.session.add(clone)
        db.session.flush()
        col_map[comp.id] = clone.id
        col_uid[comp.id] = ident.uid

    # Wood configs
    for wc in WoodConfig.query.filter_by(tournament_id=SRC_T).all():
        data = {c.name: getattr(wc, c.name) for c in WoodConfig.__table__.columns
                if c.name not in ("id", "tournament_id")}
        db.session.add(WoodConfig(tournament_id=T3, **data))

    # Flights
    flight_map = {}
    for fl in Flight.query.filter_by(tournament_id=SRC_T).all():
        data = {c.name: getattr(fl, c.name) for c in Flight.__table__.columns
                if c.name not in ("id", "tournament_id")}
        clone = Flight(tournament_id=T3, **data)
        db.session.add(clone)
        db.session.flush()
        flight_map[fl.id] = clone.id

    # Heats: remap event and flight, then clone the assignment rows. The
    # per-event competitor map that used to be chosen here went with the JSON
    # remap in commit F2; the row clone below picks its map off each row's
    # own `competitor_type`, which is a stored fact rather than an inference
    # from the event's type.
    heat_count = 0
    assign_count = 0
    for ev in Event.query.filter_by(tournament_id=SRC_T).all():
        for h in Heat.query.filter_by(event_id=ev.id).all():
            data = {c.name: getattr(h, c.name) for c in Heat.__table__.columns
                    if c.name not in ("id", "event_id", "flight_id")}
            # D12-C commit F2: the two JSON columns were remapped here, since
            # they were the only roster this script cloned before c57. They
            # are copied verbatim with the rest of `data` now and nothing
            # reads them, so remapping them would have been rewriting a
            # projection of the assignment rows cloned below. Commit F3 drops
            # the columns and `data` stops carrying them at all.
            clone_h = Heat(event_id=event_map[ev.id],
                           flight_id=flight_map.get(h.flight_id),
                           **data)
            db.session.add(clone_h)
            db.session.flush()
            heat_count += 1

            # c57: clone the heat_assignments rows as well.
            #
            # This script predates D12-C. It cloned the two JSON columns and
            # nothing else, so every staged heat arrived with a roster in JSON
            # and zero assignment rows: 173 heats, 379 rows on T2, 0 on T3.
            # Nothing read the rows yet, so the oracle lane never noticed, and
            # it would have gone on not noticing right up until D12-C commit E
            # moved the accessors onto the rows and every T3 heat read as
            # empty. The oracle template's job is to be a faithful copy of the
            # 2026 damage; a table it silently drops is not damage, it is a
            # staging bug wearing damage's clothes.
            #
            # A missing map entry is fatal rather than skipped. Measured on
            # proam_prod_mirror_p0: 379 rows, every one resolving to a live
            # competitor of its own kind, 0 off-spine uids, 0 rows whose
            # competitor_type disagrees with its event. There is no legitimate
            # unmappable row, so an unmappable row means the clone is wrong.
            for a in HeatAssignment.query.filter_by(heat_id=h.id).all():
                cmap = col_map if a.competitor_type == "college" else pro_map
                umap = col_uid if a.competitor_type == "college" else pro_uid
                new_cid = cmap.get(a.competitor_id)
                new_uid = umap.get(a.competitor_id)
                if new_cid is None or new_uid is None:
                    raise SystemExit(
                        f"heat_assignments row {a.id} (heat {h.id}, "
                        f"{a.competitor_type} {a.competitor_id}) has no clone "
                        f"in T3; refusing to stage a partial roster")
                db.session.add(HeatAssignment(
                    heat_id=clone_h.id,
                    uid=new_uid,
                    competitor_id=new_cid,
                    competitor_type=a.competitor_type,
                    stand_number=a.stand_number,
                ))
                assign_count += 1

    db.session.commit()
    print(f"staged: teams={len(team_map)} events={len(event_map)} "
          f"pros={len(pro_map)} colleges={len(col_map)} "
          f"flights={len(flight_map)} heats={heat_count} "
          f"assignments={assign_count}")
    print("pro id range T3:", min(pro_map.values()), "-", max(pro_map.values()))
    print("college id range T3:", min(col_map.values()), "-", max(col_map.values()))
