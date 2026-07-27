"""Tests for Team admin validation override persistence.

Background: small schools with edge-case rosters (e.g., 5 men + 0 women) can be
marked valid via an admin override. The override must survive re-validation and
Excel re-imports — the whole point of the feature is that a judge doesn't have
to re-override the same team every time someone fixes a different competitor
or re-uploads the entry form.

These tests pin down the contract:
  1. override_team_validation sets is_override=True AND keeps status='active'
  2. set_validation_errors on an overridden team preserves status='active'
  3. set_validation_errors with no errors auto-clears is_override (vestigial)
  4. revalidate_team route respects the override
  5. remove_team_override flips back to invalid if errors remain
  6. Excel re-import (via set_validation_errors) preserves the override
"""

import uuid

import pytest

from tests.conftest import make_college_competitor, make_team, make_tournament


@pytest.fixture()
def auth_client(app, db_session):
    """Local override: unique admin username per test so commits across tests
    within this module don't collide on the users.username UNIQUE constraint.
    Mirrors the pattern used in tests/test_partner_reassignment.py."""
    from models.user import User

    unique_name = f"test_admin_tvo_{uuid.uuid4().hex[:8]}"
    u = User(username=unique_name, role="admin")
    u.set_password("pass")
    db_session.add(u)
    db_session.flush()
    c = app.test_client()
    with c.session_transaction() as sess:
        sess["_user_id"] = str(u.id)
    return c

# ---------------------------------------------------------------------------
# Model-level tests — Team.set_validation_errors + is_override interaction
# ---------------------------------------------------------------------------


class TestSetValidationErrorsWithOverride:
    def test_errors_without_override_flips_to_invalid(self, db_session):
        t = make_tournament(db_session)
        team = make_team(db_session, t)
        assert team.status == "active"
        assert team.is_override is False

        team.set_validation_errors([{"type": "too_few_women", "message": "Need 2"}])
        assert team.status == "invalid"
        assert team.is_override is False

    def test_errors_with_override_stays_active(self, db_session):
        t = make_tournament(db_session)
        team = make_team(db_session, t)
        team.is_override = True

        team.set_validation_errors([{"type": "too_few_women", "message": "Need 2"}])
        assert team.status == "active", "override must preserve active status"
        assert team.is_override is True, "override flag must persist"
        assert (
            len(team.get_validation_errors()) == 1
        ), "errors still recorded for display"

    def test_clean_errors_auto_clears_override(self, db_session):
        """Override is vestigial once roster is genuinely clean — auto-clear."""
        t = make_tournament(db_session)
        team = make_team(db_session, t)
        team.is_override = True
        team.set_validation_errors([{"type": "too_few_women", "message": "Need 2"}])
        assert team.is_override is True

        # Now the team somehow passes (imagine roster was fixed)
        team.set_validation_errors([])
        assert team.status == "active"
        assert team.is_override is False, "override auto-clears when no errors remain"

    def test_override_survives_multiple_revalidations(self, db_session):
        """Re-running validation many times must not erode the override."""
        t = make_tournament(db_session)
        team = make_team(db_session, t)
        team.is_override = True
        errors = [{"type": "too_few_women", "message": "Need 2"}]

        for _ in range(5):
            team.set_validation_errors(errors)
            assert team.is_override is True
            assert team.status == "active"


# ---------------------------------------------------------------------------
# Route-level tests — override + revalidate + remove-override
# ---------------------------------------------------------------------------


class TestOverrideTeamValidationRoute:
    def _seed_invalid_team(self, db_session):
        """Team with 5 men + 0 women — fails women-count validation."""
        t = make_tournament(db_session)
        team = make_team(db_session, t)
        for i in range(5):
            make_college_competitor(db_session, t, team, f"Man{i}", gender="M")
        # Manually run validator so set_validation_errors sees real errors
        from services.excel_io import _validate_college_entry_constraints

        errors = _validate_college_entry_constraints({team.id}).get(team.id, [])
        team.set_validation_errors(errors)
        assert team.status == "invalid"
        db_session.flush()
        return t, team

    def test_override_route_flips_status_and_sets_flag(self, auth_client, db_session):
        t, team = self._seed_invalid_team(db_session)
        db_session.commit()

        resp = auth_client.post(
            f"/registration/{t.id}/college/team/{team.id}/override-validation",
            follow_redirects=False,
        )
        assert resp.status_code in (302, 303)

        db_session.expire_all()
        from models.team import Team

        team = Team.query.get(team.id)
        assert team.is_override is True
        assert team.status == "active"
        assert len(team.get_validation_errors()) > 0, "errors preserved for display"

    def test_revalidate_after_override_preserves_override(
        self, auth_client, db_session
    ):
        t, team = self._seed_invalid_team(db_session)
        team.is_override = True
        team.set_validation_errors(
            team.get_validation_errors()
        )  # re-apply with override
        db_session.commit()

        resp = auth_client.post(
            f"/registration/{t.id}/college/team/{team.id}/revalidate",
            follow_redirects=False,
        )
        assert resp.status_code in (302, 303)

        db_session.expire_all()
        from models.team import Team

        team = Team.query.get(team.id)
        assert team.is_override is True, "revalidate must not clear override"
        assert team.status == "active", "overridden team stays active after revalidate"

    def test_remove_override_flips_back_to_invalid(self, auth_client, db_session):
        t, team = self._seed_invalid_team(db_session)
        team.is_override = True
        team.set_validation_errors(team.get_validation_errors())
        db_session.commit()

        resp = auth_client.post(
            f"/registration/{t.id}/college/team/{team.id}/remove-override",
            follow_redirects=False,
        )
        assert resp.status_code in (302, 303)

        db_session.expire_all()
        from models.team import Team

        team = Team.query.get(team.id)
        assert team.is_override is False
        assert (
            team.status == "invalid"
        ), "team flips back to invalid when override removed"

    def test_remove_override_no_op_on_non_overridden_team(
        self, auth_client, db_session
    ):
        t, team = self._seed_invalid_team(db_session)
        assert team.is_override is False
        db_session.commit()

        resp = auth_client.post(
            f"/registration/{t.id}/college/team/{team.id}/remove-override",
            follow_redirects=False,
        )
        assert resp.status_code in (302, 303)

        db_session.expire_all()
        from models.team import Team

        team = Team.query.get(team.id)
        assert team.is_override is False
        assert team.status == "invalid", "nothing should change"


# ---------------------------------------------------------------------------
# Excel re-import preservation — the whole reason this feature exists
# ---------------------------------------------------------------------------


class TestReimportPreservesOverride:
    def test_reimport_path_preserves_override(self, db_session):
        """Simulate what happens when excel_io.process_college_entry_form re-runs
        on a team that has been overridden. The team's set_validation_errors
        gets called with the (still-failing) errors list. Override must stay."""
        t = make_tournament(db_session)
        team = make_team(db_session, t)
        team.is_override = True
        errors = [{"type": "too_few_women", "message": "Need 2"}]
        team.set_validation_errors(errors)
        assert team.status == "active"
        assert team.is_override is True

        # Simulate the re-import path from services/excel_io.py:198-209 —
        # touched_team_ids includes this team, errors_by_team still has errors,
        # the loop calls team.set_validation_errors again.
        team.set_validation_errors(errors)

        assert (
            team.status == "active"
        ), "re-import must not flip overridden team to invalid"
        assert team.is_override is True, "override must survive re-import"
