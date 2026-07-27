"""Regression: payout-template breakdown must render without a Jinja TypeError.

Prior bug: all three payout-listing templates used
    |sort(attribute='0', key=int)
Jinja2's sort filter has no key= kwarg, so GET pages 500'd whenever a
PayoutTemplate row existed. Empty-DB smoke tests missed it because the
{% if templates %} block was skipped.

Traceback came from prod:
    File "templates/scoring/tournament_payouts.html", line 330
    TypeError: do_sort() got an unexpected keyword argument 'key'
"""

import os

import pytest

from database import db as _db


@pytest.fixture(scope="module")
def app():
    from tests.db_test_utils import create_test_app

    _app, db_path = create_test_app()
    with _app.app_context():
        _seed(_app)
        yield _app
        _db.session.remove()
    try:
        os.unlink(db_path)
    except OSError:
        pass


def _seed(app):
    from models import Event, Tournament
    from models.payout_template import PayoutTemplate
    from models.user import User

    if not User.query.filter_by(username="tpl_admin").first():
        u = User(username="tpl_admin", role="admin")
        u.set_password("tpl_pass")
        _db.session.add(u)

    t = Tournament(name="Tpl Render", year=2026, status="setup")
    _db.session.add(t)
    _db.session.flush()

    ev = Event(
        tournament_id=t.id,
        name="Underhand",
        event_type="pro",
        scoring_type="time",
        scoring_order="lowest_wins",
        status="pending",
    )
    _db.session.add(ev)

    # Two templates. Second one has >= 10 positions so a lexicographic
    # string sort would order "10" before "2" — proves int-keyed sort.
    tpl_a = PayoutTemplate(name="Small Purse")
    tpl_a.set_payouts({"1": 500.0, "2": 300.0, "3": 200.0})
    _db.session.add(tpl_a)

    tpl_b = PayoutTemplate(name="Deep Purse")
    tpl_b.set_payouts({str(i): float(100 - i * 5) for i in range(1, 13)})
    _db.session.add(tpl_b)

    _db.session.commit()
    app.config["_TPL"] = {"tid": t.id, "eid": ev.id}


@pytest.fixture()
def auth_client(app):
    c = app.test_client()
    c.post(
        "/auth/login",
        data={"username": "tpl_admin", "password": "tpl_pass"},
        follow_redirects=True,
    )
    return c


def test_sorted_payouts_integer_order():
    """Model method must return items ordered by int key, not lexicographic."""
    from models.payout_template import PayoutTemplate

    tpl = PayoutTemplate(name="sort check")
    tpl.set_payouts({"10": 10.0, "2": 20.0, "1": 30.0, "11": 5.0})
    positions = [pos for pos, _amt in tpl.sorted_payouts()]
    assert positions == [
        "1",
        "2",
        "10",
        "11",
    ], f"expected int-ordered positions, got {positions}"


def test_payout_manager_renders_with_templates(auth_client, app):
    """The prod-reproducing case: GET payout-manager when PayoutTemplates exist."""
    d = app.config["_TPL"]
    r = auth_client.get(f"/scoring/{d['tid']}/pro/payout-manager")
    assert (
        r.status_code < 500
    ), f"payout-manager 500'd with templates present: {r.data.decode()[:2000]}"
    assert r.status_code == 200


def test_configure_payouts_renders_with_templates(auth_client, app):
    """Per-event payout page also has the same Jinja bug site."""
    d = app.config["_TPL"]
    r = auth_client.get(f"/scoring/{d['tid']}/event/{d['eid']}/payouts")
    assert (
        r.status_code < 500
    ), f"configure_payouts 500'd with templates present: {r.data.decode()[:2000]}"
    assert r.status_code == 200


def test_tournament_setup_payouts_tab_renders_with_templates(auth_client, app):
    """Third bug site: tournament_setup.html payouts tab."""
    d = app.config["_TPL"]
    r = auth_client.get(f"/tournament/{d['tid']}/setup?tab=payouts")
    assert (
        r.status_code < 500
    ), f"tournament_setup 500'd with templates present: {r.data.decode()[:2000]}"
    assert r.status_code == 200
