"""Stage the multi-tournament oracle template (tranche 2, c37).

Clones tournament 2 into a staged 2027 tournament inside
proam_prod_mirror_mt: new ids everywhere, FKs and JSON remapped
(heat rosters, stand keys, events_entered with its c22 mixed shapes,
partners, gear_sharing), competitor names IDENTICAL on purpose, because
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

SRC_T = 2

app = create_app()
with app.app_context():
    from models import Tournament
    from models.competitor import CollegeCompetitor, ProCompetitor
    from models.competitor_identity import Competitor
    from models.event import Event
    from models.heat import Flight, Heat
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

    # Heats: remap event, flight, competitor JSON, stand keys
    def comp_map_for(etype):
        return col_map if etype == "college" else pro_map

    heat_count = 0
    for ev in Event.query.filter_by(tournament_id=SRC_T).all():
        m = comp_map_for(ev.event_type)
        for h in Heat.query.filter_by(event_id=ev.id).all():
            data = {c.name: getattr(h, c.name) for c in Heat.__table__.columns
                    if c.name not in ("id", "event_id", "flight_id")}
            try:
                ids = json.loads(data.get("competitors") or "[]")
            except (TypeError, ValueError):
                ids = []
            data["competitors"] = json.dumps(
                [m.get(int(c), int(c)) for c in ids])
            try:
                stands = json.loads(data.get("stand_assignments") or "{}")
            except (TypeError, ValueError):
                stands = {}
            data["stand_assignments"] = json.dumps(
                {str(m.get(int(k), int(k))): v for k, v in stands.items()})
            db.session.add(Heat(event_id=event_map[ev.id],
                                flight_id=flight_map.get(h.flight_id),
                                **data))
            heat_count += 1

    db.session.commit()
    print(f"staged: teams={len(team_map)} events={len(event_map)} "
          f"pros={len(pro_map)} colleges={len(col_map)} "
          f"flights={len(flight_map)} heats={heat_count}")
    print("pro id range T3:", min(pro_map.values()), "-", max(pro_map.values()))
    print("college id range T3:", min(col_map.values()), "-", max(col_map.values()))
