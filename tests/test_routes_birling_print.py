"""
Route-level tests for the blank-bracket print endpoints (PR D).

Covers:
    - GET /scheduling/<tid>/event/<eid>/birling/print-blank
      (redirects to seeding page when the bracket isn't generated yet;
       otherwise returns HTML or PDF via weasyprint_or_html)
    - GET /scheduling/<tid>/birling/print-all
      (combined doc; skips ungenerated events with a flash)
    - 404s for non-bracket events / wrong tournament

Run:  pytest tests/test_routes_birling_print.py -v
"""

from __future__ import annotations

import json
import uuid

import pytest

from tests.conftest import make_event, make_tournament


@pytest.fixture()
def bp_auth_client(app, db_session):
    """Test client logged in as a unique admin per test.  Same pattern as
    tests/test_routes_video_judge.py::vj_auth_client — avoids the
    conftest admin_user fixture's unique-username collision."""
    from models.user import User

    user = User(username=f"bp_admin_{uuid.uuid4().hex[:8]}", role="admin")
    user.set_password("bp_pass")
    db_session.add(user)
    db_session.flush()

    c = app.test_client()
    with c.session_transaction() as sess:
        sess["_user_id"] = str(user.id)
    return c


def _make_bracket_event(session, tournament, name="Birling", gender="M"):
    return make_event(
        session,
        tournament,
        name=name,
        event_type="college",
        scoring_type="bracket",
        stand_type="birling",
        gender=gender,
    )


def _seed_generated_bracket(event):
    """Give `event` a minimal generated-bracket payload so
    build_birling_print_context returns a real context."""
    payload = {
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
                    },
                ],
                [
                    {
                        "match_id": "W2_1",
                        "round": "winners_2",
                        "competitor1": None,
                        "competitor2": None,
                        "winner": None,
                        "loser": None,
                        "falls": [],
                        "is_bye": False,
                    },
                ],
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
        "competitors": [
            {"id": 1, "name": "Alice"},
            {"id": 2, "name": "Bob"},
        ],
        "seeding": [1, 2],
        "placements": {},
    }
    event.payouts = json.dumps(payload)


# ---------------------------------------------------------------------------
# Per-event: /scheduling/<tid>/event/<eid>/birling/print-blank
# ---------------------------------------------------------------------------


class TestPerEventPrint:
    def test_ungenerated_bracket_flashes_and_redirects(
        self,
        bp_auth_client,
        db_session,
    ):
        t = make_tournament(db_session)
        ev = _make_bracket_event(db_session, t)
        db_session.flush()

        resp = bp_auth_client.get(
            f"/scheduling/{t.id}/event/{ev.id}/birling/print-blank"
        )
        # Flash + redirect to birling_manage.
        assert resp.status_code in (302, 303)
        assert f"/scheduling/{t.id}/event/{ev.id}/birling" in resp.headers["Location"]

    def test_generated_bracket_returns_printable_html(
        self,
        bp_auth_client,
        db_session,
    ):
        t = make_tournament(db_session)
        ev = _make_bracket_event(db_session, t)
        _seed_generated_bracket(ev)
        db_session.flush()

        resp = bp_auth_client.get(
            f"/scheduling/{t.id}/event/{ev.id}/birling/print-blank"
        )
        assert resp.status_code == 200
        # WeasyPrint may or may not be installed — accept HTML or PDF.
        ct = resp.mimetype
        assert ct in ("text/html", "application/pdf", "application/octet-stream")
        body = resp.data
        # Round-1 competitor names should appear (seeded).
        if ct == "text/html":
            assert b"Alice" in body and b"Bob" in body

    def test_non_bracket_event_404(self, bp_auth_client, db_session):
        t = make_tournament(db_session)
        ev = make_event(
            db_session,
            t,
            name="Underhand",
            event_type="pro",
            scoring_type="time",
            stand_type="underhand",
        )
        db_session.flush()

        resp = bp_auth_client.get(
            f"/scheduling/{t.id}/event/{ev.id}/birling/print-blank"
        )
        assert resp.status_code == 404

    def test_missing_tournament_404(self, bp_auth_client):
        resp = bp_auth_client.get("/scheduling/99999/event/1/birling/print-blank")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Combined: /scheduling/<tid>/birling/print-all
# ---------------------------------------------------------------------------


class TestPrintAll:
    def test_no_bracket_events_flashes_and_redirects(
        self,
        bp_auth_client,
        db_session,
    ):
        t = make_tournament(db_session)
        # No bracket events at all.
        db_session.flush()
        resp = bp_auth_client.get(f"/scheduling/{t.id}/birling/print-all")
        assert resp.status_code in (302, 303)
        assert f"/tournament/{t.id}" in resp.headers["Location"]

    def test_all_ungenerated_flashes_and_redirects(
        self,
        bp_auth_client,
        db_session,
    ):
        t = make_tournament(db_session)
        _make_bracket_event(db_session, t, name="Birling", gender="M")
        _make_bracket_event(db_session, t, name="Birling", gender="F")
        db_session.flush()

        resp = bp_auth_client.get(f"/scheduling/{t.id}/birling/print-all")
        # None seeded → redirect, don't render a blank doc.
        assert resp.status_code in (302, 303)

    def test_mixed_generation_renders_only_seeded(
        self,
        bp_auth_client,
        db_session,
    ):
        """One seeded, one not → combined doc contains the seeded event,
        skip flash mentions the other."""
        t = make_tournament(db_session)
        men = _make_bracket_event(db_session, t, name="Birling", gender="M")
        _make_bracket_event(db_session, t, name="Birling", gender="F")
        _seed_generated_bracket(men)
        db_session.flush()

        resp = bp_auth_client.get(f"/scheduling/{t.id}/birling/print-all")
        assert resp.status_code == 200
        if resp.mimetype == "text/html":
            # Jinja escapes apostrophes — "Men's" renders as "Men&#39;s".
            assert b"Men" in resp.data and b"Birling" in resp.data
            # Alice+Bob from the seeded bracket show up too.
            assert b"Alice" in resp.data

    def test_all_generated_renders_all(self, bp_auth_client, db_session):
        t = make_tournament(db_session)
        men = _make_bracket_event(db_session, t, name="Birling", gender="M")
        women = _make_bracket_event(db_session, t, name="Birling", gender="F")
        _seed_generated_bracket(men)
        _seed_generated_bracket(women)
        db_session.flush()

        resp = bp_auth_client.get(f"/scheduling/{t.id}/birling/print-all")
        assert resp.status_code == 200
        if resp.mimetype == "text/html":
            # Two bracket pages separated by <div class="bracket-page">.
            assert resp.data.count(b"bracket-page") >= 2
            # Women's and Men's both render (checking escaped forms).
            assert b"Men" in resp.data and b"Women" in resp.data

    def test_missing_tournament_404(self, bp_auth_client):
        resp = bp_auth_client.get("/scheduling/99999/birling/print-all")
        assert resp.status_code == 404
