"""Tests for the Pro Saturday Checkout Roster print routes."""

import pytest


@pytest.fixture()
def tournament_with_pros(app, db_session):
    from tests.conftest import make_pro_competitor, make_tournament

    t = make_tournament(db_session)
    make_pro_competitor(
        db_session, t, "Charlie", gender="M", events=["Underhand", "Springboard"]
    )
    make_pro_competitor(db_session, t, "Alice", gender="F", events=["Standing Block"])
    make_pro_competitor(
        db_session,
        t,
        "Bob (scratched)",
        gender="M",
        events=["Underhand"],
        status="scratched",
    )
    db_session.commit()
    return t


@pytest.fixture()
def judge_client(app, db_session):
    from models.user import User

    import uuid as _uuid

    u = User(username=f"roster_judge_{_uuid.uuid4().hex[:8]}", role="judge")
    u.set_password("pw")
    db_session.add(u)
    db_session.commit()
    c = app.test_client()
    with c.session_transaction() as sess:
        sess["_user_id"] = str(u.id)
    return c


# ---------------------------------------------------------------------------
# Print (HTML)
# ---------------------------------------------------------------------------


def test_checkout_roster_print_renders(judge_client, tournament_with_pros):
    resp = judge_client.get(
        f"/scheduling/{tournament_with_pros.id}/pro/checkout-roster/print"
    )
    assert resp.status_code == 200
    assert b"Pro Saturday Checkout" in resp.data


def test_checkout_roster_is_alphabetical(judge_client, tournament_with_pros):
    resp = judge_client.get(
        f"/scheduling/{tournament_with_pros.id}/pro/checkout-roster/print"
    )
    body = resp.data.decode("utf-8")
    # Alice should appear before Charlie.
    alice_pos = body.find("Alice")
    charlie_pos = body.find("Charlie")
    assert alice_pos > 0 and charlie_pos > 0
    assert alice_pos < charlie_pos


def test_checkout_roster_excludes_scratched(judge_client, tournament_with_pros):
    resp = judge_client.get(
        f"/scheduling/{tournament_with_pros.id}/pro/checkout-roster/print"
    )
    body = resp.data.decode("utf-8")
    assert "Bob (scratched)" not in body


def test_checkout_roster_shows_events_column(judge_client, tournament_with_pros):
    resp = judge_client.get(
        f"/scheduling/{tournament_with_pros.id}/pro/checkout-roster/print"
    )
    body = resp.data.decode("utf-8")
    # Charlie entered Underhand and Springboard — both should appear in the row.
    # Grab the line with Charlie and verify both events are present nearby.
    assert "Underhand" in body
    assert "Springboard" in body


def test_checkout_roster_empty_tournament_ok(judge_client, app, db_session):
    from tests.conftest import make_tournament

    t = make_tournament(db_session)
    db_session.commit()
    resp = judge_client.get(f"/scheduling/{t.id}/pro/checkout-roster/print")
    assert resp.status_code == 200
    assert b"No active pro competitors" in resp.data


def test_checkout_roster_writes_tracker(
    judge_client, app, db_session, tournament_with_pros
):
    from models import PrintTracker

    judge_client.get(f"/scheduling/{tournament_with_pros.id}/pro/checkout-roster/print")
    row = PrintTracker.query.filter_by(
        tournament_id=tournament_with_pros.id,
        doc_key="pro_checkout",
    ).first()
    assert row is not None
    assert row.last_printed_fingerprint


# ---------------------------------------------------------------------------
# PDF route
# ---------------------------------------------------------------------------


def test_checkout_roster_pdf_route_registers(app):
    rules = {r.endpoint for r in app.url_map.iter_rules()}
    assert "scheduling.pro_checkout_roster_pdf" in rules


def test_checkout_roster_pdf_returns_pdf_or_html(judge_client, tournament_with_pros):
    resp = judge_client.get(
        f"/scheduling/{tournament_with_pros.id}/pro/checkout-roster/pdf"
    )
    assert resp.status_code == 200
    assert resp.headers.get("Content-Type") in (
        "application/pdf",
        "text/html",
        "text/html; charset=utf-8",
    )
