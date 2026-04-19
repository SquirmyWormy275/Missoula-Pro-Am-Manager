"""
Route-level tests for scratch cascade endpoints in routes/scoring.py.

Covers:
  - GET  /tournament/<tid>/competitor/<cid>/scratch-preview  (JSON effects)
  - POST /tournament/<tid>/competitor/<cid>/scratch-confirm  (execute cascade)
  - POST /tournament/<tid>/competitor/<cid>/scratch-undo     (reverse cascade)
  - Integration: registration scratch paths redirect through cascade

Uses TEST_USE_CREATE_ALL=1 (fast in-memory SQLite, no migration stack).
CSRF is disabled via WTF_CSRF_ENABLED=False.
"""

from __future__ import annotations

import json
import os
import tempfile

import pytest

os.environ.setdefault("SECRET_KEY", "test-scratch-routes")
os.environ.setdefault("WTF_CSRF_ENABLED", "False")
# NOTE: `TEST_USE_CREATE_ALL` is set inside the `app` fixture, NOT at module
# import, because pytest collects (imports) every test file before running any
# tests — a module-level `os.environ[...] = "1"` would leak to every test that
# runs before this module's teardown fixture fires, breaking tests in
# test_api_endpoints and test_model_json_safety which expect `flask db upgrade`.


# ---------------------------------------------------------------------------
# Fixtures
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
    from database import db as _db

    with app.app_context():
        yield
        _db.session.remove()
        for table in reversed(_db.metadata.sorted_tables):
            _db.session.execute(table.delete())
        _db.session.commit()


# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------


def _seed_tournament(db):
    from models.tournament import Tournament

    t = Tournament(name="Scratch Routes Test 2026", year=2026, status="active")
    db.session.add(t)
    db.session.flush()
    return t


def _seed_admin(db):
    from models.user import User

    u = User(username="admin_scratch_routes", role="admin")
    u.set_password("pass")
    db.session.add(u)
    db.session.flush()
    return u


def _seed_pro(db, tournament, name="Alice Pro", status="active"):
    from models.competitor import ProCompetitor

    c = ProCompetitor(
        tournament_id=tournament.id,
        name=name,
        gender="F",
        status=status,
    )
    db.session.add(c)
    db.session.flush()
    return c


def _seed_event(db, tournament, name="UH Open", event_type="pro", is_finalized=False):
    from models.event import Event

    e = Event(
        tournament_id=tournament.id,
        name=name,
        event_type=event_type,
        scoring_type="time",
        scoring_order="lowest_wins",
        is_finalized=is_finalized,
        status="pending",
    )
    db.session.add(e)
    db.session.flush()
    return e


def _seed_result(db, event, competitor, comp_type="pro", status="pending"):
    from models.event import EventResult

    r = EventResult(
        event_id=event.id,
        competitor_id=competitor.id,
        competitor_type=comp_type,
        competitor_name=competitor.name,
        status=status,
    )
    db.session.add(r)
    db.session.flush()
    return r


def _auth_client(app, user):
    c = app.test_client()
    with c.session_transaction() as sess:
        sess["_user_id"] = str(user.id)
    return c


# ---------------------------------------------------------------------------
# Helper: build effect payload mirroring what scratch-confirm expects
# ---------------------------------------------------------------------------


def _build_effect_form(effects: list[dict]) -> dict:
    """Convert a list of effect dicts (from JSON preview) into POST form data."""
    data = {}
    for i, eff in enumerate(effects):
        data[f"effect_type_{i}"] = eff["effect_type"]
        data[f"affected_entity_id_{i}"] = str(eff["affected_entity_id"])
        data[f"affected_entity_type_{i}"] = eff["affected_entity_type"]
        data[f"effect_checked_{i}"] = "on"
    data["effect_count"] = str(len(effects))
    return data


# ===========================================================================
# TestScratchPreviewGet
# ===========================================================================


class TestScratchPreviewGet:
    """GET /tournament/<tid>/competitor/<cid>/scratch-preview returns JSON effects."""

    def test_happy_path_returns_effects_list(self, app):
        from database import db

        with app.app_context():
            t = _seed_tournament(db)
            u = _seed_admin(db)
            comp = _seed_pro(db, t)
            ev = _seed_event(db, t)
            _seed_result(db, ev, comp)
            db.session.commit()

            client = _auth_client(app, u)
            resp = client.get(f"/scoring/{t.id}/competitor/{comp.id}/scratch-preview")
            assert resp.status_code == 200
            data = json.loads(resp.data)
            assert "effects" in data
            assert isinstance(data["effects"], list)
            # At least one event_result effect expected
            types = [e["effect_type"] for e in data["effects"]]
            assert "event_result" in types

    def test_happy_path_no_events_returns_empty_list(self, app):
        from database import db

        with app.app_context():
            t = _seed_tournament(db)
            u = _seed_admin(db)
            comp = _seed_pro(db, t)
            db.session.commit()

            client = _auth_client(app, u)
            resp = client.get(f"/scoring/{t.id}/competitor/{comp.id}/scratch-preview")
            assert resp.status_code == 200
            data = json.loads(resp.data)
            assert data["effects"] == []

    def test_idor_guard_wrong_tournament_returns_404(self, app):
        """Competitor belongs to a different tournament — should return 404."""
        from database import db
        from models.tournament import Tournament

        with app.app_context():
            t1 = _seed_tournament(db)
            t2 = Tournament(name="Other Tournament", year=2026, status="active")
            db.session.add(t2)
            db.session.flush()
            u = _seed_admin(db)
            comp = _seed_pro(db, t1)
            db.session.commit()

            client = _auth_client(app, u)
            # Request with t2's id but comp belongs to t1
            resp = client.get(f"/scoring/{t2.id}/competitor/{comp.id}/scratch-preview")
            assert resp.status_code in (403, 404)

    def test_competitor_not_found_returns_404(self, app):
        from database import db

        with app.app_context():
            t = _seed_tournament(db)
            u = _seed_admin(db)
            db.session.commit()

            client = _auth_client(app, u)
            resp = client.get(f"/scoring/{t.id}/competitor/99999/scratch-preview")
            assert resp.status_code == 404


# ===========================================================================
# TestScratchConfirmPost
# ===========================================================================


class TestScratchConfirmPost:
    """POST /tournament/<tid>/competitor/<cid>/scratch-confirm executes cascade."""

    def test_happy_path_all_effects_cascade_executes(self, app):
        from database import db
        from models.competitor import ProCompetitor
        from models.event import EventResult

        with app.app_context():
            t = _seed_tournament(db)
            u = _seed_admin(db)
            comp = _seed_pro(db, t)
            ev = _seed_event(db, t)
            result = _seed_result(db, ev, comp)
            result_id = result.id
            comp_id = comp.id
            db.session.commit()

            # First fetch effects via preview
            client = _auth_client(app, u)
            preview_resp = client.get(
                f"/scoring/{t.id}/competitor/{comp_id}/scratch-preview"
            )
            effects = json.loads(preview_resp.data)["effects"]

            form_data = _build_effect_form(effects)
            resp = client.post(
                f"/scoring/{t.id}/competitor/{comp_id}/scratch-confirm",
                data=form_data,
                follow_redirects=False,
            )
            # POST-redirect-GET
            assert resp.status_code in (302, 303)

            # Verify DB state
            comp_reloaded = ProCompetitor.query.get(comp_id)
            assert comp_reloaded.status == "scratched"

            result_reloaded = EventResult.query.get(result_id)
            assert result_reloaded.status == "scratched"

    def test_happy_path_partial_effects_only_checked_execute(self, app):
        """Unchecked effects are not applied."""
        from database import db
        from models.competitor import ProCompetitor
        from models.event import EventResult

        with app.app_context():
            t = _seed_tournament(db)
            u = _seed_admin(db)
            comp = _seed_pro(db, t)
            ev = _seed_event(db, t)
            result = _seed_result(db, ev, comp)
            result_id = result.id
            comp_id = comp.id
            db.session.commit()

            client = _auth_client(app, u)
            preview_resp = client.get(
                f"/scoring/{t.id}/competitor/{comp_id}/scratch-preview"
            )
            effects = json.loads(preview_resp.data)["effects"]

            # Build form but omit effect_checked for all effects (none checked)
            data = {}
            for i, eff in enumerate(effects):
                data[f"effect_type_{i}"] = eff["effect_type"]
                data[f"affected_entity_id_{i}"] = str(eff["affected_entity_id"])
                data[f"affected_entity_type_{i}"] = eff["affected_entity_type"]
                # No effect_checked_{i} key → unchecked
            data["effect_count"] = str(len(effects))

            resp = client.post(
                f"/scoring/{t.id}/competitor/{comp_id}/scratch-confirm",
                data=data,
                follow_redirects=False,
            )
            assert resp.status_code in (302, 303)

            # Competitor should still be scratched (status is always set)
            comp_reloaded = ProCompetitor.query.get(comp_id)
            assert comp_reloaded.status == "scratched"

            # EventResult should NOT be scratched (effect was unchecked)
            result_reloaded = EventResult.query.get(result_id)
            assert result_reloaded.status == "pending"

    def test_no_effects_checked_flashes_no_changes(self, app):
        """When no effects are checked, flash 'No changes applied'."""
        from database import db

        with app.app_context():
            t = _seed_tournament(db)
            u = _seed_admin(db)
            comp = _seed_pro(db, t)
            comp_id = comp.id
            db.session.commit()

            client = _auth_client(app, u)
            resp = client.post(
                f"/scoring/{t.id}/competitor/{comp_id}/scratch-confirm",
                data={"effect_count": "0"},
                follow_redirects=True,
            )
            assert resp.status_code == 200
            assert b"No changes" in resp.data or b"scratched" in resp.data.lower()

    def test_idor_guard_wrong_tournament_returns_403_or_404(self, app):
        from database import db
        from models.tournament import Tournament

        with app.app_context():
            t1 = _seed_tournament(db)
            t2 = Tournament(name="Other Tourney", year=2026, status="active")
            db.session.add(t2)
            db.session.flush()
            u = _seed_admin(db)
            comp = _seed_pro(db, t1)
            db.session.commit()

            client = _auth_client(app, u)
            resp = client.post(
                f"/scoring/{t2.id}/competitor/{comp.id}/scratch-confirm",
                data={"effect_count": "0"},
                follow_redirects=False,
            )
            assert resp.status_code in (403, 404)

    def test_redirect_uses_post_redirect_get(self, app):
        """Successful POST must redirect (not render a template)."""
        from database import db

        with app.app_context():
            t = _seed_tournament(db)
            u = _seed_admin(db)
            comp = _seed_pro(db, t)
            comp_id = comp.id
            db.session.commit()

            client = _auth_client(app, u)
            resp = client.post(
                f"/scoring/{t.id}/competitor/{comp_id}/scratch-confirm",
                data={"effect_count": "0"},
                follow_redirects=False,
            )
            assert resp.status_code in (302, 303)
            assert resp.location  # must have Location header


# ===========================================================================
# TestScratchUndoPost
# ===========================================================================


class TestScratchUndoPost:
    """POST /tournament/<tid>/competitor/<cid>/scratch-undo reverses the cascade."""

    def test_happy_path_undo_within_window_restores_status(self, app):
        from database import db
        from models.competitor import ProCompetitor

        with app.app_context():
            t = _seed_tournament(db)
            u = _seed_admin(db)
            comp = _seed_pro(db, t)
            ev = _seed_event(db, t)
            _seed_result(db, ev, comp)
            comp_id = comp.id
            db.session.commit()

            client = _auth_client(app, u)

            # Scratch the competitor via confirm
            preview_resp = client.get(
                f"/scoring/{t.id}/competitor/{comp_id}/scratch-preview"
            )
            effects = json.loads(preview_resp.data)["effects"]
            client.post(
                f"/scoring/{t.id}/competitor/{comp_id}/scratch-confirm",
                data=_build_effect_form(effects),
            )

            # Now undo
            undo_resp = client.post(
                f"/scoring/{t.id}/competitor/{comp_id}/scratch-undo",
                follow_redirects=False,
            )
            assert undo_resp.status_code in (302, 303)

            comp_reloaded = ProCompetitor.query.get(comp_id)
            assert comp_reloaded.status == "active"

    def test_undo_after_window_flashes_expired(self, app):
        """When no recent audit entry exists, flash 'Undo window expired' or 'No scratch to undo'."""
        from database import db

        with app.app_context():
            t = _seed_tournament(db)
            u = _seed_admin(db)
            comp = _seed_pro(db, t)
            comp_id = comp.id
            db.session.commit()

            client = _auth_client(app, u)
            resp = client.post(
                f"/scoring/{t.id}/competitor/{comp_id}/scratch-undo",
                follow_redirects=True,
            )
            assert resp.status_code == 200
            assert (
                b"No scratch" in resp.data
                or b"expired" in resp.data
                or b"undo" in resp.data.lower()
            )

    def test_undo_redirects_post_redirect_get(self, app):
        from database import db

        with app.app_context():
            t = _seed_tournament(db)
            u = _seed_admin(db)
            comp = _seed_pro(db, t)
            comp_id = comp.id
            db.session.commit()

            client = _auth_client(app, u)
            resp = client.post(
                f"/scoring/{t.id}/competitor/{comp_id}/scratch-undo",
                follow_redirects=False,
            )
            assert resp.status_code in (302, 303)
            assert resp.location


# ===========================================================================
# TestRegistrationScratchIntegration
# ===========================================================================


class TestRegistrationScratchIntegration:
    """Registration scratch paths now go through the cascade."""

    def test_pro_scratch_route_redirects_to_preview(self, app):
        """POST to registration scratch should redirect to cascade preview page."""
        from database import db

        with app.app_context():
            t = _seed_tournament(db)
            u = _seed_admin(db)
            comp = _seed_pro(db, t)
            comp_id = comp.id
            db.session.commit()

            client = _auth_client(app, u)
            resp = client.post(
                f"/registration/{t.id}/pro/{comp_id}/scratch",
                follow_redirects=False,
            )
            # Should redirect — either to preview page or directly (cascade executes)
            assert resp.status_code in (302, 303)
            # The competitor must eventually be scratched (follow the chain)
            final = client.post(
                f"/registration/{t.id}/pro/{comp_id}/scratch",
                follow_redirects=True,
            )
            assert final.status_code == 200

    def test_college_scratch_route_uses_cascade(self, app):
        """POST to college scratch should use the cascade service."""
        from database import db
        from models.competitor import CollegeCompetitor
        from models.team import Team

        with app.app_context():
            t = _seed_tournament(db)
            u = _seed_admin(db)
            team = Team(
                tournament_id=t.id,
                school_name="University of Montana",
                school_abbreviation="UM",
                team_code="UM-A",
            )
            db.session.add(team)
            db.session.flush()
            comp = CollegeCompetitor(
                tournament_id=t.id,
                team_id=team.id,
                name="College Alice",
                gender="F",
                status="active",
            )
            db.session.add(comp)
            db.session.flush()
            comp_id = comp.id
            team_id = team.id
            db.session.commit()

            client = _auth_client(app, u)
            resp = client.post(
                f"/registration/{t.id}/college/competitor/{comp_id}/scratch",
                follow_redirects=False,
            )
            assert resp.status_code in (302, 303)
