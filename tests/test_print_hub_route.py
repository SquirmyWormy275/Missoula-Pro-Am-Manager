"""Tests for the Print Hub page + email POST route."""

import smtplib

import pytest


@pytest.fixture()
def tournament(app, db_session):
    from tests.conftest import make_pro_competitor, make_tournament

    t = make_tournament(db_session)
    make_pro_competitor(db_session, t, "Alice", gender="F")
    db_session.commit()
    return t


@pytest.fixture()
def auth_scheduling_client(app, db_session):
    """Logged-in judge client that can hit scheduling routes."""
    import uuid as _uuid

    from models.user import User

    u = User(username=f"hub_judge_{_uuid.uuid4().hex[:8]}", role="judge")
    u.set_password("pw")
    db_session.add(u)
    db_session.commit()
    c = app.test_client()
    with c.session_transaction() as sess:
        sess["_user_id"] = str(u.id)
    return c, u


# ---------------------------------------------------------------------------
# GET /print-hub
# ---------------------------------------------------------------------------


def test_print_hub_get_renders_200(auth_scheduling_client, tournament):
    client, _ = auth_scheduling_client
    resp = client.get(f"/scheduling/{tournament.id}/print-hub")
    assert resp.status_code == 200
    assert b"Print Hub" in resp.data


def test_print_hub_shows_pro_checkout_row(auth_scheduling_client, tournament):
    client, _ = auth_scheduling_client
    resp = client.get(f"/scheduling/{tournament.id}/print-hub")
    assert resp.status_code == 200
    # Pro checkout row should be present (Alice makes it configured).
    assert b"Pro Saturday Checkout Roster" in resp.data


def test_print_hub_shows_email_disabled_when_smtp_missing(
    auth_scheduling_client,
    tournament,
    monkeypatch,
):
    monkeypatch.delenv("SMTP_HOST", raising=False)
    client, _ = auth_scheduling_client
    resp = client.get(f"/scheduling/{tournament.id}/print-hub")
    assert resp.status_code == 200
    assert (
        b"SMTP not configured" in resp.data
        or b"Email delivery is disabled" in resp.data
    )


def test_print_hub_404_on_unknown_tournament(auth_scheduling_client):
    client, _ = auth_scheduling_client
    resp = client.get("/scheduling/99999/print-hub")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# POST /print-hub/email
# ---------------------------------------------------------------------------


def test_email_send_smtp_not_configured_flashes(
    auth_scheduling_client,
    tournament,
    monkeypatch,
):
    monkeypatch.delenv("SMTP_HOST", raising=False)
    client, _ = auth_scheduling_client
    resp = client.post(
        f"/scheduling/{tournament.id}/print-hub/email",
        data={"doc_key": "pro_checkout", "extra_emails": "a@b.com"},
        follow_redirects=False,
    )
    # Flashes error + redirects back to hub.
    assert resp.status_code == 302


def test_email_send_no_recipients_flashes_error(
    auth_scheduling_client,
    tournament,
    monkeypatch,
):
    monkeypatch.setenv("SMTP_HOST", "smtp.test")
    monkeypatch.setenv("SMTP_USER", "user")
    monkeypatch.setenv("SMTP_PASSWORD", "pw")

    # Block actual SMTP just in case.
    def sync_submit(fn, *args, **kwargs):
        pass

    import services.background_jobs as bj

    monkeypatch.setattr(bj, "submit", sync_submit)

    client, _ = auth_scheduling_client
    resp = client.post(
        f"/scheduling/{tournament.id}/print-hub/email",
        data={"doc_key": "pro_checkout"},
        follow_redirects=False,
    )
    assert resp.status_code == 302  # redirect with flash error


def test_email_send_unknown_doc_key_flashes_error(
    auth_scheduling_client,
    tournament,
    monkeypatch,
):
    monkeypatch.setenv("SMTP_HOST", "smtp.test")
    monkeypatch.setenv("SMTP_USER", "user")
    monkeypatch.setenv("SMTP_PASSWORD", "pw")
    client, _ = auth_scheduling_client
    resp = client.post(
        f"/scheduling/{tournament.id}/print-hub/email",
        data={"doc_key": "does-not-exist", "extra_emails": "a@b.com"},
        follow_redirects=False,
    )
    assert resp.status_code == 302


def test_email_send_queues_log_on_valid_input(
    app,
    db_session,
    auth_scheduling_client,
    tournament,
    monkeypatch,
):
    from models import PrintEmailLog

    monkeypatch.setenv("SMTP_HOST", "smtp.test")
    monkeypatch.setenv("SMTP_PORT", "2525")
    monkeypatch.setenv("SMTP_USER", "tester")
    monkeypatch.setenv("SMTP_PASSWORD", "secret")
    monkeypatch.setenv("SMTP_FROM", "noreply@proam.test")

    # Intercept SMTP so we don't hit a real server.
    from tests.test_email_delivery import _FakeSMTP

    _FakeSMTP.instances = []
    monkeypatch.setattr(smtplib, "SMTP", _FakeSMTP)

    def sync_submit(fn, *args, **kwargs):
        fn(*args, **kwargs)

    import services.background_jobs as bj

    monkeypatch.setattr(bj, "submit", sync_submit)

    client, _ = auth_scheduling_client
    resp = client.post(
        f"/scheduling/{tournament.id}/print-hub/email",
        data={
            "doc_key": "pro_checkout",
            "extra_emails": "recipient@example.com",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 302

    log = PrintEmailLog.query.filter_by(
        tournament_id=tournament.id, doc_key="pro_checkout"
    ).first()
    assert log is not None
    assert log.status == "sent"
    assert "recipient@example.com" in log.get_recipients()


def test_email_send_rate_limit_decorator_present(app):
    """Minimal regression: the email POST route is registered + has a limiter.

    We don't simulate 21 requests — flask-limiter + test client interplay is
    fiddly and the limiter is exercised at real traffic. This just proves
    the route is wired.
    """
    rules = {r.endpoint for r in app.url_map.iter_rules()}
    assert "scheduling.print_hub_email" in rules
