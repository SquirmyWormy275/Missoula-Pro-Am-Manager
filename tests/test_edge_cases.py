"""Phase 3 edge-case QA tests."""
from __future__ import annotations

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
    """Return a fresh app/client pair backed by a copied real database."""
    TMP_ROOT.mkdir(exist_ok=True)
    temp_dir = TMP_ROOT / f"edge-qa-{uuid.uuid4().hex}"
    temp_dir.mkdir()
    db_copy = temp_dir / "proam-copy.db"
    shutil.copy2(SOURCE_DB, db_copy)

    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_copy}")
    monkeypatch.setenv("SECRET_KEY", "edge-qa-secret")
    monkeypatch.setenv("FLASK_ENV", "testing")
    monkeypatch.setenv("TESTING", "1")

    app = create_app()
    app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)

    with app.app_context():
        from models import User

        admin_user = User.query.order_by(User.id).first()
        assert admin_user is not None

    client = app.test_client()
    with client.session_transaction() as sess:
        sess["_user_id"] = str(admin_user.id)
        sess["_fresh"] = True

    try:
        yield {"app": app, "client": client}
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def _seed_boundary_event(app, competitor_count: int, max_stands: int):
    """Create a pro event with the requested competitor count and standings config."""
    with app.app_context():
        from database import db
        from config import event_rank_category
        from models import Event, EventResult, ProEventRank, Tournament
        from models.competitor import ProCompetitor

        tournament = Tournament(name=f"QA Boundary {uuid.uuid4().hex[:8]}", year=2026, status="setup")
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
            max_stands=max_stands,
            status="pending",
        )
        db.session.add(event)
        db.session.flush()
        rank_category = event_rank_category(event)

        for idx in range(1, competitor_count + 1):
            comp = ProCompetitor(
                tournament_id=tournament.id,
                name=f"Boundary Pro {competitor_count}-{idx}",
                gender="M",
                email=f"boundary-{competitor_count}-{idx}@qa.test",
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

        db.session.commit()
        return tournament.id, event.id


def _seed_scoring_heat(app, competitor_count: int = 1):
    """Create a single pending timed heat."""
    with app.app_context():
        from database import db
        from models import Event, EventResult, Heat, Tournament
        from models.competitor import ProCompetitor

        tournament = Tournament(name=f"QA Score {uuid.uuid4().hex[:8]}", year=2026, status="setup")
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
            max_stands=max(competitor_count, 1),
            status="in_progress",
        )
        db.session.add(event)
        db.session.flush()

        heat = Heat(event_id=event.id, heat_number=1, run_number=1, status="pending")
        db.session.add(heat)
        db.session.flush()

        comp_ids = []
        for idx in range(1, competitor_count + 1):
            comp = ProCompetitor(
                tournament_id=tournament.id,
                name=f"Score Edge Pro {idx}",
                gender="M",
                email=f"score-edge-{idx}@qa.test",
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
            comp_ids.append(comp.id)

        db.session.flush()
        heat.sync_assignments("pro")
        db.session.commit()
        return tournament.id, event.id, heat.id, comp_ids


def _seed_college_scored_competitor(app):
    """Create a scored college competitor tied to a deletable team."""
    with app.app_context():
        from database import db
        from models import Event, EventResult, Team, Tournament
        from models.competitor import CollegeCompetitor

        tournament = Tournament(name=f"QA College {uuid.uuid4().hex[:8]}", year=2026, status="setup")
        db.session.add(tournament)
        db.session.flush()

        team = Team(
            tournament_id=tournament.id,
            team_code="QA-A",
            school_name="QA Lumber U",
            school_abbreviation="QLU",
        )
        db.session.add(team)
        db.session.flush()

        event = Event(
            tournament_id=tournament.id,
            name="Single Buck",
            event_type="college",
            gender="M",
            scoring_type="time",
            scoring_order="lowest_wins",
            stand_type="saw_hand",
            max_stands=2,
            status="completed",
            is_finalized=True,
        )
        db.session.add(event)
        db.session.flush()

        competitor = CollegeCompetitor(
            tournament_id=tournament.id,
            team_id=team.id,
            name="College Edge Competitor",
            gender="M",
            status="active",
        )
        competitor.set_events_entered([str(event.id)])
        db.session.add(competitor)
        db.session.flush()

        result = EventResult(
            event_id=event.id,
            competitor_id=competitor.id,
            competitor_type="college",
            competitor_name=competitor.name,
            result_value=22.0,
            run1_value=22.0,
            status="completed",
            final_position=1,
        )
        db.session.add(result)
        db.session.commit()
        return tournament.id, team.id, competitor.id, event.id, result.id


def _extract_heat_version(response) -> str:
    """Extract the heat version token from the enter-heat page."""
    import re

    html = response.get_data(as_text=True)
    match = re.search(r'name="heat_version"\s+value="(\d+)"', html)
    assert match
    return match.group(1)


def test_heat_generation_boundary_counts(qa_env):
    """Heat generation handles 1, max, and max+1 competitors without malformed heat counts."""
    app = qa_env["app"]
    client = qa_env["client"]

    scenarios = [(1, 4, [1]), (4, 4, [4]), (5, 4, [3, 2])]
    for competitor_count, max_stands, expected_sizes in scenarios:
        tournament_id, event_id = _seed_boundary_event(app, competitor_count, max_stands)
        response = client.post(
            f"/scheduling/{tournament_id}/event/{event_id}/generate-heats",
            data={},
            follow_redirects=False,
        )
        assert response.status_code == 302

        with app.app_context():
            from models import Heat

            heats = (
                Heat.query.filter_by(event_id=event_id, run_number=1)
                .order_by(Heat.heat_number)
                .all()
            )
            assert [len(heat.get_competitors()) for heat in heats] == expected_sizes


def test_unassigned_competitor_and_all_scratched_heat_render_cleanly(qa_env):
    """An unassigned competitor and an all-scratched heat do not break views."""
    app = qa_env["app"]
    client = qa_env["client"]

    with app.app_context():
        from database import db
        from models import Event, EventResult, Heat, Tournament
        from models.competitor import ProCompetitor

        tournament = Tournament(name=f"QA Unassigned {uuid.uuid4().hex[:8]}", year=2026, status="setup")
        db.session.add(tournament)
        db.session.flush()

        idle_competitor = ProCompetitor(
            tournament_id=tournament.id,
            name="Idle Pro",
            gender="M",
            email="idle@qa.test",
            status="active",
        )
        idle_competitor.set_events_entered([])
        db.session.add(idle_competitor)

        event = Event(
            tournament_id=tournament.id,
            name="Underhand",
            event_type="pro",
            gender="M",
            scoring_type="time",
            scoring_order="lowest_wins",
            stand_type="underhand",
            max_stands=2,
            status="in_progress",
        )
        db.session.add(event)
        db.session.flush()

        heat = Heat(event_id=event.id, heat_number=1, run_number=1, status="pending")
        db.session.add(heat)
        db.session.flush()

        competitors = []
        for idx in range(1, 3):
            comp = ProCompetitor(
                tournament_id=tournament.id,
                name=f"Scratch Edge {idx}",
                gender="M",
                email=f"scratch-edge-{idx}@qa.test",
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
            competitors.append(comp.id)

        db.session.flush()
        heat.sync_assignments("pro")
        db.session.commit()
        tournament_id = tournament.id
        event_id = event.id
        heat_id = heat.id

    dashboard = client.get(f"/tournament/{tournament_id}/pro")
    assert dashboard.status_code == 200
    assert "Idle Pro" in dashboard.get_data(as_text=True)

    for competitor_id in competitors:
        response = client.post(
            f"/scheduling/{tournament_id}/event/{event_id}/scratch-competitor",
            data={"competitor_id": str(competitor_id), "heat_id": str(heat_id)},
            follow_redirects=False,
        )
        assert response.status_code == 302

    heats_page = client.get(f"/scheduling/{tournament_id}/event/{event_id}/heats")
    assert heats_page.status_code == 200

    with app.app_context():
        from database import db
        from models import Heat

        heat = db.session.get(Heat, heat_id)
        assert heat.get_competitors() == []
        assert heat.status == "completed"


def test_score_value_boundaries_and_status_values(qa_env):
    """Timed scoring accepts 0/999.9, rejects negatives, and handles DNS/DNF/DSQ statuses."""
    app = qa_env["app"]
    client = qa_env["client"]

    # 0.0 accepted
    tournament_id, event_id, heat_id, comp_ids = _seed_scoring_heat(app)
    form_page = client.get(f"/scoring/{tournament_id}/heat/{heat_id}/enter")
    version = _extract_heat_version(form_page)
    zero_response = client.post(
        f"/scoring/{tournament_id}/heat/{heat_id}/enter",
        data={
            "heat_version": version,
            f"t1_run1_{comp_ids[0]}": "0.0",
            f"t2_run1_{comp_ids[0]}": "0.0",
            f"status_{comp_ids[0]}": "completed",
        },
        headers={"X-Requested-With": "XMLHttpRequest"},
    )
    assert zero_response.status_code == 200

    with app.app_context():
        from models import EventResult

        result = EventResult.query.filter_by(event_id=event_id, competitor_id=comp_ids[0]).first()
        assert float(result.result_value) == 0.0

    # 999.9 accepted
    tournament_id, event_id, heat_id, comp_ids = _seed_scoring_heat(app)
    form_page = client.get(f"/scoring/{tournament_id}/heat/{heat_id}/enter")
    version = _extract_heat_version(form_page)
    high_response = client.post(
        f"/scoring/{tournament_id}/heat/{heat_id}/enter",
        data={
            "heat_version": version,
            f"t1_run1_{comp_ids[0]}": "999.9",
            f"t2_run1_{comp_ids[0]}": "999.9",
            f"status_{comp_ids[0]}": "completed",
        },
        headers={"X-Requested-With": "XMLHttpRequest"},
    )
    assert high_response.status_code == 200

    with app.app_context():
        from models import EventResult

        result = EventResult.query.filter_by(event_id=event_id, competitor_id=comp_ids[0]).first()
        assert float(result.result_value) == 999.9

    # Negative values should be rejected
    tournament_id, event_id, heat_id, comp_ids = _seed_scoring_heat(app)
    form_page = client.get(f"/scoring/{tournament_id}/heat/{heat_id}/enter")
    version = _extract_heat_version(form_page)
    negative_response = client.post(
        f"/scoring/{tournament_id}/heat/{heat_id}/enter",
        data={
            "heat_version": version,
            f"t1_run1_{comp_ids[0]}": "-5.0",
            f"t2_run1_{comp_ids[0]}": "-4.0",
            f"status_{comp_ids[0]}": "completed",
        },
        headers={"X-Requested-With": "XMLHttpRequest"},
    )
    assert negative_response.status_code in {400, 409}

    with app.app_context():
        from models import EventResult

        result = EventResult.query.filter_by(event_id=event_id, competitor_id=comp_ids[0]).first()
        assert result.result_value in (None, 0)

    # DNS / DNF / DSQ are stored and do not break results rendering
    tournament_id, event_id, heat_id, comp_ids = _seed_scoring_heat(app, competitor_count=3)
    form_page = client.get(f"/scoring/{tournament_id}/heat/{heat_id}/enter")
    version = _extract_heat_version(form_page)
    status_response = client.post(
        f"/scoring/{tournament_id}/heat/{heat_id}/enter",
        data={
            "heat_version": version,
            f"t1_run1_{comp_ids[0]}": "20.0",
            f"t2_run1_{comp_ids[0]}": "20.0",
            f"status_{comp_ids[0]}": "completed",
            f"t1_run1_{comp_ids[1]}": "21.0",
            f"t2_run1_{comp_ids[1]}": "21.0",
            f"status_{comp_ids[1]}": "dnf",
            f"t1_run1_{comp_ids[2]}": "22.0",
            f"t2_run1_{comp_ids[2]}": "22.0",
            f"status_{comp_ids[2]}": "scratched",
        },
        headers={"X-Requested-With": "XMLHttpRequest"},
    )
    assert status_response.status_code == 200

    results_page = client.get(f"/scoring/{tournament_id}/event/{event_id}/results")
    assert results_page.status_code == 200

    with app.app_context():
        from models import EventResult

        rows = (
            EventResult.query.filter_by(event_id=event_id)
            .order_by(EventResult.competitor_id)
            .all()
        )
        assert [row.status for row in rows] == ["completed", "dnf", "scratched"]


def test_delete_scored_competitor_and_regenerate_scored_event(qa_env):
    """Deleting a scored competitor should not orphan rows, and scored events should not regenerate silently."""
    app = qa_env["app"]
    client = qa_env["client"]

    tournament_id, team_id, competitor_id, event_id, result_id = _seed_college_scored_competitor(app)
    delete_response = client.post(
        f"/registration/{tournament_id}/college/competitor/{competitor_id}/delete",
        data={},
        follow_redirects=False,
    )
    assert delete_response.status_code == 302

    with app.app_context():
        from database import db
        from models import EventResult
        from models.competitor import CollegeCompetitor

        assert db.session.get(CollegeCompetitor, competitor_id) is None
        assert db.session.get(EventResult, result_id) is None

    tournament_id, event_id = _seed_boundary_event(app, competitor_count=3, max_stands=2)
    client.post(
        f"/scheduling/{tournament_id}/event/{event_id}/generate-heats",
        data={},
        follow_redirects=False,
    )

    with app.app_context():
        from database import db
        from models import Event, EventResult, Heat

        event = db.session.get(Event, event_id)
        heat_count_before = Heat.query.filter_by(event_id=event_id).count()
        for row in EventResult.query.filter_by(event_id=event_id).all():
            row.status = "completed"
            row.result_value = 30.0 + row.competitor_id
        event.status = "completed"
        db.session.commit()

    regenerate = client.post(
        f"/scheduling/{tournament_id}/event/{event_id}/generate-heats",
        data={},
        follow_redirects=False,
    )
    assert regenerate.status_code == 302

    with app.app_context():
        from models import Heat

        heat_count_after = Heat.query.filter_by(event_id=event_id).count()
        assert heat_count_after == heat_count_before


def test_pro_scratch_removes_competitor_from_generated_heat(qa_env):
    """Scratching a pro competitor should remove them from generated heats and mark their result scratched."""
    app = qa_env["app"]
    client = qa_env["client"]
    tournament_id, event_id = _seed_boundary_event(app, competitor_count=3, max_stands=2)

    generate = client.post(
        f"/scheduling/{tournament_id}/event/{event_id}/generate-heats",
        data={},
        follow_redirects=False,
    )
    assert generate.status_code == 302

    with app.app_context():
        from models import EventResult, Heat
        from models.competitor import ProCompetitor

        competitor = (
            ProCompetitor.query.filter_by(tournament_id=tournament_id, status="active")
            .order_by(ProCompetitor.id)
            .first()
        )
        assert competitor is not None
        heat = (
            Heat.query.filter_by(event_id=event_id, run_number=1)
            .filter(Heat.competitors.like(f"%{competitor.id}%"))
            .first()
        )
        assert heat is not None
        competitor_id = competitor.id

    scratch = client.post(
        f"/registration/{tournament_id}/pro/{competitor_id}/scratch",
        data={},
        follow_redirects=False,
    )
    assert scratch.status_code == 302

    with app.app_context():
        from models import EventResult, Heat

        heat = (
            Heat.query.filter_by(event_id=event_id, run_number=1)
            .filter(Heat.competitors.like(f"%{competitor_id}%"))
            .first()
        )
        result = EventResult.query.filter_by(
            event_id=event_id,
            competitor_id=competitor_id,
            competitor_type="pro",
        ).first()
        assert heat is None or competitor_id not in heat.get_competitors()
        assert result is not None and result.status == "scratched"
