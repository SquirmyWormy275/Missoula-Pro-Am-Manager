"""Phase 2 workflow integration QA tests.

DEPRECATED FIXTURE PATTERN: this file copies instance/proam.db (production
data). See tests/test_edge_cases.py module docstring for the full rationale.
For new tests, use tests/fixtures/synthetic_data.py + create_test_app().
The fixture below now SKIPs cleanly when SOURCE_DB is absent (CI default).
"""
from __future__ import annotations

import re
import shutil
import uuid
from pathlib import Path

import pytest

from app import create_app

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SOURCE_DB = PROJECT_ROOT / "instance" / "proam.db"
TMP_ROOT = PROJECT_ROOT / ".qa_tmp"


@pytest.fixture()
def qa_env(monkeypatch):
    """Return a fresh app/client pair backed by a copied real database.

    Skips cleanly when SOURCE_DB is absent so CI without prod data passes.
    See module docstring for the deprecation context.
    """
    if not SOURCE_DB.exists():
        pytest.skip(
            f"SOURCE_DB ({SOURCE_DB}) is absent; "
            "test relies on local prod-data copy, see deprecation in module docstring"
        )
    TMP_ROOT.mkdir(exist_ok=True)
    temp_dir = TMP_ROOT / f"integration-qa-{uuid.uuid4().hex}"
    temp_dir.mkdir()
    db_copy = temp_dir / "proam-copy.db"
    shutil.copy2(SOURCE_DB, db_copy)

    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_copy}")
    monkeypatch.setenv("SECRET_KEY", "integration-qa-secret")
    monkeypatch.setenv("FLASK_ENV", "testing")
    monkeypatch.setenv("TESTING", "1")

    app = create_app()
    app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)

    with app.app_context():
        from models import User

        admin_user = User.query.order_by(User.id).first()
        assert admin_user is not None, "expected at least one admin-capable user in copied DB"

    client = app.test_client()
    with client.session_transaction() as sess:
        sess["_user_id"] = str(admin_user.id)
        sess["_fresh"] = True

    try:
        yield {"app": app, "client": client, "db_copy": db_copy}
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def _seed_pro_workflow(app, *, event_name: str, max_stands: int = 2):
    """Create an isolated pro tournament, timed event, and ranked competitors."""
    with app.app_context():
        from config import event_rank_category
        from database import db
        from models import Event, EventResult, ProEventRank, Tournament
        from models.competitor import ProCompetitor

        tournament = Tournament(
            name=f"QA Pro Workflow {uuid.uuid4().hex[:8]}",
            year=2026,
            status="setup",
        )
        db.session.add(tournament)
        db.session.flush()

        event = Event(
            tournament_id=tournament.id,
            name=event_name,
            event_type="pro",
            gender="M",
            scoring_type="time",
            scoring_order="lowest_wins",
            stand_type="underhand" if "Underhand" in event_name else "standing_block",
            max_stands=max_stands,
            status="pending",
            is_handicap=False,
        )
        db.session.add(event)
        db.session.flush()
        rank_category = event_rank_category(event)

        seeded = []
        for idx in range(1, 4):
            comp = ProCompetitor(
                tournament_id=tournament.id,
                name=f"Seeded QA Pro {idx}",
                gender="M",
                email=f"seeded-{idx}@qa.test",
                status="active",
            )
            comp.set_events_entered([str(event.id)])
            db.session.add(comp)
            db.session.flush()

            db.session.add(
                EventResult(
                    event_id=event.id,
                    competitor_id=comp.id,
                    competitor_type="pro",
                    competitor_name=comp.name,
                    status="pending",
                )
            )
            db.session.add(
                ProEventRank(
                    tournament_id=tournament.id,
                    competitor_id=comp.id,
                    event_category=rank_category,
                    rank=idx,
                )
            )
            seeded.append(comp.id)

        db.session.commit()
        return {
            "tournament_id": tournament.id,
            "event_id": event.id,
            "seeded_ids": seeded,
        }


def _seed_heat_with_results(
    app,
    *,
    event_name: str = "Underhand",
    competitor_count: int = 3,
    max_stands: int = 3,
):
    """Create an isolated event with one pending heat and result rows."""
    with app.app_context():
        from database import db
        from models import Event, EventResult, Heat, Tournament
        from models.competitor import ProCompetitor

        tournament = Tournament(
            name=f"QA Scoring {uuid.uuid4().hex[:8]}",
            year=2026,
            status="setup",
        )
        db.session.add(tournament)
        db.session.flush()

        event = Event(
            tournament_id=tournament.id,
            name=event_name,
            event_type="pro",
            gender="M",
            scoring_type="time",
            scoring_order="lowest_wins",
            stand_type="underhand",
            max_stands=max_stands,
            status="in_progress",
        )
        db.session.add(event)
        db.session.flush()

        heat = Heat(
            event_id=event.id,
            heat_number=1,
            run_number=1,
            status="pending",
        )
        db.session.add(heat)
        db.session.flush()

        competitor_ids = []
        for idx in range(1, competitor_count + 1):
            comp = ProCompetitor(
                tournament_id=tournament.id,
                name=f"Scoring QA Pro {idx}",
                gender="M",
                email=f"scoring-{idx}@qa.test",
                status="active",
            )
            comp.set_events_entered([str(event.id)])
            db.session.add(comp)
            db.session.flush()

            heat.add_competitor(comp.id)
            heat.set_stand_assignment(comp.id, idx)
            db.session.add(
                EventResult(
                    event_id=event.id,
                    competitor_id=comp.id,
                    competitor_type="pro",
                    competitor_name=comp.name,
                    status="pending",
                )
            )
            competitor_ids.append(comp.id)

        db.session.flush()
        heat.sync_assignments("pro")
        db.session.commit()

        return {
            "tournament_id": tournament.id,
            "event_id": event.id,
            "heat_id": heat.id,
            "competitor_ids": competitor_ids,
        }


def _seed_day_of_operations_state(app):
    """Create an isolated event with two heats plus one unassigned competitor."""
    with app.app_context():
        from database import db
        from models import Event, EventResult, Heat, Tournament
        from models.competitor import ProCompetitor

        tournament = Tournament(
            name=f"QA Day Ops {uuid.uuid4().hex[:8]}",
            year=2026,
            status="setup",
        )
        db.session.add(tournament)
        db.session.flush()

        event = Event(
            tournament_id=tournament.id,
            name="Underhand",
            event_type="pro",
            gender="M",
            scoring_type="time",
            scoring_order="lowest_wins",
            stand_type="underhand",
            max_stands=3,
            status="in_progress",
        )
        db.session.add(event)
        db.session.flush()

        heat_a = Heat(event_id=event.id, heat_number=1, run_number=1, status="pending")
        heat_b = Heat(event_id=event.id, heat_number=2, run_number=1, status="pending")
        heat_empty = Heat(event_id=event.id, heat_number=3, run_number=1, status="pending")
        db.session.add_all([heat_a, heat_b, heat_empty])
        db.session.flush()

        assigned_ids = []
        for idx in range(1, 5):
            comp = ProCompetitor(
                tournament_id=tournament.id,
                name=f"Day Ops Pro {idx}",
                gender="M",
                email=f"dayops-{idx}@qa.test",
                status="active",
            )
            comp.set_events_entered([str(event.id)])
            db.session.add(comp)
            db.session.flush()

            target_heat = heat_a if idx <= 2 else heat_b
            target_heat.add_competitor(comp.id)
            target_heat.set_stand_assignment(comp.id, 1 if idx % 2 == 1 else 2)
            db.session.add(
                EventResult(
                    event_id=event.id,
                    competitor_id=comp.id,
                    competitor_type="pro",
                    competitor_name=comp.name,
                    status="pending",
                )
            )
            assigned_ids.append(comp.id)

        late_comp = ProCompetitor(
            tournament_id=tournament.id,
            name="Day Ops Late Entry",
            gender="M",
            email="dayops-late@qa.test",
            status="active",
        )
        late_comp.set_events_entered([str(event.id)])
        db.session.add(late_comp)
        db.session.flush()
        db.session.add(
            EventResult(
                event_id=event.id,
                competitor_id=late_comp.id,
                competitor_type="pro",
                competitor_name=late_comp.name,
                status="pending",
            )
        )

        for heat in (heat_a, heat_b, heat_empty):
            db.session.flush()
            heat.sync_assignments("pro")

        db.session.commit()
        return {
            "tournament_id": tournament.id,
            "event_id": event.id,
            "heat_a_id": heat_a.id,
            "heat_b_id": heat_b.id,
            "heat_empty_id": heat_empty.id,
            "assigned_ids": assigned_ids,
            "late_id": late_comp.id,
        }


def _extract_heat_version(response) -> int:
    """Extract the hidden heat_version field from the scoring form."""
    html = response.get_data(as_text=True)
    match = re.search(r'name="heat_version"\s+value="(\d+)"', html)
    assert match, "expected heat_version hidden field in scoring form"
    return int(match.group(1))


def _rank_map_for_event(app, event_id: int) -> dict[int, int]:
    """Return competitor_id -> rank for the event's rank category."""
    with app.app_context():
        from config import event_rank_category
        from database import db
        from models import Event, ProEventRank

        event = db.session.get(Event, event_id)
        category = event_rank_category(event)
        rows = ProEventRank.query.filter_by(
            tournament_id=event.tournament_id,
            event_category=category,
        ).all()
        return {row.competitor_id: row.rank for row in rows}


def test_registration_to_heat_workflow(qa_env):
    """Register a pro competitor, enroll them, generate heats, and verify placement."""
    app = qa_env["app"]
    client = qa_env["client"]
    seeded = _seed_pro_workflow(app, event_name="Underhand", max_stands=2)

    create_response = client.post(
        f"/registration/{seeded['tournament_id']}/pro/new",
        data={
            "name": "Integration QA Added Pro",
            "gender": "M",
            "address": "123 Test Lane",
            "phone": "406-555-0101",
            "email": "integration-added@example.com",
            "shirt_size": "L",
        },
        follow_redirects=False,
    )
    assert create_response.status_code == 302

    with app.app_context():
        from config import event_rank_category
        from database import db
        from models import Event, ProEventRank
        from models.competitor import ProCompetitor

        competitor = ProCompetitor.query.filter_by(
            tournament_id=seeded["tournament_id"],
            name="Integration QA Added Pro",
        ).first()
        assert competitor is not None
        new_competitor_id = competitor.id
        detail_response = client.get(f"/registration/{seeded['tournament_id']}/pro/{new_competitor_id}")
        assert detail_response.status_code == 200
        assert "Integration QA Added Pro" in detail_response.get_data(as_text=True)

        event = db.session.get(Event, seeded["event_id"])
        rank_row = ProEventRank(
            tournament_id=seeded["tournament_id"],
            competitor_id=new_competitor_id,
            event_category=event_rank_category(event),
            rank=4,
        )
        db.session.add(rank_row)
        db.session.commit()

    enroll_response = client.post(
        f"/registration/{seeded['tournament_id']}/pro/{new_competitor_id}/update-events",
        data={"event_ids": [str(seeded["event_id"])]},
        follow_redirects=False,
    )
    assert enroll_response.status_code == 302

    generate_response = client.post(
        f"/scheduling/{seeded['tournament_id']}/event/{seeded['event_id']}/generate-heats",
        data={},
        follow_redirects=False,
    )
    assert generate_response.status_code == 302

    rank_map = _rank_map_for_event(app, seeded["event_id"])
    with app.app_context():
        from models import Heat

        heats = (
            Heat.query.filter_by(event_id=seeded["event_id"], run_number=1)
            .order_by(Heat.heat_number)
            .all()
        )
        assert len(heats) == 2
        flattened = [cid for heat in heats for cid in heat.get_competitors()]
        assert new_competitor_id in flattened

        heat_ranks = [[rank_map[cid] for cid in heat.get_competitors()] for heat in heats]
        assert heat_ranks == [[1, 4], [2, 3]]


def test_heat_generation_rule_verification(qa_env):
    """Generate heats for paired SB/UH events and verify rule behavior."""
    app = qa_env["app"]
    client = qa_env["client"]

    with app.app_context():
        from config import event_rank_category
        from database import db
        from models import Event, EventResult, ProEventRank, Tournament
        from models.competitor import ProCompetitor

        tournament = Tournament(
            name=f"QA Heat Rules {uuid.uuid4().hex[:8]}",
            year=2026,
            status="setup",
        )
        db.session.add(tournament)
        db.session.flush()

        uh_event = Event(
            tournament_id=tournament.id,
            name="Underhand",
            event_type="pro",
            gender="M",
            scoring_type="time",
            scoring_order="lowest_wins",
            stand_type="underhand",
            max_stands=2,
            status="pending",
        )
        sb_event = Event(
            tournament_id=tournament.id,
            name="Springboard",
            event_type="pro",
            gender="M",
            scoring_type="time",
            scoring_order="lowest_wins",
            stand_type="springboard",
            max_stands=2,
            status="pending",
        )
        db.session.add_all([uh_event, sb_event])
        db.session.flush()
        uh_rank_category = event_rank_category(uh_event)
        sb_rank_category = event_rank_category(sb_event)

        competitor_ids = []
        for idx in range(1, 5):
            comp = ProCompetitor(
                tournament_id=tournament.id,
                name=f"Heat Rule Pro {idx}",
                gender="M",
                email=f"heatrules-{idx}@qa.test",
                status="active",
            )
            comp.set_events_entered([str(uh_event.id), str(sb_event.id)])
            db.session.add(comp)
            db.session.flush()
            competitor_ids.append(comp.id)

            for event in (uh_event, sb_event):
                db.session.add(
                    EventResult(
                        event_id=event.id,
                        competitor_id=comp.id,
                        competitor_type="pro",
                        competitor_name=comp.name,
                        status="pending",
                    )
                )
                db.session.add(
                    ProEventRank(
                        tournament_id=tournament.id,
                        competitor_id=comp.id,
                        event_category=uh_rank_category if event.id == uh_event.id else sb_rank_category,
                        rank=idx,
                    )
                )

        db.session.commit()
        tournament_id = tournament.id
        uh_event_id = uh_event.id
        sb_event_id = sb_event.id

    for event_id in (uh_event_id, sb_event_id):
        response = client.post(
            f"/scheduling/{tournament_id}/event/{event_id}/generate-heats",
            data={},
            follow_redirects=False,
        )
        assert response.status_code == 302

    with app.app_context():
        from models import Heat

        uh_heats = (
            Heat.query.filter_by(event_id=uh_event_id, run_number=1)
            .order_by(Heat.heat_number)
            .all()
        )
        sb_heats = (
            Heat.query.filter_by(event_id=sb_event_id, run_number=1)
            .order_by(Heat.heat_number)
            .all()
        )
        assert len(uh_heats) == 2
        assert len(sb_heats) == 2

        for heats in (uh_heats, sb_heats):
            all_ids = []
            for heat in heats:
                competitors = heat.get_competitors()
                assert 1 <= len(competitors) <= 2
                all_ids.extend(competitors)
            assert len(all_ids) == len(set(all_ids))
            assert all(heat.get_competitors() for heat in heats)

        uh_rank_map = _rank_map_for_event(app, uh_event_id)
        uh_heat_ranks = [[uh_rank_map[cid] for cid in heat.get_competitors()] for heat in uh_heats]
        assert uh_heat_ranks == [[1, 4], [2, 3]]

        # Each event's heats must partition its own competitor pool
        # disjointly, but heats across *different* events can legitimately
        # share competitors — a single person can enter both underhand
        # and springboard, landing in heat 1 of each. The disjoint-across-
        # events check was a wrong domain assumption.
        sb_heat1 = set(sb_heats[0].get_competitors())
        uh_heat1 = set(uh_heats[0].get_competitors())
        assert sb_heat1  # non-empty
        assert uh_heat1  # non-empty


def test_scoring_workflow_persists_and_updates_results(qa_env):
    """Score a heat through the form and verify persistence and update behavior."""
    app = qa_env["app"]
    client = qa_env["client"]
    seeded = _seed_heat_with_results(app)

    get_response = client.get(f"/scoring/{seeded['tournament_id']}/heat/{seeded['heat_id']}/enter")
    assert get_response.status_code == 200
    version = _extract_heat_version(get_response)

    initial_form = {"heat_version": str(version)}
    updated_form = {"heat_version": str(version + 1)}
    for idx, competitor_id in enumerate(seeded["competitor_ids"], start=1):
        initial_form[f"t1_run1_{competitor_id}"] = f"{14 + idx:.2f}"
        initial_form[f"t2_run1_{competitor_id}"] = f"{15 + idx:.2f}"
        initial_form[f"status_{competitor_id}"] = "completed"
        updated_form[f"t1_run1_{competitor_id}"] = f"{24 + idx:.2f}"
        updated_form[f"t2_run1_{competitor_id}"] = f"{25 + idx:.2f}"
        updated_form[f"status_{competitor_id}"] = "completed"

    save_response = client.post(
        f"/scoring/{seeded['tournament_id']}/heat/{seeded['heat_id']}/enter",
        data=initial_form,
        headers={"X-Requested-With": "XMLHttpRequest"},
    )
    assert save_response.status_code == 200
    assert save_response.json["ok"] is True

    with app.app_context():
        from models import EventResult

        results = (
            EventResult.query.filter_by(event_id=seeded["event_id"])
            .order_by(EventResult.competitor_id)
            .all()
        )
        assert all(result.status == "completed" for result in results)
        assert [float(result.result_value) for result in results] == [15.5, 16.5, 17.5]

    results_page = client.get(f"/scoring/{seeded['tournament_id']}/event/{seeded['event_id']}/results")
    assert results_page.status_code == 200
    assert "Scoring QA Pro 1" in results_page.get_data(as_text=True)

    second_save = client.post(
        f"/scoring/{seeded['tournament_id']}/heat/{seeded['heat_id']}/enter",
        data=updated_form,
        headers={"X-Requested-With": "XMLHttpRequest"},
    )
    assert second_save.status_code == 200
    assert second_save.json["ok"] is True

    with app.app_context():
        from models import EventResult

        results = (
            EventResult.query.filter_by(event_id=seeded["event_id"])
            .order_by(EventResult.competitor_id)
            .all()
        )
        assert [float(result.result_value) for result in results] == [25.5, 26.5, 27.5]


def test_concurrent_scoring_simulation_documents_optimistic_locking(qa_env):
    """Submit the same heat version twice and verify conflict handling."""
    app = qa_env["app"]
    first_client = qa_env["client"]
    second_client = app.test_client()

    with app.app_context():
        from models import User

        admin_user = User.query.order_by(User.id).first()

    with second_client.session_transaction() as sess:
        sess["_user_id"] = str(admin_user.id)
        sess["_fresh"] = True

    seeded = _seed_heat_with_results(app)
    get_response = first_client.get(f"/scoring/{seeded['tournament_id']}/heat/{seeded['heat_id']}/enter")
    assert get_response.status_code == 200
    version = _extract_heat_version(get_response)

    payload = {"heat_version": str(version)}
    for idx, competitor_id in enumerate(seeded["competitor_ids"], start=1):
        payload[f"t1_run1_{competitor_id}"] = f"{20 + idx:.2f}"
        payload[f"t2_run1_{competitor_id}"] = f"{21 + idx:.2f}"
        payload[f"status_{competitor_id}"] = "completed"

    first_post = first_client.post(
        f"/scoring/{seeded['tournament_id']}/heat/{seeded['heat_id']}/enter",
        data=payload,
        headers={"X-Requested-With": "XMLHttpRequest"},
    )
    second_post = second_client.post(
        f"/scoring/{seeded['tournament_id']}/heat/{seeded['heat_id']}/enter",
        data=payload,
        headers={"X-Requested-With": "XMLHttpRequest"},
    )

    assert first_post.status_code == 200
    assert second_post.status_code == 409
    assert second_post.json["ok"] is False

    with app.app_context():
        from models import EventResult

        values = [
            float(row.result_value)
            for row in EventResult.query.filter_by(event_id=seeded["event_id"])
            .order_by(EventResult.competitor_id)
            .all()
        ]
        assert values == [21.5, 22.5, 23.5]


def test_day_of_operations_workflow(qa_env):
    """Scratch, move, add, and delete-heat routes mutate heat state coherently."""
    app = qa_env["app"]
    client = qa_env["client"]
    seeded = _seed_day_of_operations_state(app)
    scratch_id = seeded["assigned_ids"][0]
    move_id = seeded["assigned_ids"][1]

    scratch_response = client.post(
        f"/scheduling/{seeded['tournament_id']}/event/{seeded['event_id']}/scratch-competitor",
        data={"competitor_id": str(scratch_id), "heat_id": str(seeded["heat_a_id"])},
        follow_redirects=False,
    )
    assert scratch_response.status_code == 302

    move_response = client.post(
        f"/scheduling/{seeded['tournament_id']}/event/{seeded['event_id']}/move-competitor",
        data={
            "competitor_id": str(move_id),
            "from_heat_id": str(seeded["heat_a_id"]),
            "to_heat_id": str(seeded["heat_b_id"]),
        },
        follow_redirects=False,
    )
    assert move_response.status_code == 302

    add_response = client.post(
        f"/scheduling/{seeded['tournament_id']}/event/{seeded['event_id']}/add-to-heat",
        data={"competitor_id": str(seeded["late_id"]), "heat_id": str(seeded["heat_a_id"])},
        follow_redirects=False,
    )
    assert add_response.status_code == 302

    delete_response = client.post(
        f"/scheduling/{seeded['tournament_id']}/event/{seeded['event_id']}/delete-heat/{seeded['heat_empty_id']}",
        data={},
        follow_redirects=False,
    )
    assert delete_response.status_code == 302

    with app.app_context():
        from database import db
        from models import EventResult, Heat
        from models.competitor import ProCompetitor

        heat_a = db.session.get(Heat, seeded["heat_a_id"])
        heat_b = db.session.get(Heat, seeded["heat_b_id"])
        heat_numbers = [
            heat.heat_number
            for heat in Heat.query.filter_by(event_id=seeded["event_id"])
            .order_by(Heat.heat_number)
            .all()
        ]
        assert scratch_id not in heat_a.get_competitors()
        scratched_result = EventResult.query.filter_by(
            event_id=seeded["event_id"],
            competitor_id=scratch_id,
            competitor_type="pro",
        ).first()
        assert scratched_result.status == "scratched"
        assert db.session.get(ProCompetitor, scratch_id) is not None

        assert move_id not in heat_a.get_competitors()
        assert move_id in heat_b.get_competitors()
        assert len(heat_a.get_competitors()) <= 3
        assert len(heat_b.get_competitors()) <= 3

        assert seeded["late_id"] in heat_a.get_competitors()
        assert len(heat_a.get_competitors()) <= 3
        assert heat_numbers == [1, 2]


def test_ala_membership_report_generates_html_and_pdf(qa_env):
    """ALA report returns HTML and a non-empty PDF export."""
    app = qa_env["app"]
    client = qa_env["client"]

    with app.app_context():
        from models import Tournament
        from models.competitor import ProCompetitor

        tournament = Tournament.query.order_by(Tournament.id).first()
        attendee_count = ProCompetitor.query.filter_by(
            tournament_id=tournament.id,
            status="active",
        ).count()

    html_response = client.get(f"/reporting/ala-membership-report/{tournament.id}")
    assert html_response.status_code == 200
    html = html_response.get_data(as_text=True)
    assert f"All Attending Pro Competitors ({attendee_count})" in html

    pdf_response = client.get(f"/reporting/ala-membership-report/{tournament.id}/pdf")
    assert pdf_response.status_code == 200
    assert "application/pdf" in pdf_response.headers.get("Content-Type", "")
    assert len(pdf_response.data) > 0


def test_strathmark_assign_marks_route_falls_back_cleanly_when_unconfigured(qa_env):
    """Mark assignment route renders without crashing for an eligible event."""
    app = qa_env["app"]
    client = qa_env["client"]

    with app.app_context():
        from database import db
        from models import Event, EventResult, Tournament
        from models.competitor import ProCompetitor

        tournament = Tournament(
            name=f"QA Marks {uuid.uuid4().hex[:8]}",
            year=2026,
            status="setup",
        )
        db.session.add(tournament)
        db.session.flush()

        event = Event(
            tournament_id=tournament.id,
            name="Underhand",
            event_type="pro",
            gender="M",
            scoring_type="time",
            scoring_order="lowest_wins",
            stand_type="underhand",
            max_stands=2,
            is_handicap=True,
            status="in_progress",
        )
        db.session.add(event)
        db.session.flush()

        competitor = ProCompetitor(
            tournament_id=tournament.id,
            name="Mark QA Pro",
            gender="M",
            email="marks@qa.test",
            status="active",
        )
        competitor.set_events_entered([str(event.id)])
        db.session.add(competitor)
        db.session.flush()

        db.session.add(
            EventResult(
                event_id=event.id,
                competitor_id=competitor.id,
                competitor_type="pro",
                competitor_name=competitor.name,
                status="pending",
            )
        )
        db.session.commit()

        tournament_id = tournament.id
        event_id = event.id
        predicted_count = EventResult.query.filter(
            EventResult.predicted_time.isnot(None)
        ).count()
        handicap_count = EventResult.query.filter(EventResult.handicap_factor != 0.0).count()

    response = client.get(f"/scheduling/{tournament_id}/events/{event_id}/assign-marks")
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "assign marks" in body.lower() or "handicap" in body.lower()
    assert (
        "not configured" in body.lower()
        or "manual" in body.lower()
        or "csv" in body.lower()
    )
    assert predicted_count >= 0
    assert handicap_count >= 0
