"""Tests for services/print_catalog.py — catalog registry, status/fingerprint
functions, and the @record_print decorator.
"""

import pytest

from services import print_catalog

# ---------------------------------------------------------------------------
# Registry integrity
# ---------------------------------------------------------------------------


def test_catalog_has_expected_docs():
    """Every expected doc key is registered."""
    expected = {
        "woodboss_report",
        "day_schedule",
        "fnf_print",
        "fnf_pdf",
        "heat_sheets",
        "judge_sheet_all",
        "birling_blank",
        "gear_sharing_print",
        "pro_checkout",
        "relay_teams_sheet",
        "college_standings",
        "all_results",
        "pro_payouts",
        "event_results",
        "ala_report",
    }
    actual = {d.key for d in print_catalog.PRINT_DOCUMENTS}
    assert (
        expected == actual
    ), f"Mismatch: expected-actual={expected - actual}, actual-expected={actual - expected}"


def test_catalog_no_duplicate_keys():
    keys = [d.key for d in print_catalog.PRINT_DOCUMENTS]
    assert len(keys) == len(set(keys)), "Duplicate doc_key in PRINT_DOCUMENTS"


def test_catalog_every_doc_has_required_fields():
    for d in print_catalog.PRINT_DOCUMENTS:
        assert d.key and isinstance(d.key, str)
        assert d.label and isinstance(d.label, str)
        assert d.section in print_catalog.SECTIONS_ORDER
        assert callable(d.status_fn)
        assert callable(d.fingerprint_fn)
        assert d.route_endpoint and "." in d.route_endpoint


def test_catalog_every_endpoint_registered_in_app(app):
    rules = {r.endpoint for r in app.url_map.iter_rules()}
    for d in print_catalog.PRINT_DOCUMENTS:
        assert (
            d.route_endpoint in rules
        ), f"{d.key} -> {d.route_endpoint} not registered"


def test_dynamic_docs_have_enumerate_fn():
    for d in print_catalog.PRINT_DOCUMENTS:
        if d.dynamic:
            assert callable(d.enumerate_fn), f"{d.key}: dynamic doc needs enumerate_fn"


# ---------------------------------------------------------------------------
# Status functions — empty tournament → not configured
# ---------------------------------------------------------------------------


@pytest.fixture()
def empty_tournament(app, db_session):
    from tests.conftest import make_tournament

    return make_tournament(db_session)


def test_status_heat_sheets_empty(empty_tournament):
    status = print_catalog._status_heat_sheets(empty_tournament)
    assert status.configured is False
    assert "heats" in (status.reason or "").lower()


def test_status_day_schedule_empty(empty_tournament):
    status = print_catalog._status_day_schedule(empty_tournament)
    assert status.configured is False


def test_status_fnf_empty(empty_tournament):
    status = print_catalog._status_fnf(empty_tournament)
    assert status.configured is False


def test_status_college_standings_empty(empty_tournament):
    status = print_catalog._status_college_standings(empty_tournament)
    assert status.configured is False


def test_status_all_results_empty(empty_tournament):
    status = print_catalog._status_all_results(empty_tournament)
    assert status.configured is False


def test_status_pro_payouts_empty(empty_tournament):
    status = print_catalog._status_pro_payouts(empty_tournament)
    assert status.configured is False


def test_status_ala_empty(empty_tournament):
    status = print_catalog._status_ala(empty_tournament)
    assert status.configured is False


def test_status_pro_checkout_empty(empty_tournament):
    status = print_catalog._status_pro_checkout(empty_tournament)
    assert status.configured is False


def test_status_woodboss_empty(empty_tournament):
    status = print_catalog._status_woodboss(empty_tournament)
    assert status.configured is False


# ---------------------------------------------------------------------------
# Status flips when minimum data is present
# ---------------------------------------------------------------------------


def test_status_pro_checkout_with_one_pro(app, db_session):
    from tests.conftest import make_pro_competitor, make_tournament

    t = make_tournament(db_session)
    make_pro_competitor(db_session, t, "Alice", gender="F")
    status = print_catalog._status_pro_checkout(t)
    assert status.configured is True


def test_status_event_results_requires_finalization(app, db_session):
    from tests.conftest import make_event, make_tournament

    t = make_tournament(db_session)
    e = make_event(db_session, t, "Underhand", is_open=False)
    status = print_catalog._status_event_results(t, e)
    assert status.configured is False
    assert "finalized" in (status.reason or "").lower()
    e.is_finalized = True
    db_session.flush()
    status = print_catalog._status_event_results(t, e)
    assert status.configured is True


def test_status_event_results_missing_entity():
    from models import Tournament

    t = Tournament(name="x", year=2026)
    status = print_catalog._status_event_results(t, None)
    assert status.configured is False


# ---------------------------------------------------------------------------
# Fingerprints are deterministic and change with data
# ---------------------------------------------------------------------------


def test_fingerprint_deterministic(empty_tournament):
    fp1 = print_catalog._fp_pro_checkout(empty_tournament)
    fp2 = print_catalog._fp_pro_checkout(empty_tournament)
    assert fp1 == fp2


def test_fingerprint_changes_on_scratch(app, db_session):
    from tests.conftest import make_pro_competitor, make_tournament

    t = make_tournament(db_session)
    make_pro_competitor(db_session, t, "Alice", gender="F")
    bob = make_pro_competitor(db_session, t, "Bob", gender="M")
    db_session.flush()
    fp_before = print_catalog._fp_heat_sheets(t)
    bob.status = "scratched"
    db_session.flush()
    fp_after = print_catalog._fp_heat_sheets(t)
    assert fp_before != fp_after


def test_fingerprint_length_under_64_chars(empty_tournament):
    for doc in print_catalog.PRINT_DOCUMENTS:
        if doc.dynamic:
            continue
        fp = doc.fingerprint_fn(empty_tournament)
        assert isinstance(fp, str)
        assert 0 < len(fp) <= 64


def test_fingerprint_empty_tournament_doesnt_raise(empty_tournament):
    for doc in print_catalog.PRINT_DOCUMENTS:
        if doc.dynamic:
            continue
        doc.fingerprint_fn(empty_tournament)  # should not raise


# ---------------------------------------------------------------------------
# upsert_tracker
# ---------------------------------------------------------------------------


def test_upsert_tracker_inserts_new_row(app, db_session):
    from models import PrintTracker
    from tests.conftest import make_tournament

    t = make_tournament(db_session)
    db_session.commit()
    print_catalog.upsert_tracker(t.id, "heat_sheets", None, "fp1", None)
    rows = PrintTracker.query.filter_by(tournament_id=t.id).all()
    assert len(rows) == 1
    assert rows[0].doc_key == "heat_sheets"
    assert rows[0].entity_id is None
    assert rows[0].last_printed_fingerprint == "fp1"


def test_upsert_tracker_updates_existing_row(app, db_session):
    from models import PrintTracker
    from tests.conftest import make_tournament

    t = make_tournament(db_session)
    db_session.commit()
    print_catalog.upsert_tracker(t.id, "heat_sheets", None, "fp1", None)
    print_catalog.upsert_tracker(t.id, "heat_sheets", None, "fp2", None)
    rows = PrintTracker.query.filter_by(tournament_id=t.id).all()
    assert len(rows) == 1
    assert rows[0].last_printed_fingerprint == "fp2"


def test_upsert_tracker_distinguishes_by_entity_id(app, db_session):
    from models import PrintTracker
    from tests.conftest import make_tournament

    t = make_tournament(db_session)
    db_session.commit()
    print_catalog.upsert_tracker(t.id, "event_results", 1, "fp-e1", None)
    print_catalog.upsert_tracker(t.id, "event_results", 2, "fp-e2", None)
    rows = PrintTracker.query.filter_by(tournament_id=t.id).all()
    assert len(rows) == 2
    assert {r.entity_id for r in rows} == {1, 2}


# ---------------------------------------------------------------------------
# Hub row builder
# ---------------------------------------------------------------------------


def test_build_hub_rows_empty_tournament(app, db_session):
    from tests.conftest import make_tournament

    t = make_tournament(db_session)
    db_session.commit()
    rows = print_catalog.build_hub_rows(t)
    # Fixed rows only (no events → no dynamic rows).
    assert len(rows) == len([d for d in print_catalog.PRINT_DOCUMENTS if not d.dynamic])
    # All rows should be not-configured on an empty tournament.
    for row in rows:
        assert row.status.configured is False
        assert row.never_printed is True
        assert row.stale is False


def test_build_hub_rows_dynamic_expands_per_event(app, db_session):
    from tests.conftest import make_event, make_tournament

    t = make_tournament(db_session)
    make_event(db_session, t, "Underhand", gender="M")
    make_event(db_session, t, "Standing Block", gender="F")
    db_session.commit()
    rows = print_catalog.build_hub_rows(t)
    event_rows = [r for r in rows if r.doc.key == "event_results"]
    assert len(event_rows) == 2


def test_build_hub_rows_fresh_after_print(app, db_session):
    from tests.conftest import make_pro_competitor, make_tournament

    t = make_tournament(db_session)
    make_pro_competitor(db_session, t, "Alice", gender="F")
    db_session.commit()
    # Record a print with the CURRENT fingerprint.
    fp = print_catalog._fp_pro_checkout(t)
    print_catalog.upsert_tracker(t.id, "pro_checkout", None, fp, None)
    rows = print_catalog.build_hub_rows(t)
    pc = next(r for r in rows if r.doc.key == "pro_checkout")
    assert pc.status.configured is True
    assert pc.never_printed is False
    assert pc.stale is False


def test_build_hub_rows_stale_after_data_change(app, db_session):
    from tests.conftest import make_pro_competitor, make_tournament

    t = make_tournament(db_session)
    make_pro_competitor(db_session, t, "Alice", gender="F")
    db_session.commit()
    fp = print_catalog._fp_pro_checkout(t)
    print_catalog.upsert_tracker(t.id, "pro_checkout", None, fp, None)
    make_pro_competitor(db_session, t, "Bob", gender="M")
    db_session.commit()
    rows = print_catalog.build_hub_rows(t)
    pc = next(r for r in rows if r.doc.key == "pro_checkout")
    assert pc.stale is True
    assert pc.never_printed is False


# ---------------------------------------------------------------------------
# @record_print decorator behavior (unit-level)
# ---------------------------------------------------------------------------


def test_record_print_writes_tracker(app, db_session):
    from models import PrintTracker
    from tests.conftest import make_pro_competitor, make_tournament

    t = make_tournament(db_session)
    make_pro_competitor(db_session, t, "Alice", gender="F")
    db_session.commit()

    @print_catalog.record_print("pro_checkout")
    def fake_view(tournament_id):
        return "ok"

    with app.test_request_context("/"):
        fake_view(tournament_id=t.id)

    rows = PrintTracker.query.filter_by(
        tournament_id=t.id, doc_key="pro_checkout"
    ).all()
    assert len(rows) == 1


def test_record_print_does_not_mutate_response(app, db_session):
    from tests.conftest import make_tournament

    t = make_tournament(db_session)
    db_session.commit()

    sentinel = ("body", 200, {"X-Test": "yes"})

    @print_catalog.record_print("heat_sheets")
    def fake_view(tournament_id):
        return sentinel

    with app.test_request_context("/"):
        result = fake_view(tournament_id=t.id)
    assert result is sentinel


def test_record_print_swallows_tracker_failure(app, db_session, monkeypatch):
    from tests.conftest import make_tournament

    t = make_tournament(db_session)
    db_session.commit()

    def boom(*args, **kwargs):
        raise RuntimeError("tracker exploded")

    monkeypatch.setattr(print_catalog, "upsert_tracker", boom)

    @print_catalog.record_print("heat_sheets")
    def fake_view(tournament_id):
        return "still-ok"

    with app.test_request_context("/"):
        # Decorator must not propagate the tracker failure — print wins.
        assert fake_view(tournament_id=t.id) == "still-ok"


def test_record_print_no_write_when_view_raises(app, db_session):
    from models import PrintTracker
    from tests.conftest import make_tournament

    t = make_tournament(db_session)
    db_session.commit()

    @print_catalog.record_print("heat_sheets")
    def fake_view(tournament_id):
        raise ValueError("view error")

    with app.test_request_context("/"):
        with pytest.raises(ValueError):
            fake_view(tournament_id=t.id)

    rows = PrintTracker.query.filter_by(tournament_id=t.id).all()
    assert len(rows) == 0
