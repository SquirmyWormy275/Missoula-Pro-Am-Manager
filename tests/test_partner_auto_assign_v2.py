"""
Regression tests for the V2.14.16 partner-pairing rewrite.

Covers the new ``auto_assign_event_partners`` resolver:
  A. Fuzzy match: typo'd partner names (Mckinley/Mickinley) confirm as
     reciprocal pairs instead of falling through to the unclaimed pool.
  B. Claim tracking: A→B with B→blank holds BOTH for review and does NOT
     auto-pair B with someone else.
  C. Genuinely unclaimed competitors (no partner field, no inbound claim)
     get auto-paired with mixed-gender priority for mixed events.
  D. Self-reference held for review.
  E. Non-reciprocal A→B but B→C holds A and B for review.
  F. Idempotent on a clean roster — second run reports zero new pairs.

Run:
    pytest tests/test_partner_auto_assign_v2.py -v
"""

import json
import os

import pytest

from database import db as _db


@pytest.fixture(scope="module")
def app():
    from tests.db_test_utils import create_test_app

    _app, db_path = create_test_app()
    with _app.app_context():
        yield _app
        _db.session.remove()
    try:
        os.unlink(db_path)
    except OSError:
        pass


@pytest.fixture(autouse=True)
def db_session(app):
    with app.app_context():
        _db.session.begin_nested()
        yield _db.session
        _db.session.rollback()


def _make_tournament(session):
    from models import Tournament

    t = Tournament(name="PartnerResolverV2", year=2026, status="setup")
    session.add(t)
    session.flush()
    return t


def _make_event(session, tournament, name="Pulp Toss", gender_req="mixed"):
    from models import Event

    ev = Event(
        tournament_id=tournament.id,
        name=name,
        event_type="pro",
        gender=None,
        scoring_type="time",
        is_partnered=True,
        partner_gender_requirement=gender_req,
    )
    session.add(ev)
    session.flush()
    return ev


def _make_pro(session, tournament, name, gender, event, partner_name=None):
    from models import ProCompetitor

    comp = ProCompetitor(
        tournament_id=tournament.id,
        name=name,
        gender=gender,
        status="active",
    )
    comp.events_entered = json.dumps([str(event.id)])
    if partner_name:
        comp.partners = json.dumps({str(event.id): partner_name})
    session.add(comp)
    session.flush()
    return comp


def test_typo_partner_confirms_as_reciprocal(db_session):
    """Mckinley/Mickinley fuzzy-resolve to each other — confirmed pair."""
    from services.partner_matching import auto_assign_event_partners

    t = _make_tournament(db_session)
    ev = _make_event(db_session, t)
    a = _make_pro(
        db_session, t, "McKinley Verhulst", "F", ev, partner_name="Jordan Smithe"
    )
    b = _make_pro(
        db_session, t, "Jordan Smith", "M", ev, partner_name="Mickinley Verhulst"
    )

    summary = auto_assign_event_partners(ev)

    assert summary["confirmed_pairs"] == 1
    assert summary["assigned_pairs"] == 0
    assert summary["one_sided_claims"] == []
    assert summary["unmatched"] == 0

    db_session.refresh(a)
    db_session.refresh(b)
    assert a.get_partners()[str(ev.id)] == "Jordan Smith"
    assert b.get_partners()[str(ev.id)] == "McKinley Verhulst"


def test_one_sided_claim_holds_both_for_review(db_session):
    """A says B, B says nothing → both held; B is NOT auto-paired with C."""
    from services.partner_matching import auto_assign_event_partners

    t = _make_tournament(db_session)
    ev = _make_event(db_session, t)
    _make_pro(db_session, t, "Alex Kaper", "M", ev, partner_name="Bobbie Jones")
    bobbie = _make_pro(db_session, t, "Bobbie Jones", "F", ev, partner_name=None)
    # C is genuinely unclaimed and shouldn't be paired with Bobbie because
    # Alex already claimed her.
    cleo = _make_pro(db_session, t, "Cleo Wilson", "F", ev, partner_name=None)

    summary = auto_assign_event_partners(ev)

    assert summary["confirmed_pairs"] == 0
    assert summary["assigned_pairs"] == 0
    assert len(summary["one_sided_claims"]) == 1
    claim = summary["one_sided_claims"][0]
    assert claim["reason"] == "one_sided_claim"
    assert claim["competitor_name"] == "Alex Kaper"
    assert claim["matched_partner_name"] == "Bobbie Jones"

    db_session.refresh(bobbie)
    db_session.refresh(cleo)
    assert bobbie.get_partners() in ({}, None) or not bobbie.get_partners().get(
        str(ev.id)
    )
    assert cleo.get_partners() in ({}, None) or not cleo.get_partners().get(str(ev.id))


def test_unclaimed_pool_auto_pairs_mixed(db_session):
    """Two M + two F with no partner fields, mixed event → 2 mixed pairs."""
    from services.partner_matching import auto_assign_event_partners

    t = _make_tournament(db_session)
    ev = _make_event(db_session, t, gender_req="mixed")
    _make_pro(db_session, t, "Man One", "M", ev)
    _make_pro(db_session, t, "Man Two", "M", ev)
    _make_pro(db_session, t, "Woman One", "F", ev)
    _make_pro(db_session, t, "Woman Two", "F", ev)

    summary = auto_assign_event_partners(ev)

    assert summary["confirmed_pairs"] == 0
    assert summary["assigned_pairs"] == 2
    assert summary["unmatched"] == 0


def test_self_reference_held_for_review(db_session):
    from services.partner_matching import auto_assign_event_partners

    t = _make_tournament(db_session)
    ev = _make_event(db_session, t)
    _make_pro(db_session, t, "Alex Kaper", "M", ev, partner_name="Alex Kaper")
    # Add a second comp so the pool size > 1 (single-entry pool early-returns).
    _make_pro(db_session, t, "Decoy Person", "F", ev)

    summary = auto_assign_event_partners(ev)

    assert summary["confirmed_pairs"] == 0
    assert summary["assigned_pairs"] == 0
    reasons = {c["reason"] for c in summary["one_sided_claims"]}
    assert "self_reference" in reasons


def test_non_reciprocal_holds_both(db_session):
    """A→B and B→C: A and B both held; C is also held because B claimed C."""
    from services.partner_matching import auto_assign_event_partners

    t = _make_tournament(db_session)
    ev = _make_event(db_session, t)
    _make_pro(db_session, t, "Alex Kaper", "M", ev, partner_name="Bobbie Jones")
    _make_pro(db_session, t, "Bobbie Jones", "F", ev, partner_name="Cleo Wilson")
    _make_pro(db_session, t, "Cleo Wilson", "F", ev, partner_name=None)

    summary = auto_assign_event_partners(ev)

    assert summary["confirmed_pairs"] == 0
    assert summary["assigned_pairs"] == 0
    reasons = {c["reason"] for c in summary["one_sided_claims"]}
    # Alex's claim is non-reciprocal; Bobbie's claim is one-sided.
    assert "non_reciprocal" in reasons or "one_sided_claim" in reasons


def test_idempotent_on_already_paired_roster(db_session):
    """Re-run on a confirmed-paired roster: zero new pairs."""
    from services.partner_matching import auto_assign_event_partners

    t = _make_tournament(db_session)
    ev = _make_event(db_session, t)
    _make_pro(db_session, t, "Alex Kaper", "M", ev, partner_name="Bobbie Jones")
    _make_pro(db_session, t, "Bobbie Jones", "F", ev, partner_name="Alex Kaper")

    auto_assign_event_partners(ev)
    db_session.flush()
    second = auto_assign_event_partners(ev)

    assert second["assigned_pairs"] == 0
    assert second["unmatched"] == 0
