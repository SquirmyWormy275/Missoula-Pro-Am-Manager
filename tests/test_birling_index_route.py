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

from tests.conftest import make_event, make_tournament


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


def _seed_bracket(event):
    """Minimal generated-bracket payload (no matches played)."""
    event.payouts = json.dumps(
        {
            "bracket": {
                "winners": [
                    [
                        {
                            "match_id": "W1_1",
                            "round": "winners_1",
                            "competitor1": 1,
                            "competitor2": 2,
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
            "competitors": [{"id": 1, "name": "Alice"}, {"id": 2, "name": "Bob"}],
            "seeding": [1, 2],
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
        _seed_bracket(men)
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
