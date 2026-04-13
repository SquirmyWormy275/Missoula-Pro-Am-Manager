"""
Unit tests for services/scratch_cascade.py — compute_scratch_effects().

Uses in-memory SQLite via TEST_USE_CREATE_ALL=1 so we avoid the slow
migration stack for a pure-service unit test.
"""

import json
import os
import tempfile

import pytest

os.environ.setdefault("SECRET_KEY", "test-scratch-cascade")
os.environ.setdefault("WTF_CSRF_ENABLED", "False")
os.environ.setdefault("TEST_USE_CREATE_ALL", "1")


# ---------------------------------------------------------------------------
# App + DB fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def app():
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    db_path = tmp.name
    tmp.close()

    old_url = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = f"sqlite:///{db_path}"

    try:
        from app import create_app

        _app = create_app()
        _app.config.update(
            {
                "TESTING": True,
                "SQLALCHEMY_DATABASE_URI": f"sqlite:///{db_path}",
                "WTF_CSRF_ENABLED": False,
                "WTF_CSRF_CHECK_DEFAULT": False,
            }
        )

        from database import db as _db

        with _app.app_context():
            _db.create_all()

        yield _app

        with _app.app_context():
            _db.session.remove()

    finally:
        if old_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = old_url
        try:
            os.unlink(db_path)
        except OSError:
            pass


@pytest.fixture(autouse=True)
def clean_db(app):
    """Roll back all data between tests."""
    from database import db as _db

    with app.app_context():
        yield
        _db.session.remove()
        # Truncate all tables by deleting rows
        for table in reversed(_db.metadata.sorted_tables):
            _db.session.execute(table.delete())
        _db.session.commit()


# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------


def _seed_base(db, tournament_name="Test Tournament 2026"):
    """Create a tournament and return it."""
    from models.tournament import Tournament

    t = Tournament(name=tournament_name, year=2026, status="active")
    db.session.add(t)
    db.session.flush()
    return t


def _seed_pro(db, tournament, name="Alice Pro", gender="F", status="active"):
    from models.competitor import ProCompetitor

    c = ProCompetitor(
        tournament_id=tournament.id,
        name=name,
        gender=gender,
        status=status,
    )
    db.session.add(c)
    db.session.flush()
    return c


def _seed_college(db, tournament, name="Bob College", gender="M", status="active"):
    from models.team import Team
    from models.competitor import CollegeCompetitor

    team = Team.query.filter_by(tournament_id=tournament.id).first()
    if team is None:
        team = Team(
            tournament_id=tournament.id,
            school_name="University of Montana",
            school_abbreviation="UM",
            team_code="UM-A",
        )
        db.session.add(team)
        db.session.flush()
    c = CollegeCompetitor(
        tournament_id=tournament.id,
        team_id=team.id,
        name=name,
        gender=gender,
        status=status,
    )
    db.session.add(c)
    db.session.flush()
    return c


def _seed_event(
    db,
    tournament,
    name="Underhand",
    event_type="pro",
    scoring_type="time",
    is_finalized=False,
    status="pending",
):
    from models.event import Event

    e = Event(
        tournament_id=tournament.id,
        name=name,
        event_type=event_type,
        scoring_type=scoring_type,
        is_finalized=is_finalized,
        status=status,
        scoring_order="lowest_wins",
    )
    db.session.add(e)
    db.session.flush()
    return e


def _seed_result(
    db,
    event,
    competitor,
    comp_type="pro",
    heat_number=1,
    result_status="pending",
    partner_name=None,
):
    from models.event import EventResult

    r = EventResult(
        event_id=event.id,
        competitor_id=competitor.id,
        competitor_type=comp_type,
        competitor_name=competitor.name,
        status=result_status,
        partner_name=partner_name,
    )
    db.session.add(r)
    db.session.flush()
    return r


def _make_relay_state(teams):
    """Return JSON string for event_state with relay teams."""
    return json.dumps({"status": "drawn", "teams": teams})


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestHappyPathSingleEvent:
    """Competitor in one event → one event_result effect."""

    def test_one_event_result_effect(self, app):
        from database import db
        from services.scratch_cascade import compute_scratch_effects

        with app.app_context():
            t = _seed_base(db)
            comp = _seed_pro(db, t, name="Carol")
            event = _seed_event(db, t, name="Underhand")
            _seed_result(db, event, comp, comp_type="pro", result_status="pending")
            db.session.commit()

            effects = compute_scratch_effects(comp, t)

        assert len(effects) == 1
        assert effects[0].effect_type == "event_result"
        assert "Underhand" in effects[0].description
        assert effects[0].affected_entity_id is not None
        assert effects[0].affected_entity_type == "event_result"


class TestHappyPathPartner:
    """Competitor with partner reference → event_result + partner effects."""

    def test_partner_effect_included(self, app):
        from database import db
        from services.scratch_cascade import compute_scratch_effects

        with app.app_context():
            t = _seed_base(db)
            comp = _seed_pro(db, t, name="Dana")
            partner = _seed_pro(db, t, name="Eve", gender="F")
            event = _seed_event(db, t, name="Axe Throw")
            # comp's result references Eve as partner
            _seed_result(
                db,
                event,
                comp,
                comp_type="pro",
                result_status="pending",
                partner_name="Eve",
            )
            # Eve's result references Dana as partner
            _seed_result(
                db,
                event,
                partner,
                comp_type="pro",
                result_status="pending",
                partner_name="Dana",
            )
            db.session.commit()

            effects = compute_scratch_effects(comp, t)

        types = [e.effect_type for e in effects]
        assert "event_result" in types
        assert "partner" in types
        partner_effects = [e for e in effects if e.effect_type == "partner"]
        assert len(partner_effects) == 1
        assert (
            "Eve" in partner_effects[0].description
            or "Dana" in partner_effects[0].description
        )


class TestHappyPathRelayTeam:
    """Competitor on relay team → relay_team effect."""

    def test_relay_team_effect(self, app):
        from database import db
        from services.scratch_cascade import compute_scratch_effects

        with app.app_context():
            t = _seed_base(db)
            comp = _seed_pro(db, t, name="Frank")
            relay_event = _seed_event(
                db, t, name="Pro-Am Relay", event_type="pro", scoring_type="time"
            )
            teams = [
                {
                    "team_number": 1,
                    "name": "Team 1",
                    "pro_members": [{"id": comp.id, "name": "Frank", "gender": "M"}],
                    "college_members": [],
                }
            ]
            relay_event.event_state = _make_relay_state(teams)
            db.session.commit()

            effects = compute_scratch_effects(comp, t)

        assert any(e.effect_type == "relay_team" for e in effects)
        relay_effects = [e for e in effects if e.effect_type == "relay_team"]
        assert (
            "1" in relay_effects[0].description
            or "Team 1" in relay_effects[0].description
        )


class TestHappyPathAllEffectTypes:
    """Competitor in 3 events + relay + partner → all effect types present."""

    def test_all_effect_types(self, app):
        from database import db
        from services.scratch_cascade import compute_scratch_effects

        with app.app_context():
            t = _seed_base(db)
            comp = _seed_pro(db, t, name="George")
            partner = _seed_pro(db, t, name="Hannah", gender="F")

            # 3 regular events
            e1 = _seed_event(db, t, name="Underhand", is_finalized=True)
            e2 = _seed_event(db, t, name="Standing Block", is_finalized=False)
            e3 = _seed_event(db, t, name="Axe Throw", is_finalized=True)
            _seed_result(db, e1, comp, comp_type="pro", result_status="completed")
            _seed_result(db, e2, comp, comp_type="pro", result_status="pending")
            _seed_result(
                db,
                e3,
                comp,
                comp_type="pro",
                result_status="pending",
                partner_name="Hannah",
            )
            # Hannah references George as partner
            _seed_result(
                db,
                e3,
                partner,
                comp_type="pro",
                result_status="pending",
                partner_name="George",
            )

            # Relay event
            relay_event = _seed_event(db, t, name="Pro-Am Relay", event_type="pro")
            teams = [
                {
                    "team_number": 2,
                    "name": "Team 2",
                    "pro_members": [{"id": comp.id, "name": "George", "gender": "M"}],
                    "college_members": [],
                }
            ]
            relay_event.event_state = _make_relay_state(teams)
            db.session.commit()

            effects = compute_scratch_effects(comp, t)

        types = {e.effect_type for e in effects}
        assert "event_result" in types
        assert "partner" in types
        assert "relay_team" in types
        assert "standings" in types

        # 3 event_result effects
        assert len([e for e in effects if e.effect_type == "event_result"]) == 3


class TestEdgeCaseNoEvents:
    """Competitor with no events → empty list."""

    def test_no_events_returns_empty(self, app):
        from database import db
        from services.scratch_cascade import compute_scratch_effects

        with app.app_context():
            t = _seed_base(db)
            comp = _seed_pro(db, t, name="Iris")
            db.session.commit()

            effects = compute_scratch_effects(comp, t)

        assert effects == []


class TestEdgeCaseAlreadyScratched:
    """Competitor already scratched → returns empty list (no active results)."""

    def test_scratched_competitor_no_effects(self, app):
        from database import db
        from services.scratch_cascade import compute_scratch_effects

        with app.app_context():
            t = _seed_base(db)
            comp = _seed_pro(db, t, name="Jack", status="scratched")
            event = _seed_event(db, t, name="Underhand")
            # Result is already scratched — should not appear
            _seed_result(db, event, comp, comp_type="pro", result_status="scratched")
            db.session.commit()

            effects = compute_scratch_effects(comp, t)

        assert effects == []


class TestEdgeCaseTournamentMismatch:
    """competitor.tournament_id != tournament.id → raises ValueError."""

    def test_tournament_id_mismatch_raises(self, app):
        from database import db
        from services.scratch_cascade import compute_scratch_effects

        with app.app_context():
            t1 = _seed_base(db, tournament_name="Tournament One")
            t2 = _seed_base(db, tournament_name="Tournament Two")
            comp = _seed_pro(db, t1, name="Kim")
            db.session.commit()

            with pytest.raises(ValueError, match="tournament"):
                compute_scratch_effects(comp, t2)


class TestEdgeCasePartnerAlreadyScratched:
    """Partner already scratched → partner effect description notes it."""

    def test_partner_scratched_noted_in_description(self, app):
        from database import db
        from services.scratch_cascade import compute_scratch_effects

        with app.app_context():
            t = _seed_base(db)
            comp = _seed_pro(db, t, name="Leo")
            partner = _seed_pro(db, t, name="Mia", status="scratched")
            event = _seed_event(db, t, name="Partnered Axe Throw")
            _seed_result(
                db,
                event,
                comp,
                comp_type="pro",
                result_status="pending",
                partner_name="Mia",
            )
            # Mia's result is already scratched — she's the partner
            _seed_result(db, event, partner, comp_type="pro", result_status="scratched")
            db.session.commit()

            effects = compute_scratch_effects(comp, t)

        partner_effects = [e for e in effects if e.effect_type == "partner"]
        assert len(partner_effects) == 1
        assert "scratched" in partner_effects[0].description.lower()


class TestCollegeCompetitorRelayMember:
    """College competitor on relay college_members → relay_team effect."""

    def test_college_relay_effect(self, app):
        from database import db
        from services.scratch_cascade import compute_scratch_effects

        with app.app_context():
            t = _seed_base(db)
            comp = _seed_college(db, t, name="Nina", gender="F")
            relay_event = _seed_event(db, t, name="Pro-Am Relay", event_type="pro")
            teams = [
                {
                    "team_number": 3,
                    "name": "Team 3",
                    "pro_members": [],
                    "college_members": [{"id": comp.id, "name": "Nina", "gender": "F"}],
                }
            ]
            relay_event.event_state = _make_relay_state(teams)
            db.session.commit()

            effects = compute_scratch_effects(comp, t)

        assert any(e.effect_type == "relay_team" for e in effects)


class TestFinalizedEventsStandingsEffect:
    """Any finalized event → standings effect included."""

    def test_standings_effect_when_finalized_event(self, app):
        from database import db
        from services.scratch_cascade import compute_scratch_effects

        with app.app_context():
            t = _seed_base(db)
            comp = _seed_pro(db, t, name="Oscar")
            event = _seed_event(db, t, name="Speed Climb", is_finalized=True)
            _seed_result(db, event, comp, comp_type="pro", result_status="completed")
            db.session.commit()

            effects = compute_scratch_effects(comp, t)

        assert any(e.effect_type == "standings" for e in effects)
        standings = [e for e in effects if e.effect_type == "standings"]
        assert "1" in standings[0].description  # "1 finalized events"


class TestNoStandingsEffectWhenNotFinalized:
    """No finalized events → no standings effect."""

    def test_no_standings_effect_when_not_finalized(self, app):
        from database import db
        from services.scratch_cascade import compute_scratch_effects

        with app.app_context():
            t = _seed_base(db)
            comp = _seed_pro(db, t, name="Penny")
            event = _seed_event(db, t, name="Underhand", is_finalized=False)
            _seed_result(db, event, comp, comp_type="pro", result_status="pending")
            db.session.commit()

            effects = compute_scratch_effects(comp, t)

        assert not any(e.effect_type == "standings" for e in effects)


# ---------------------------------------------------------------------------
# Tests for execute_cascade() and reverse_cascade()
# ---------------------------------------------------------------------------


def _seed_result_with_points(
    db,
    event,
    competitor,
    comp_type="pro",
    result_status="pending",
    points=0.0,
    payout=0.0,
    final_position=None,
):
    from models.event import EventResult

    r = EventResult(
        event_id=event.id,
        competitor_id=competitor.id,
        competitor_type=comp_type,
        competitor_name=competitor.name,
        status=result_status,
        points_awarded=points,
        payout_amount=payout,
        final_position=final_position,
    )
    db.session.add(r)
    db.session.flush()
    return r


class TestExecuteCascadeHappyPath:
    """execute_cascade() sets competitor status to scratched."""

    def test_competitor_status_set_to_scratched(self, app):
        from database import db
        from services.scratch_cascade import compute_scratch_effects, execute_cascade

        with app.app_context():
            t = _seed_base(db)
            comp = _seed_pro(db, t, name="TestExec1")
            event = _seed_event(db, t, name="Underhand")
            _seed_result(db, event, comp, comp_type="pro", result_status="pending")
            db.session.commit()

            effects = compute_scratch_effects(comp, t)
            result = execute_cascade(comp, effects, judge_user_id=1, tournament=t)
            db.session.commit()

            assert result["success"] is True
            assert comp.status == "scratched"


class TestExecuteCascadeResultStatuses:
    """execute_cascade() sets event result statuses to scratched and zeros points."""

    def test_event_results_scratched_and_zeroed(self, app):
        from database import db
        from models.event import EventResult
        from services.scratch_cascade import compute_scratch_effects, execute_cascade

        with app.app_context():
            t = _seed_base(db)
            comp = _seed_pro(db, t, name="TestExec2")
            event = _seed_event(db, t, name="Speed Climb", is_finalized=True)
            r = _seed_result_with_points(
                db, event, comp, comp_type="pro",
                result_status="completed", points=7.0, payout=50.0, final_position=2
            )
            result_id = r.id
            db.session.commit()

            effects = compute_scratch_effects(comp, t)
            execute_cascade(comp, effects, judge_user_id=1, tournament=t)
            db.session.commit()

            updated = EventResult.query.get(result_id)
            assert updated.status == "scratched"
            assert updated.points_awarded == 0
            assert updated.payout_amount == 0


class TestExecuteCascadeAuditLog:
    """execute_cascade() logs audit entry with scratch_snapshot."""

    def test_audit_log_created_with_snapshot(self, app):
        from database import db
        from models.audit_log import AuditLog
        from services.scratch_cascade import compute_scratch_effects, execute_cascade

        with app.app_context():
            t = _seed_base(db)
            comp = _seed_pro(db, t, name="TestExec3")
            event = _seed_event(db, t, name="Axe Throw")
            _seed_result(db, event, comp, comp_type="pro", result_status="pending")
            db.session.commit()

            effects = compute_scratch_effects(comp, t)
            execute_cascade(comp, effects, judge_user_id=1, tournament=t)
            db.session.commit()

            entry = (
                AuditLog.query
                .filter_by(action="competitor_scratched")
                .order_by(AuditLog.id.desc())
                .first()
            )
            assert entry is not None
            details = json.loads(entry.details_json)
            assert "scratch_snapshot" in details
            assert details["scratch_snapshot"]["competitor_status"] is not None


class TestExecuteCascadeEmptyEffects:
    """execute_cascade() with empty effects list only sets competitor status."""

    def test_empty_effects_only_sets_competitor_scratched(self, app):
        from database import db
        from services.scratch_cascade import execute_cascade

        with app.app_context():
            t = _seed_base(db)
            comp = _seed_pro(db, t, name="TestExec4")
            db.session.commit()

            result = execute_cascade(comp, [], judge_user_id=1, tournament=t)
            db.session.commit()

            assert result["success"] is True
            assert comp.status == "scratched"
            assert result["effects_applied"] == 0


class TestReverseCascadeHappyPath:
    """reverse_cascade() within 30 min restores all state."""

    def test_reverse_restores_competitor_and_results(self, app):
        from database import db
        from models.event import EventResult
        from services.scratch_cascade import (
            compute_scratch_effects,
            execute_cascade,
            reverse_cascade,
        )

        with app.app_context():
            t = _seed_base(db)
            comp = _seed_pro(db, t, name="TestRev1")
            event = _seed_event(db, t, name="Underhand")
            r = _seed_result_with_points(
                db, event, comp, comp_type="pro",
                result_status="pending", points=5.0, payout=0.0, final_position=3
            )
            result_id = r.id
            original_status = comp.status
            db.session.commit()

            effects = compute_scratch_effects(comp, t)
            execute_cascade(comp, effects, judge_user_id=1, tournament=t)
            db.session.commit()

            assert comp.status == "scratched"

            undo = reverse_cascade(comp.id, judge_user_id=1, tournament=t)
            db.session.commit()

            assert undo["success"] is True
            assert comp.status == original_status
            restored = EventResult.query.get(result_id)
            assert restored.status == "pending"


class TestReverseCascadeExpiredWindow:
    """reverse_cascade() after 30 min returns error."""

    def test_expired_window_returns_error(self, app):
        from datetime import datetime, timedelta
        from database import db
        from models.audit_log import AuditLog
        from services.scratch_cascade import (
            compute_scratch_effects,
            execute_cascade,
            reverse_cascade,
        )

        with app.app_context():
            t = _seed_base(db)
            comp = _seed_pro(db, t, name="TestRev2")
            event = _seed_event(db, t, name="Speed Climb")
            _seed_result(db, event, comp, comp_type="pro", result_status="pending")
            db.session.commit()

            effects = compute_scratch_effects(comp, t)
            execute_cascade(comp, effects, judge_user_id=1, tournament=t)
            db.session.commit()

            # Back-date the audit log entry so it's outside the undo window
            entry = (
                AuditLog.query
                .filter_by(action="competitor_scratched")
                .order_by(AuditLog.id.desc())
                .first()
            )
            entry.created_at = datetime.utcnow() - timedelta(minutes=31)
            db.session.commit()

            undo = reverse_cascade(comp.id, judge_user_id=1, tournament=t)
            assert undo["success"] is False
            assert "expired" in undo["message"].lower()


class TestReverseCascadeNoEntry:
    """reverse_cascade() with no audit entry returns 'No scratch to undo'."""

    def test_no_audit_entry_returns_error(self, app):
        from database import db
        from services.scratch_cascade import reverse_cascade

        with app.app_context():
            t = _seed_base(db)
            comp = _seed_pro(db, t, name="TestRev3")
            db.session.commit()

            undo = reverse_cascade(comp.id, judge_user_id=1, tournament=t)
            assert undo["success"] is False
            assert "no scratch" in undo["message"].lower()


class TestExecuteReverseRoundTrip:
    """Integration: execute + reverse leaves DB in original state."""

    def test_round_trip_restores_full_state(self, app):
        from database import db
        from models.competitor import ProCompetitor
        from models.event import EventResult
        from services.scratch_cascade import (
            compute_scratch_effects,
            execute_cascade,
            reverse_cascade,
        )

        with app.app_context():
            t = _seed_base(db)
            comp = _seed_pro(db, t, name="TestRound1")
            e1 = _seed_event(db, t, name="Underhand")
            e2 = _seed_event(db, t, name="Springboard")
            r1 = _seed_result_with_points(
                db, e1, comp, comp_type="pro",
                result_status="pending", points=3.0, payout=0.0, final_position=4
            )
            r2 = _seed_result_with_points(
                db, e2, comp, comp_type="pro",
                result_status="completed", points=7.0, payout=100.0, final_position=2
            )
            r1_id, r2_id = r1.id, r2.id
            original_comp_status = comp.status
            original_r1_status = r1.status
            original_r2_status = r2.status
            db.session.commit()

            effects = compute_scratch_effects(comp, t)
            execute_cascade(comp, effects, judge_user_id=1, tournament=t)
            db.session.commit()

            reverse_cascade(comp.id, judge_user_id=1, tournament=t)
            db.session.commit()

            final_comp = ProCompetitor.query.get(comp.id)
            assert final_comp.status == original_comp_status

            final_r1 = EventResult.query.get(r1_id)
            final_r2 = EventResult.query.get(r2_id)
            assert final_r1.status == original_r1_status
            assert final_r2.status == original_r2_status
