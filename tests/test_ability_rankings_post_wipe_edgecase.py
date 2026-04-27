"""
Regression tests for the "ranks revert to preset after save" bug.

The bug lived in routes/scheduling/ability_rankings.py:88-96 — the delete-stale
loop passed Python `True` to .filter() when ranked_ids was empty, which
SQLAlchemy emits as WHERE TRUE and silently DELETED every rank in that
category. Triggered any time the form submitted an empty `order_<cat>_<gender>`
hidden input (i.e., the Ranked zone had no children at submit time).

User-visible symptom: "I set my rankings, save, and they go back to whatever
your fucking preset was."

Each test uses a fresh tempfile SQLite DB so commits from the POST route
cannot leak between tests via the shared module-scoped app fixture.
"""

import json
import os
import tempfile

import pytest

from database import db as _db


def _make_app_with_admin():
    from tests.db_test_utils import create_test_app
    from models.user import User

    _app, db_path = create_test_app()
    with _app.app_context():
        if not User.query.filter_by(username="ar_admin").first():
            u = User(username="ar_admin", role="admin")
            u.set_password("ar_pass")
            _db.session.add(u)
            _db.session.commit()
    return _app, db_path


@pytest.fixture()
def app():
    _app, db_path = _make_app_with_admin()
    with _app.app_context():
        yield _app
        _db.session.remove()
    try:
        os.unlink(db_path)
    except OSError:
        pass


@pytest.fixture()
def auth_client(app):
    c = app.test_client()
    c.post(
        "/auth/login",
        data={"username": "ar_admin", "password": "ar_pass"},
        follow_redirects=True,
    )
    return c


def _seed(session):
    from models import Tournament
    from models.competitor import ProCompetitor
    from models.event import Event

    t = Tournament(name="edge", year=2026, status="pro_active")
    session.add(t)
    session.flush()

    ev_sb = Event(
        tournament_id=t.id,
        name="Springboard",
        event_type="pro",
        gender="M",
        scoring_type="time",
        scoring_order="lowest_wins",
        stand_type="springboard",
        max_stands=4,
    )
    ev_uh = Event(
        tournament_id=t.id,
        name="Underhand",
        event_type="pro",
        gender="M",
        scoring_type="time",
        scoring_order="lowest_wins",
        stand_type="underhand",
        max_stands=5,
    )
    session.add_all([ev_sb, ev_uh])
    session.flush()

    pros = []
    for nm in ["A Andy", "B Bob", "C Cal"]:
        p = ProCompetitor(
            tournament_id=t.id,
            name=nm,
            gender="M",
            events_entered=json.dumps(["Springboard", "Underhand"]),
            gear_sharing=json.dumps({}),
            partners=json.dumps({}),
            status="active",
        )
        session.add(p)
        session.flush()
        pros.append(p)

    session.commit()
    return t, pros


def test_baseline_post_saves_and_reloads_as_ranked(auth_client, app):
    """POST ranks, GET should render them in the Ranked zone."""
    from models.pro_event_rank import ProEventRank

    with app.app_context():
        t, pros = _seed(_db.session)
        tid = t.id
        pro_csv = ",".join(str(p.id) for p in pros)

    resp = auth_client.post(
        f"/scheduling/{tid}/pro/ability-rankings",
        data={"order_springboard_M": pro_csv},
        follow_redirects=False,
    )
    assert resp.status_code in (302, 303)

    with app.app_context():
        rows = ProEventRank.query.filter_by(tournament_id=tid).all()
        assert len(rows) == 3

    get_resp = auth_client.get(f"/scheduling/{tid}/pro/ability-rankings")
    html = get_resp.get_data(as_text=True)
    sb_start = html.find('id="ranked_springboard_M"')
    sb_unranked_start = html.find('id="unranked_springboard_M"')
    ranked_block = html[sb_start:sb_unranked_start]
    for p_name in ["A Andy", "B Bob", "C Cal"]:
        assert p_name in ranked_block


def test_empty_form_does_not_wipe_prior_ranks(auth_client, app):
    """
    THE REPRO. Submitting with empty order_* hidden inputs (what happens when
    the Ranked zone captured zero children at submit time — e.g. the user
    never managed to drag anything into the zone) MUST NOT silently delete
    every prior rank in that category.
    """
    from models.pro_event_rank import ProEventRank

    with app.app_context():
        t, pros = _seed(_db.session)
        tid = t.id
        for i, p in enumerate(pros, start=1):
            _db.session.add(
                ProEventRank(
                    tournament_id=tid,
                    competitor_id=p.id,
                    event_category="springboard",
                    rank=i,
                )
            )
        _db.session.commit()
        assert ProEventRank.query.filter_by(tournament_id=tid).count() == 3

    resp = auth_client.post(
        f"/scheduling/{tid}/pro/ability-rankings",
        data={
            "order_springboard_M": "",
            "order_underhand_M": "",
        },
        follow_redirects=False,
    )
    assert resp.status_code in (302, 303)

    with app.app_context():
        remaining = ProEventRank.query.filter_by(tournament_id=tid).count()
        assert remaining == 3, (
            f"Empty-form POST wiped {3 - remaining} springboard ranks — "
            "this is the 'rankings revert to preset' bug."
        )


def test_one_category_unchanged_while_saving_another(auth_client, app):
    """
    Saving Underhand ranks must not disturb a previously-saved Springboard
    ranking — even when the browser JS re-submits order_springboard_M with
    the existing IDs.
    """
    from models.pro_event_rank import ProEventRank

    with app.app_context():
        t, pros = _seed(_db.session)
        tid = t.id
        pro_csv = ",".join(str(p.id) for p in pros)

        for i, p in enumerate(pros, start=1):
            _db.session.add(
                ProEventRank(
                    tournament_id=tid,
                    competitor_id=p.id,
                    event_category="springboard",
                    rank=i,
                )
            )
        _db.session.commit()

    resp = auth_client.post(
        f"/scheduling/{tid}/pro/ability-rankings",
        data={
            "order_springboard_M": pro_csv,
            "order_underhand_M": pro_csv,
        },
        follow_redirects=False,
    )
    assert resp.status_code in (302, 303)

    with app.app_context():
        rows = ProEventRank.query.filter_by(tournament_id=tid).all()
        cats = sorted(r.event_category for r in rows)
        assert cats == [
            "springboard",
            "springboard",
            "springboard",
            "underhand",
            "underhand",
            "underhand",
        ], f"got {cats}"


def test_unranking_individual_still_works(auth_client, app):
    """
    The fix must not break the ability to unrank ONE competitor.
    Dragging a single competitor from Ranked to Unranked leaves the other
    ranked IDs in the POST — the dropped one is now absent and its rank
    should be deleted (normal stale-cleanup path with non-empty ranked_ids).
    """
    from models.pro_event_rank import ProEventRank

    with app.app_context():
        t, pros = _seed(_db.session)
        tid = t.id

        for i, p in enumerate(pros, start=1):
            _db.session.add(
                ProEventRank(
                    tournament_id=tid,
                    competitor_id=p.id,
                    event_category="springboard",
                    rank=i,
                )
            )
        _db.session.commit()
        dropped_id = pros[0].id
        kept_csv = ",".join(str(p.id) for p in pros[1:])

    resp = auth_client.post(
        f"/scheduling/{tid}/pro/ability-rankings",
        data={"order_springboard_M": kept_csv},
        follow_redirects=False,
    )
    assert resp.status_code in (302, 303)

    with app.app_context():
        remaining = ProEventRank.query.filter_by(tournament_id=tid).all()
        assert len(remaining) == 2
        remaining_ids = {r.competitor_id for r in remaining}
        assert dropped_id not in remaining_ids
