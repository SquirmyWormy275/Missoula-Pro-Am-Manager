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
# NOTE: `TEST_USE_CREATE_ALL` is set inside the `app` fixture, NOT at module
# import, because pytest collects (imports) every test file before running any
# tests — a module-level `os.environ[...] = "1"` would leak to every test that
# runs before this module's teardown fixture fires, breaking tests in
# test_api_endpoints and test_model_json_safety which expect `flask db upgrade`.


# ---------------------------------------------------------------------------
# App + DB fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def app():
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    db_path = tmp.name
    tmp.close()

    old_url = os.environ.get("DATABASE_URL")
    old_create_all = os.environ.get("TEST_USE_CREATE_ALL")
    os.environ["DATABASE_URL"] = f"sqlite:///{db_path}"
    os.environ["TEST_USE_CREATE_ALL"] = "1"

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
        if old_create_all is None:
            os.environ.pop("TEST_USE_CREATE_ALL", None)
        else:
            os.environ["TEST_USE_CREATE_ALL"] = old_create_all
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
    from models.competitor import CollegeCompetitor
    from models.team import Team

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


# ---------------------------------------------------------------------------
# Stock Saw stand alternation after scratch cascade
# ---------------------------------------------------------------------------


class TestStockSawRebalanceAfterCascadeScratch:
    """Regression: scratching a pair-heat member via the scratch cascade
    (`/scoring/.../scratch`) used to leave the survivor stuck on whatever stand
    they started on. On college Men's Stock Saw, every scratched partner
    happened to be the stand-7 seat, so six consecutive solos all parked on
    stand 8 in the printed Friday schedule.

    V2.14.13 wired rebalance into `routes/scheduling/heats.scratch_competitor`
    but NOT into this cascade service. This guard ensures the cascade path
    also re-alternates 7/8 across surviving solos.
    """

    def _seed_stock_saw_with_pair_heats(self, app, num_pairs=6):
        """Create a college Men's Stock Saw event with `num_pairs` pair heats.

        Each heat: two male competitors on stands [7, 8] in order. Returns
        (tournament, event, list_of_scratched_victims_from_stand_7).
        """
        import json as _json

        from database import db
        from models.competitor import CollegeCompetitor
        from models.event import Event, EventResult
        from models.heat import Heat
        from models.team import Team
        from models.tournament import Tournament

        t = Tournament(name="StockSawRebalance 2026", year=2026, status="active")
        db.session.add(t)
        db.session.flush()

        team = Team(
            tournament_id=t.id,
            team_code="CSU-A",
            school_name="CSU",
            school_abbreviation="CSU",
        )
        db.session.add(team)
        db.session.flush()

        ev = Event(
            tournament_id=t.id,
            name="Stock Saw",
            event_type="college",
            gender="M",
            scoring_type="time",
            scoring_order="lowest_wins",
            stand_type="stock_saw",
            max_stands=2,
            status="pending",
        )
        db.session.add(ev)
        db.session.flush()

        stand_7_victims = []
        for heat_num in range(1, num_pairs + 1):
            c7 = CollegeCompetitor(
                tournament_id=t.id,
                team_id=team.id,
                name=f"Stand7_{heat_num}",
                gender="M",
                events_entered=_json.dumps(["Stock Saw"]),
                status="active",
            )
            c8 = CollegeCompetitor(
                tournament_id=t.id,
                team_id=team.id,
                name=f"Stand8_{heat_num}",
                gender="M",
                events_entered=_json.dumps(["Stock Saw"]),
                status="active",
            )
            db.session.add_all([c7, c8])
            db.session.flush()
            stand_7_victims.append(c7)

            h = Heat(
                event_id=ev.id,
                heat_number=heat_num,
                run_number=1,
                competitors=_json.dumps([c7.id, c8.id]),
                stand_assignments=_json.dumps({str(c7.id): 7, str(c8.id): 8}),
            )
            db.session.add(h)
            # Add EventResult rows so compute_scratch_effects() sees them.
            db.session.add(
                EventResult(
                    event_id=ev.id,
                    competitor_id=c7.id,
                    competitor_type="college",
                    competitor_name=c7.name,
                    status="pending",
                )
            )
            db.session.add(
                EventResult(
                    event_id=ev.id,
                    competitor_id=c8.id,
                    competitor_type="college",
                    competitor_name=c8.name,
                    status="pending",
                )
            )
        db.session.flush()
        return t, ev, stand_7_victims

    def test_cascade_scratch_triggers_stock_saw_rebalance(self, app):
        """Exact race-day reproduction: six pair heats all with stands [7, 8];
        scratch the stand-7 seat of every heat via execute_cascade; assert
        surviving solos alternate 7, 8, 7, 8, 7, 8 across heats 1-6.
        Without the rebalance hook, all six survivors stay on stand 8."""
        from database import db
        from models.heat import Heat
        from services.scratch_cascade import (
            compute_scratch_effects,
            execute_cascade,
        )

        with app.app_context():
            t, ev, victims = self._seed_stock_saw_with_pair_heats(app, num_pairs=6)
            db.session.commit()

            for victim in victims:
                effects = compute_scratch_effects(victim, t)
                execute_cascade(victim, effects, judge_user_id=1, tournament=t)
            db.session.commit()

            heats = (
                Heat.query.filter_by(event_id=ev.id, run_number=1)
                .order_by(Heat.heat_number)
                .all()
            )
            solo_stands = []
            for h in heats:
                assignments = h.get_stand_assignments()
                assert len(assignments) == 1, (
                    f"heat {h.heat_number} should be solo after scratch; "
                    f"got {assignments}"
                )
                (stand,) = assignments.values()
                solo_stands.append(int(stand))

            # Guard against the exact race-day printout: [8, 8, 8, 8, 8, 8].
            assert solo_stands != [8, 8, 8, 8, 8, 8], (
                "cascade scratch left every survivor on stand 8 — rebalance "
                "was not applied"
            )
            # Consecutive heats must use different stands.
            for i in range(1, len(solo_stands)):
                assert solo_stands[i] != solo_stands[i - 1], (
                    f"solos {i} and {i+1} on same stand {solo_stands[i]}; "
                    f"full list: {solo_stands}"
                )
            # Only stands 7 and 8 allowed.
            assert set(solo_stands) <= {7, 8}

    def test_cascade_scratch_unrelated_event_untouched(self, app):
        """Rebalance must be scoped — scratching a competitor from a non-Stock
        Saw event (e.g. Underhand) should NOT touch any Stock Saw heat.
        Regression guard for accidental over-scoping if the rebalance filter
        ever gets weakened."""
        import json as _json

        from database import db
        from models.competitor import CollegeCompetitor
        from models.event import Event, EventResult
        from models.heat import Heat
        from models.team import Team
        from models.tournament import Tournament
        from services.scratch_cascade import (
            compute_scratch_effects,
            execute_cascade,
        )

        with app.app_context():
            t = Tournament(name="Scoped 2026", year=2026, status="active")
            db.session.add(t)
            db.session.flush()
            team = Team(
                tournament_id=t.id,
                team_code="CSU-A",
                school_name="CSU",
                school_abbreviation="CSU",
            )
            db.session.add(team)
            db.session.flush()

            # Set up an unrelated underhand event + an independent stock saw
            # event. Scratch happens on underhand; stock saw heats must be
            # byte-for-byte identical afterward.
            underhand = Event(
                tournament_id=t.id,
                name="Underhand Hard Hit",
                event_type="college",
                gender="M",
                scoring_type="hits",
                scoring_order="highest_wins",
                stand_type="underhand",
                status="pending",
            )
            stock_saw = Event(
                tournament_id=t.id,
                name="Stock Saw",
                event_type="college",
                gender="M",
                scoring_type="time",
                scoring_order="lowest_wins",
                stand_type="stock_saw",
                max_stands=2,
                status="pending",
            )
            db.session.add_all([underhand, stock_saw])
            db.session.flush()

            # Two competitors in underhand only.
            uh1 = CollegeCompetitor(
                tournament_id=t.id, team_id=team.id, name="UH_Victim", gender="M",
                events_entered=_json.dumps(["Underhand Hard Hit"]),
                status="active",
            )
            uh2 = CollegeCompetitor(
                tournament_id=t.id, team_id=team.id, name="UH_Bystander", gender="M",
                events_entered=_json.dumps(["Underhand Hard Hit"]),
                status="active",
            )
            db.session.add_all([uh1, uh2])
            db.session.flush()
            db.session.add_all([
                EventResult(event_id=underhand.id, competitor_id=uh1.id,
                            competitor_type="college", competitor_name=uh1.name,
                            status="pending"),
                EventResult(event_id=underhand.id, competitor_id=uh2.id,
                            competitor_type="college", competitor_name=uh2.name,
                            status="pending"),
            ])
            db.session.add(Heat(
                event_id=underhand.id, heat_number=1, run_number=1,
                competitors=_json.dumps([uh1.id, uh2.id]),
                stand_assignments=_json.dumps({str(uh1.id): 1, str(uh2.id): 2}),
            ))

            # Independent pair of stock saw competitors in one heat on [7, 8].
            # These competitors are NOT entered in Underhand, so the scratch
            # cascade on uh1 must not touch their heat.
            ss1 = CollegeCompetitor(
                tournament_id=t.id, team_id=team.id, name="SS_A", gender="M",
                events_entered=_json.dumps(["Stock Saw"]),
                status="active",
            )
            ss2 = CollegeCompetitor(
                tournament_id=t.id, team_id=team.id, name="SS_B", gender="M",
                events_entered=_json.dumps(["Stock Saw"]),
                status="active",
            )
            db.session.add_all([ss1, ss2])
            db.session.flush()
            db.session.add_all([
                EventResult(event_id=stock_saw.id, competitor_id=ss1.id,
                            competitor_type="college", competitor_name=ss1.name,
                            status="pending"),
                EventResult(event_id=stock_saw.id, competitor_id=ss2.id,
                            competitor_type="college", competitor_name=ss2.name,
                            status="pending"),
            ])
            ss_heat_id = db.session.add(Heat(
                event_id=stock_saw.id, heat_number=1, run_number=1,
                competitors=_json.dumps([ss1.id, ss2.id]),
                stand_assignments=_json.dumps({str(ss1.id): 7, str(ss2.id): 8}),
            ))
            db.session.flush()
            db.session.commit()

            effects = compute_scratch_effects(uh1, t)
            execute_cascade(uh1, effects, judge_user_id=1, tournament=t)
            db.session.commit()

            # Stock saw heat must be unchanged.
            ss_heat = (
                Heat.query.filter_by(event_id=stock_saw.id, run_number=1)
                .order_by(Heat.heat_number)
                .first()
            )
            assert ss_heat is not None
            assert ss_heat.get_competitors() == [ss1.id, ss2.id]
            assert ss_heat.get_stand_assignments() == {
                str(ss1.id): 7,
                str(ss2.id): 8,
            }
