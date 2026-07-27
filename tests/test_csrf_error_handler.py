"""Regression test for the CSRFError handler in app.py.

Context: the default Flask-WTF behavior on a missing/expired CSRF token is a
raw 400 "Bad Request" page. On long-open forms (CSRF time limit defaults to
1 hour) this silently ate user clicks, e.g., the "Integrate Spillover" button
on the scheduling page. The handler in app.py converts that into a flash +
redirect on HTML routes so the user gets a clear, actionable message.

Found during /investigate on 2026-04-21.
"""

import os

import pytest


@pytest.fixture
def csrf_app():
    """App with CSRF protection actually enabled (global conftest disables it)."""
    os.environ.pop("WTF_CSRF_ENABLED", None)
    from app import create_app

    app = create_app()
    app.config["WTF_CSRF_ENABLED"] = True
    app.config["TESTING"] = True
    # Re-disable globally so other tests continue to run with CSRF off
    os.environ["WTF_CSRF_ENABLED"] = "False"
    yield app


def test_csrf_error_on_html_route_redirects_with_flash(csrf_app):
    """A POST with missing CSRF on an HTML route redirects to the referrer
    with a user-friendly warning flash, rather than returning a raw 400 page."""
    with csrf_app.test_client() as client:
        referrer = "http://localhost/scheduling/2/events"
        resp = client.post(
            "/scheduling/2/events",
            data={"action": "integrate_spillover"},
            headers={"Referer": referrer},
            follow_redirects=False,
        )
        assert resp.status_code == 302, (
            f"expected redirect (handler caught CSRFError), got {resp.status_code}; "
            "regression: CSRFError handler is missing or broken"
        )
        assert resp.headers.get("Location") == referrer

        with client.session_transaction() as sess:
            flashes = sess.get("_flashes", [])
        categories = [c for c, _ in flashes]
        messages = [m for _, m in flashes]
        assert "warning" in categories, f"no warning flash: {flashes}"
        assert any(
            "session expired" in m.lower() for m in messages
        ), f"flash did not mention session expired: {messages}"


def test_csrf_error_falls_back_to_request_path_when_no_referrer(csrf_app):
    """If the browser omits the Referer header, redirect to request.path so the
    user still lands on a usable page and reloads with a fresh token."""
    with csrf_app.test_client() as client:
        resp = client.post(
            "/scheduling/2/events",
            data={"action": "integrate_spillover"},
            follow_redirects=False,
        )
        assert resp.status_code == 302
        location = resp.headers.get("Location", "")
        assert location.endswith(
            "/scheduling/2/events"
        ), f"expected fallback to request path, got {location!r}"
