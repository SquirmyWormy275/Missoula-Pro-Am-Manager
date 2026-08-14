"""
Route tests for the Birling index page.

The sidebar "Birling Brackets" link used to go straight to
``scheduling.birling_print_all`` — which silently skipped any event whose
bracket had not been seeded.  When an operator seeded one gender first,
clicking the sidebar link produced a combined PDF containing only that
gender's bracket.  The other gender's seeding page was only reachable via a
deeply-buried card on the Events page, so operators believed it could not be
seeded.

The fix is an index page at ``/scheduling/<tid>/birling`` that lists every
college birling event with per-event Manage and Print buttons, plus a single
combined Print-All action.  The sidebar now points here.

Run:  pytest tests/test_birling_index_route.py -v
"""

from __future__ import annotations

import json
import uuid

import pytest

from tests.conftest import (
    make_college_competitor,
    make_event,
    make_team,
    make_tournament,
)


@pytest.fixture()
def bi_auth_client(app, db_session):
    """Admin-authed test client — isolated per test to avoid username collisions."""
    from models.user import User

    user = User(username=f"bi_admin_{uuid.uuid4().hex[:8]}", role="admin")
    user.set_password("bi_pass")
    db_session.add(user)
    db_session.flush()

    c = app.test_client()
    with c.session_transaction() as sess:
        sess["_user_id"] = str(user.id)
    return c


def _make_birling(session, tournament, gender):
    return make_event(
        session,
        tournament,
        name="Birling",
        event_type="college",
        scoring_type="bracket",
        stand_type="birling",
        gender=gender,
    )


def _make_pro_bracket(session, tournament):
    """Model an invalid manual Pro Birling row that routes must refuse."""
    return make_event(
        session,
        tournament,
        name="Pro Birling",
        event_type="pro",
        scoring_type="bracket",
        stand_type="birling",
    )


def _real_pair(session, tournament):
    """Two live college competitors for the bracket blob to cite.

    This used to be the literal ids 1 and 2, which existed in no table.
    ``services/reference_gate.py`` refuses a save whose references resolve to
    nobody, and it is right to: a route test that renders a bracket of
    competitors who do not exist was asserting against a shape the application
    can no longer produce.
    """
    team = make_team(session, tournament, code=f"UM-{uuid.uuid4().hex[:4]}")
    alice = make_college_competitor(session, tournament, team, name="Alice")
    bob = make_college_competitor(session, tournament, team, name="Bob")
    session.flush()
    return alice.id, bob.id


def _seed_bracket(session, tournament, event):
    """Minimal generated-bracket payload (no matches played)."""
    first, second = _real_pair(session, tournament)
    event.payouts = json.dumps(
        {
            "bracket": {
                "winners": [
                    [
                        {
                            "match_id": "W1_1",
                            "round": "winners_1",
                            "competitor1": first,
                            "competitor2": second,
                            "winner": None,
                            "loser": None,
                            "falls": [],
                            "is_bye": False,
                        }
                    ]
                ],
                "losers": [],
                "finals": {
                    "match_id": "F1",
                    "round": "finals",
                    "competitor1": None,
                    "competitor2": None,
                    "winner": None,
                    "loser": None,
                    "falls": [],
                },
                "true_finals": {
                    "match_id": "F2",
                    "round": "true_finals",
                    "competitor1": None,
                    "competitor2": None,
                    "winner": None,
                    "loser": None,
                    "falls": [],
                    "needed": False,
                },
            },
            "competitors": [{"id": first, "name": "Alice"},
                            {"id": second, "name": "Bob"}],
            "seeding": [first, second],
            "placements": {},
        }
    )


class TestBirlingIndex:
    def test_empty_tournament_renders_zero_state(self, bi_auth_client, db_session):
        t = make_tournament(db_session)
        db_session.flush()

        resp = bi_auth_client.get(f"/scheduling/{t.id}/birling")
        assert resp.status_code == 200
        assert b"No birling events" in resp.data or b"no birling" in resp.data.lower()

    def test_both_genders_listed_with_manage_links(
        self,
        bi_auth_client,
        db_session,
    ):
        """The root cause fix: both men's and women's birling must appear on
        this page, each with its own Seed/Manage link. Regression guard for
        the dead-end that sent operators to the combined-print PDF instead."""
        t = make_tournament(db_session)
        men = _make_birling(db_session, t, gender="M")
        women = _make_birling(db_session, t, gender="F")
        db_session.flush()

        resp = bi_auth_client.get(f"/scheduling/{t.id}/birling")
        assert resp.status_code == 200

        # Every event must have its own manage URL rendered on the page.
        assert f"/scheduling/{t.id}/event/{men.id}/birling".encode() in resp.data
        assert f"/scheduling/{t.id}/event/{women.id}/birling".encode() in resp.data

    def test_seeded_status_surfaced(self, bi_auth_client, db_session):
        """Each event should show whether its bracket has been generated yet."""
        t = make_tournament(db_session)
        men = _make_birling(db_session, t, gender="M")
        _make_birling(db_session, t, gender="F")
        _seed_bracket(db_session, t, men)
        db_session.flush()

        resp = bi_auth_client.get(f"/scheduling/{t.id}/birling")
        assert resp.status_code == 200
        # Page must show BOTH a seeded marker and an unseeded marker.
        body = resp.data.lower()
        assert b"not seeded" in body or b"unseeded" in body or b"seed now" in body
        assert b"seeded" in body

    def test_print_all_action_present(self, bi_auth_client, db_session):
        """The combined print-all must still be reachable from the index."""
        t = make_tournament(db_session)
        _make_birling(db_session, t, gender="M")
        _make_birling(db_session, t, gender="F")
        db_session.flush()

        resp = bi_auth_client.get(f"/scheduling/{t.id}/birling")
        assert resp.status_code == 200
        assert f"/scheduling/{t.id}/birling/print-all".encode() in resp.data

    def test_missing_tournament_404(self, bi_auth_client):
        resp = bi_auth_client.get("/scheduling/99999/birling")
        assert resp.status_code == 404

    def test_index_excludes_manually_created_pro_bracket(self, bi_auth_client, db_session):
        """The college-only Birling index must not surface prohibited Pro events."""
        tournament = make_tournament(db_session)
        college_event = _make_birling(db_session, tournament, gender="M")
        pro_event = _make_pro_bracket(db_session, tournament)
        db_session.flush()

        response = bi_auth_client.get(f"/scheduling/{tournament.id}/birling")

        assert response.status_code == 200
        assert f"/event/{college_event.id}/birling".encode() in response.data
        assert f"/event/{pro_event.id}/birling".encode() not in response.data


class TestProBirlingRouteGuards:
    @pytest.mark.parametrize(
        ("method", "suffix"),
        [
            ("get", ""),
            ("post", "/generate"),
            ("post", "/record"),
            ("post", "/fall"),
            ("post", "/undo"),
            ("post", "/reset"),
            ("post", "/finalize"),
            ("get", "/print-blank"),
        ],
    )
    def test_pro_bracket_event_is_rejected_everywhere(
        self, bi_auth_client, db_session, method, suffix
    ):
        """No Birling endpoint may create or mutate a prohibited Pro bracket."""
        tournament = make_tournament(db_session)
        event = _make_pro_bracket(db_session, tournament)
        db_session.flush()

        response = getattr(bi_auth_client, method)(
            f"/scheduling/{tournament.id}/event/{event.id}/birling{suffix}",
            data={},
        )

        assert response.status_code == 404

    def test_heat_sheets_do_not_render_a_pro_bracket(
        self,
        bi_auth_client,
        db_session,
        monkeypatch,
    ):
        """The aggregate operational print is college-only too."""
        tournament = make_tournament(db_session)
        _make_pro_bracket(db_session, tournament)
        db_session.flush()

        class UnexpectedBracket:
            def __init__(self, _event):
                raise AssertionError("Pro brackets must not reach the Birling renderer")

        monkeypatch.setattr(
            "services.birling_bracket.BirlingBracket", UnexpectedBracket
        )
        response = bi_auth_client.get(f"/scheduling/{tournament.id}/heat-sheets")
        assert response.status_code == 200


class TestSidebarLink:
    """The sidebar link must point at the new index, not the print-all PDF."""

    def test_sidebar_links_to_birling_index(self, bi_auth_client, db_session):
        t = make_tournament(db_session)
        _make_birling(db_session, t, gender="M")
        db_session.flush()

        # Load a page that renders the sidebar.
        resp = bi_auth_client.get(f"/tournament/{t.id}")
        assert resp.status_code == 200
        # Index URL appears in the rendered sidebar.
        assert (
            f'/scheduling/{t.id}/birling"'.encode() in resp.data
            or f"/scheduling/{t.id}/birling'".encode() in resp.data
        )
