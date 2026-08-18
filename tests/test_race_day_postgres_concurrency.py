"""Real PostgreSQL races for race-day state transitions.

Run this module with ``PROAM_UNIT_PG=1``. The shared test factory clones a
migrated PostgreSQL template into a private synthetic database; SQLite runs
collect the module but skip every test because they cannot certify row locks.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from uuid import uuid4

import pytest

from database import db
from tests.db_test_utils import create_test_app, drop_test_db

pytestmark = pytest.mark.integration

_WAIT_SECONDS = 10


@pytest.fixture(scope="module")
def app():
    previous_testing = os.environ.get("TESTING")
    os.environ["TESTING"] = "1"
    try:
        test_app, handle = create_test_app()
    finally:
        if previous_testing is None:
            os.environ.pop("TESTING", None)
        else:
            os.environ["TESTING"] = previous_testing
    try:
        with test_app.app_context():
            dialect = db.engine.dialect.name
        if dialect != "postgresql":
            pytest.skip(
                "race-day concurrency certification requires "
                "PROAM_UNIT_PG=1 and the isolated PostgreSQL test factory"
            )
        yield test_app
    finally:
        with test_app.app_context():
            db.session.remove()
            db.engine.dispose()
        drop_test_db(handle)


def _new_admin(app, label: str) -> int:
    from models import User

    with app.app_context():
        user = User(username=f"{label}-{uuid4().hex}", role=User.ROLE_ADMIN)
        user.set_password("postgres-race-test")
        db.session.add(user)
        db.session.commit()
        user_id = user.id
        db.session.remove()
        return user_id


def _authenticated_client(app, user_id: int):
    client = app.test_client()
    with client.session_transaction() as session:
        session["_user_id"] = str(user_id)
        session["_fresh"] = True
    return client


def _start_thread(*, name: str, target, done: threading.Event):
    errors = []

    def guarded():
        try:
            target()
        except BaseException as exc:  # pragma: no cover - surfaced in caller
            errors.append(exc)
        finally:
            done.set()

    thread = threading.Thread(name=name, target=guarded)
    thread.start()
    return thread, errors


def _join(thread: threading.Thread, errors: list[BaseException]) -> None:
    thread.join(_WAIT_SECONDS)
    assert not thread.is_alive(), f"{thread.name} did not finish"
    assert errors == []


def _assert_peer_waits(
    peer_started: threading.Event,
    peer_done: threading.Event,
) -> None:
    assert peer_started.wait(_WAIT_SECONDS)
    assert not peer_done.wait(0.35), (
        "the peer transaction finished while the first PostgreSQL writer "
        "still held the tournament row lock"
    )


def _seed_score(app, user_id: int) -> dict:
    from models import User
    from services.scoring_workflow import heat_submission_identity
    from tests.conftest import (
        make_college_competitor,
        make_event,
        make_heat,
        make_team,
        make_tournament,
    )

    with app.app_context():
        session = db.session
        tournament = make_tournament(session, name=f"PG Receipt Race {uuid4()}", year=2097)
        team = make_team(
            session,
            tournament,
            code=f"PG-{uuid4().hex[:8]}",
            school="Synthetic PostgreSQL College",
        )
        competitor = make_college_competitor(
            session,
            tournament,
            team,
            f"Synthetic Scorer {uuid4().hex[:8]}",
        )
        event = make_event(
            session,
            tournament,
            "Standing Block Hard Hit",
            event_type="college",
            scoring_type="hits",
            scoring_order="highest_wins",
        )
        heat = make_heat(
            session,
            event,
            competitors=[competitor.id],
            stand_assignments={str(competitor.id): 1},
        )
        request_id = str(uuid4())
        form_data = {
            "request_id": request_id,
            "tournament_id": str(tournament.id),
            "heat_id": str(heat.id),
            "issuer_user_id": str(user_id),
            "issuer_role": User.ROLE_ADMIN,
            "schedule_fingerprint": "synthetic-pg-schedule",
            "queued_at": datetime.now(timezone.utc).isoformat(),
            "heat_version": str(heat.version_id),
            "heat_identity": heat_submission_identity(heat),
            f"result_{competitor.id}": "9",
            f"status_{competitor.id}": "completed",
            f"reason_{competitor.id}": "",
        }
        ids = {
            "tournament_id": tournament.id,
            "event_id": event.id,
            "heat_id": heat.id,
            "competitor_id": competitor.id,
            "request_id": request_id,
            "form_data": form_data,
        }
        session.commit()
        session.remove()
        return ids


def test_same_uuid_score_race_is_single_commit_and_replayable(
    app,
    monkeypatch,
):
    """An overlapped lost response produces one score, audit, and receipt."""
    from models import AuditLog, Event, EventResult, Heat, ScoreSubmissionReceipt
    from services import scoring_workflow
    from services.scoring_workflow import save_heat_results_submission

    user_id = _new_admin(app, "pg-score-operator")
    seeded = _seed_score(app, user_id)
    first_at_receipt = threading.Event()
    release_first = threading.Event()
    second_started = threading.Event()
    first_done = threading.Event()
    second_done = threading.Event()
    outcomes = {}
    original_create_receipt = scoring_workflow._create_submission_receipt

    def hold_first_receipt(*args, **kwargs):
        receipt = original_create_receipt(*args, **kwargs)
        if threading.current_thread().name == "score-first":
            first_at_receipt.set()
            assert release_first.wait(_WAIT_SECONDS)
        return receipt

    monkeypatch.setattr(
        scoring_workflow,
        "_create_submission_receipt",
        hold_first_receipt,
    )

    def submit(slot: str, started: threading.Event | None = None):
        if started is not None:
            started.set()
        with app.app_context():
            heat = db.session.get(Heat, seeded["heat_id"])
            event = db.session.get(Event, seeded["event_id"])
            outcomes[slot] = save_heat_results_submission(
                tournament_id=seeded["tournament_id"],
                heat=heat,
                event=event,
                form_data=dict(seeded["form_data"]),
                judge_user_id=user_id,
            )
            db.session.remove()

    first_thread, first_errors = _start_thread(
        name="score-first",
        target=lambda: submit("first"),
        done=first_done,
    )
    assert first_at_receipt.wait(_WAIT_SECONDS)
    second_thread, second_errors = _start_thread(
        name="score-second",
        target=lambda: submit("second", second_started),
        done=second_done,
    )
    try:
        _assert_peer_waits(second_started, second_done)
    finally:
        release_first.set()
    _join(first_thread, first_errors)
    _join(second_thread, second_errors)

    assert outcomes["first"]["ok"] is True
    assert outcomes["first"]["receipt"]["request_id"] == seeded["request_id"]
    assert outcomes["second"]["ok"] is True
    assert outcomes["second"]["receipt_replayed"] is True
    assert outcomes["second"]["receipt"]["request_id"] == seeded["request_id"]

    with app.app_context():
        replay = save_heat_results_submission(
            tournament_id=seeded["tournament_id"],
            heat=db.session.get(Heat, seeded["heat_id"]),
            event=db.session.get(Event, seeded["event_id"]),
            form_data=dict(seeded["form_data"]),
            judge_user_id=user_id,
        )
        assert replay["ok"] is True
        assert replay["receipt_replayed"] is True
        assert replay["receipt"]["request_id"] == seeded["request_id"]
        assert ScoreSubmissionReceipt.query.filter_by(request_id=seeded["request_id"]).count() == 1
        result = EventResult.query.filter_by(
            event_id=seeded["event_id"],
            competitor_id=seeded["competitor_id"],
        ).one()
        assert result.result_value == 9
        assert (
            AuditLog.query.filter_by(
                action="heat_results_saved",
                entity_type="heat",
                entity_id=seeded["heat_id"],
            ).count()
            == 1
        )


def test_receipt_lifecycle_preserves_history_and_revokes_deleted_issuer(app):
    from models import Heat, ScoreSubmissionReceipt, Tournament, User
    from services.scoring_workflow import save_heat_results_submission

    user_id = _new_admin(app, "pg-receipt-lifecycle")
    seeded = _seed_score(app, user_id)

    with app.app_context():
        heat = db.session.get(Heat, seeded["heat_id"])
        outcome = save_heat_results_submission(
            tournament_id=seeded["tournament_id"],
            heat=heat,
            event=heat.event,
            form_data=seeded["form_data"],
            judge_user_id=user_id,
        )
        assert outcome["ok"] is True
        db.session.commit()

        db.session.delete(heat)
        db.session.commit()
        receipt = db.session.get(
            ScoreSubmissionReceipt, seeded["request_id"]
        )
        assert receipt is not None
        assert receipt.heat_id == seeded["heat_id"]

        removable_user = User(
            username=f"pg-removable-receipt-{uuid4().hex}",
            role=User.ROLE_ADMIN,
        )
        removable_user.set_password("postgres-race-test")
        db.session.add(removable_user)
        db.session.flush()
        removable_request_id = str(uuid4())
        db.session.add(ScoreSubmissionReceipt(
            request_id=removable_request_id,
            tournament_id=seeded["tournament_id"],
            heat_id=seeded["heat_id"],
            issuing_user_id=removable_user.id,
            canonical_payload_sha256="d" * 64,
            accepted_outcome_json={"ok": True},
        ))
        db.session.commit()
        db.session.delete(removable_user)
        db.session.commit()
        receipt = db.session.get(
            ScoreSubmissionReceipt, removable_request_id
        )
        assert receipt is not None
        assert receipt.issuing_user_id is None

        tournament = db.session.get(Tournament, seeded["tournament_id"])
        db.session.delete(tournament)
        db.session.commit()
        assert db.session.get(
            ScoreSubmissionReceipt, seeded["request_id"]
        ) is None


def _seed_relay(app) -> dict:
    from models import Event, Tournament
    from services.proam_relay import ProAmRelay

    events = {key: {"result": None, "status": "pending"} for key in ProAmRelay.RELAY_EVENTS}
    state = {
        "status": "drawn",
        "teams": [
            {
                "team_number": 1,
                "name": "Synthetic Team 1",
                "pro_members": [],
                "college_members": [],
                "events": events,
                "total_time": None,
            }
        ],
        "eligible_college": [],
        "eligible_pro": [],
        "drawn_college": [],
        "drawn_pro": [],
    }
    with app.app_context():
        tournament = Tournament(name=f"PG Relay Race {uuid4()}", year=2097, status="setup")
        db.session.add(tournament)
        db.session.flush()
        relay_event = Event(
            tournament_id=tournament.id,
            name="Pro-Am Relay",
            event_type="pro",
            scoring_type="time",
            scoring_order="lowest_wins",
            is_partnered=True,
            event_state=json.dumps(state),
        )
        db.session.add(relay_event)
        db.session.commit()
        digest = ProAmRelay(tournament).team_state_digest(1)
        ids = {"tournament_id": tournament.id, "digest": digest}
        db.session.remove()
        return ids


def test_same_relay_team_race_commits_one_reviewed_snapshot(
    app,
    monkeypatch,
):
    from models import Event, RelayState, RelayTeam, RelayTeamEvent, Tournament
    from services.proam_relay import ProAmRelay

    user_id = _new_admin(app, "pg-relay-operator")
    seeded = _seed_relay(app)
    first_inside_writer = threading.Event()
    release_first = threading.Event()
    second_started = threading.Event()
    first_done = threading.Event()
    second_done = threading.Event()
    responses = {}
    original_record = ProAmRelay.record_total_time

    def hold_first_writer(self, *args, **kwargs):
        if threading.current_thread().name == "relay-first":
            first_inside_writer.set()
            assert release_first.wait(_WAIT_SECONDS)
        return original_record(self, *args, **kwargs)

    monkeypatch.setattr(ProAmRelay, "record_total_time", hold_first_writer)

    def post_result(slot: str, value: str, started=None):
        if started is not None:
            started.set()
        responses[slot] = _authenticated_client(app, user_id).post(
            f"/tournament/{seeded['tournament_id']}/proam-relay/results",
            data={
                "team_number": "1",
                "time_seconds": value,
                "expected_relay_digest": seeded["digest"],
            },
            follow_redirects=False,
        )

    first_thread, first_errors = _start_thread(
        name="relay-first",
        target=lambda: post_result("first", "101.25"),
        done=first_done,
    )
    assert first_inside_writer.wait(_WAIT_SECONDS)
    second_thread, second_errors = _start_thread(
        name="relay-second",
        target=lambda: post_result("second", "202.50", second_started),
        done=second_done,
    )
    try:
        _assert_peer_waits(second_started, second_done)
    finally:
        release_first.set()
    _join(first_thread, first_errors)
    _join(second_thread, second_errors)

    assert responses["first"].status_code in (302, 303)
    assert responses["second"].status_code == 409
    with app.app_context():
        tournament = db.session.get(Tournament, seeded["tournament_id"])
        relay = ProAmRelay(tournament)
        team = relay.get_teams()[0]
        assert team["total_time"] == 101.25
        assert relay.get_status() == "completed"
        relay_event = Event.query.filter_by(
            tournament_id=seeded["tournament_id"],
            name="Pro-Am Relay",
        ).one()
        state = RelayState.query.filter_by(event_id=relay_event.id).one()
        assert RelayTeam.query.filter_by(relay_state_id=state.id).count() == 1
        persisted_team = RelayTeam.query.filter_by(relay_state_id=state.id, team_number=1).one()
        assert persisted_team.total_time == 101.25
        assert RelayTeamEvent.query.filter_by(relay_team_id=persisted_team.id).count() == len(
            ProAmRelay.RELAY_EVENTS
        )


def test_two_stale_relay_payout_forms_commit_one_reviewed_snapshot(
    app,
    monkeypatch,
):
    from models import Event, Tournament
    from routes.proam_relay import _relay_payout_state_digest

    user_id = _new_admin(app, "pg-relay-payout-operator")
    seeded = _seed_relay(app)
    with app.app_context():
        tournament = db.session.get(Tournament, seeded["tournament_id"])
        relay_event = Event.query.filter_by(
            tournament_id=seeded["tournament_id"],
            name="Pro-Am Relay",
        ).one()
        stale_digest = _relay_payout_state_digest(tournament, relay_event)
        db.session.remove()

    first_inside_writer = threading.Event()
    release_first = threading.Event()
    second_started = threading.Event()
    first_done = threading.Event()
    second_done = threading.Event()
    responses = {}
    original_set_payouts = Event.set_payouts

    def hold_first_writer(self, payout_dict):
        if threading.current_thread().name == "relay-payout-first":
            first_inside_writer.set()
            assert release_first.wait(_WAIT_SECONDS)
        return original_set_payouts(self, payout_dict)

    monkeypatch.setattr(Event, "set_payouts", hold_first_writer)

    def post_payouts(slot: str, amount: str, started=None):
        if started is not None:
            started.set()
        responses[slot] = _authenticated_client(app, user_id).post(
            f"/tournament/{seeded['tournament_id']}/proam-relay/payouts",
            data={
                "expected_payout_digest": stale_digest,
                "payout_1": amount,
            },
            follow_redirects=False,
        )

    first_thread, first_errors = _start_thread(
        name="relay-payout-first",
        target=lambda: post_payouts("first", "800.00"),
        done=first_done,
    )
    assert first_inside_writer.wait(_WAIT_SECONDS)
    second_thread, second_errors = _start_thread(
        name="relay-payout-second",
        target=lambda: post_payouts("second", "1600.00", second_started),
        done=second_done,
    )
    try:
        _assert_peer_waits(second_started, second_done)
    finally:
        release_first.set()
    _join(first_thread, first_errors)
    _join(second_thread, second_errors)

    assert responses["first"].status_code in (302, 303)
    assert responses["second"].status_code == 409
    assert b"Relay payouts or results changed" in responses["second"].data
    with app.app_context():
        relay_event = Event.query.filter_by(
            tournament_id=seeded["tournament_id"],
            name="Pro-Am Relay",
        ).one()
        assert relay_event.get_payouts() == {"1": 800.0}


def _seed_partner_claim(app) -> dict:
    from models import Event, EventResult, Tournament
    from models.competitor import ProCompetitor
    from services.partner_matching import partner_claim_digest

    with app.app_context():
        tournament = Tournament(name=f"PG Partner Race {uuid4()}", year=2097, status="setup")
        db.session.add(tournament)
        db.session.flush()
        event = Event(
            tournament_id=tournament.id,
            name="Partnered Axe Throw",
            event_type="pro",
            scoring_type="score",
            scoring_order="highest_wins",
            is_partnered=True,
            partner_gender_requirement="any",
        )
        db.session.add(event)
        db.session.flush()

        def competitor(name: str, *, status="active", partner=""):
            row = ProCompetitor(
                tournament_id=tournament.id,
                name=name,
                gender="F",
                status=status,
                events_entered=json.dumps([event.id]),
                partners=json.dumps({str(event.id): partner} if partner else {}),
            )
            db.session.add(row)
            db.session.flush()
            return row

        alice = competitor("Synthetic Alice", partner="Synthetic Bob")
        competitor("Synthetic Bob", status="scratched", partner="Synthetic Alice")
        dana = competitor("Synthetic Dana", partner="Synthetic Evan")
        competitor("Synthetic Evan", status="scratched", partner="Synthetic Dana")
        candidate = competitor("Synthetic Carol")
        for orphan, old_name in (
            (alice, "Synthetic Bob"),
            (dana, "Synthetic Evan"),
        ):
            db.session.add(
                EventResult(
                    event_id=event.id,
                    competitor_id=orphan.id,
                    competitor_type="pro",
                    competitor_name=orphan.name,
                    partner_name=old_name,
                    status="pending",
                )
            )
        db.session.flush()
        ids = {
            "tournament_id": tournament.id,
            "event_id": event.id,
            "alice_id": alice.id,
            "dana_id": dana.id,
            "candidate_id": candidate.id,
            "alice_digest": partner_claim_digest(event, alice, candidate),
            "dana_digest": partner_claim_digest(event, dana, candidate),
        }
        db.session.commit()
        db.session.remove()
        return ids


def test_two_orphans_cannot_claim_the_same_partner(
    app,
    monkeypatch,
):
    from models.competitor import ProCompetitor
    from routes.scheduling import partners as partner_routes

    user_id = _new_admin(app, "pg-partner-operator")
    seeded = _seed_partner_claim(app)
    first_inside_writer = threading.Event()
    release_first = threading.Event()
    second_started = threading.Event()
    first_done = threading.Event()
    second_done = threading.Event()
    responses = {}
    original_set_partner = partner_routes.set_partner_bidirectional

    def hold_first_writer(a, b, event):
        if threading.current_thread().name == "partner-first":
            first_inside_writer.set()
            assert release_first.wait(_WAIT_SECONDS)
        return original_set_partner(a, b, event)

    monkeypatch.setattr(
        partner_routes,
        "set_partner_bidirectional",
        hold_first_writer,
    )

    def post_claim(slot: str, orphan_id: int, digest: str, started=None):
        if started is not None:
            started.set()
        responses[slot] = _authenticated_client(app, user_id).post(
            f"/scheduling/{seeded['tournament_id']}/events/{seeded['event_id']}/reassign-partner",
            data={
                "orphan_id": str(orphan_id),
                "orphan_type": "pro",
                "new_partner_id": str(seeded["candidate_id"]),
                "new_partner_type": "pro",
                "expected_claim_digest": digest,
            },
            follow_redirects=False,
        )

    first_thread, first_errors = _start_thread(
        name="partner-first",
        target=lambda: post_claim("first", seeded["alice_id"], seeded["alice_digest"]),
        done=first_done,
    )
    assert first_inside_writer.wait(_WAIT_SECONDS)
    second_thread, second_errors = _start_thread(
        name="partner-second",
        target=lambda: post_claim(
            "second", seeded["dana_id"], seeded["dana_digest"], second_started
        ),
        done=second_done,
    )
    try:
        _assert_peer_waits(second_started, second_done)
    finally:
        release_first.set()
    _join(first_thread, first_errors)
    _join(second_thread, second_errors)

    assert responses["first"].status_code in (302, 303)
    assert responses["second"].status_code == 409
    with app.app_context():
        alice = db.session.get(ProCompetitor, seeded["alice_id"])
        dana = db.session.get(ProCompetitor, seeded["dana_id"])
        candidate = db.session.get(ProCompetitor, seeded["candidate_id"])
        event_key = str(seeded["event_id"])
        assert alice.get_partners()[event_key] == candidate.name
        assert candidate.get_partners()[event_key] == alice.name
        assert dana.get_partners()[event_key] == "Synthetic Evan"
        inbound = [
            row.id for row in (alice, dana) if row.get_partners().get(event_key) == candidate.name
        ]
        assert inbound == [alice.id]


def _seed_flight(app) -> dict:
    from models import Event, Flight, Heat, Tournament
    from routes.scheduling.flights import flight_order_digest

    with app.app_context():
        tournament = Tournament(name=f"PG Flight Race {uuid4()}", year=2097, status="setup")
        db.session.add(tournament)
        db.session.flush()
        flight = Flight(
            tournament_id=tournament.id,
            flight_number=1,
            status="pending",
        )
        db.session.add(flight)
        db.session.flush()
        heat_ids = []
        for position in (1, 2):
            event = Event(
                tournament_id=tournament.id,
                name=f"Synthetic Flight Event {uuid4().hex[:8]}",
                event_type="pro",
                scoring_type="time",
                scoring_order="lowest_wins",
                status="pending",
            )
            db.session.add(event)
            db.session.flush()
            heat = Heat(
                event_id=event.id,
                heat_number=1,
                run_number=1,
                status="pending",
                flight_id=flight.id,
                flight_position=position,
            )
            heat.set_roster("pro", [])
            db.session.add(heat)
            db.session.flush()
            heat_ids.append(heat.id)
        db.session.commit()
        ids = {
            "tournament_id": tournament.id,
            "flight_id": flight.id,
            "heat_ids": heat_ids,
            "digest": flight_order_digest(flight.id),
        }
        db.session.remove()
        return ids


def test_flight_start_wins_over_overlapped_reorder(
    app,
    monkeypatch,
):
    from models import Flight, Heat
    from routes.scheduling import flights as flight_routes

    user_id = _new_admin(app, "pg-flight-operator")
    seeded = _seed_flight(app)
    first_inside_writer = threading.Event()
    release_first = threading.Event()
    second_started = threading.Event()
    first_done = threading.Event()
    second_done = threading.Event()
    responses = {}

    def hold_start_transaction(*_args, **_kwargs):
        first_inside_writer.set()
        assert release_first.wait(_WAIT_SECONDS)

    monkeypatch.setattr(
        flight_routes,
        "_send_upcoming_heat_sms",
        hold_start_transaction,
    )

    def start_flight():
        responses["start"] = _authenticated_client(app, user_id).post(
            f"/scheduling/{seeded['tournament_id']}/flights/{seeded['flight_id']}/start",
            follow_redirects=False,
        )

    def reorder_flight():
        second_started.set()
        responses["reorder"] = _authenticated_client(app, user_id).post(
            f"/scheduling/{seeded['tournament_id']}/flights/{seeded['flight_id']}/reorder",
            json={
                "heat_ids": list(reversed(seeded["heat_ids"])),
                "expected_digest": seeded["digest"],
            },
        )

    first_thread, first_errors = _start_thread(
        name="flight-start", target=start_flight, done=first_done
    )
    assert first_inside_writer.wait(_WAIT_SECONDS)
    second_thread, second_errors = _start_thread(
        name="flight-reorder", target=reorder_flight, done=second_done
    )
    try:
        _assert_peer_waits(second_started, second_done)
    finally:
        release_first.set()
    _join(first_thread, first_errors)
    _join(second_thread, second_errors)

    assert responses["start"].status_code in (302, 303)
    assert responses["reorder"].status_code == 409
    assert responses["reorder"].get_json()["code"] == "active_flight"
    with app.app_context():
        assert db.session.get(Flight, seeded["flight_id"]).status == "in_progress"
        persisted_order = [
            heat.id
            for heat in Heat.query.filter_by(flight_id=seeded["flight_id"]).order_by(
                Heat.flight_position, Heat.id
            )
        ]
        assert persisted_order == seeded["heat_ids"]
        positions = [
            heat.flight_position for heat in Heat.query.filter_by(flight_id=seeded["flight_id"])
        ]
        assert sorted(positions) == [1, 2]


def _seed_birling(app) -> dict:
    from services.birling_bracket import BirlingBracket
    from tests.conftest import (
        make_college_competitor,
        make_event,
        make_team,
        make_tournament,
    )

    with app.app_context():
        tournament = make_tournament(db.session, name=f"PG Birling Race {uuid4()}", year=2097)
        team = make_team(
            db.session,
            tournament,
            code=f"BR-{uuid4().hex[:8]}",
            school="Synthetic Birling College",
        )
        people = [
            make_college_competitor(
                db.session,
                tournament,
                team,
                f"Synthetic Birler {index}-{uuid4().hex[:6]}",
            )
            for index in range(4)
        ]
        event = make_event(
            db.session,
            tournament,
            "Birling",
            event_type="college",
            scoring_type="bracket",
        )
        db.session.flush()
        bracket = BirlingBracket(event)
        bracket.generate_bracket(
            [{"id": person.id, "name": person.display_name} for person in people]
        )
        match = bracket.get_current_matches()[0]
        ids = {
            "tournament_id": tournament.id,
            "event_id": event.id,
            "match_id": match["match_id"],
            "fall_winner_id": match["competitor1"],
            "direct_winner_id": match["competitor2"],
            "digest": bracket.match_fall_digest(match["match_id"]),
        }
        db.session.remove()
        return ids


def test_birling_fall_wins_over_overlapped_direct_winner(
    app,
    monkeypatch,
):
    from models import Event
    from services.birling_bracket import BirlingBracket

    user_id = _new_admin(app, "pg-birling-operator")
    seeded = _seed_birling(app)
    first_inside_writer = threading.Event()
    release_first = threading.Event()
    second_started = threading.Event()
    first_done = threading.Event()
    second_done = threading.Event()
    responses = {}
    original_record_fall = BirlingBracket.record_fall

    def hold_first_fall(self, *args, **kwargs):
        first_inside_writer.set()
        assert release_first.wait(_WAIT_SECONDS)
        return original_record_fall(self, *args, **kwargs)

    monkeypatch.setattr(BirlingBracket, "record_fall", hold_first_fall)

    base = f"/scheduling/{seeded['tournament_id']}/event/{seeded['event_id']}/birling"

    def record_fall():
        responses["fall"] = _authenticated_client(app, user_id).post(
            f"{base}/fall",
            data={
                "match_id": seeded["match_id"],
                "fall_winner_id": str(seeded["fall_winner_id"]),
                "expected_fall_digest": seeded["digest"],
            },
            follow_redirects=False,
        )

    def declare_winner():
        second_started.set()
        responses["direct"] = _authenticated_client(app, user_id).post(
            f"{base}/record",
            data={
                "match_id": seeded["match_id"],
                "winner_id": str(seeded["direct_winner_id"]),
                "expected_fall_digest": seeded["digest"],
            },
            follow_redirects=False,
        )

    first_thread, first_errors = _start_thread(
        name="birling-fall", target=record_fall, done=first_done
    )
    assert first_inside_writer.wait(_WAIT_SECONDS)
    second_thread, second_errors = _start_thread(
        name="birling-direct", target=declare_winner, done=second_done
    )
    try:
        _assert_peer_waits(second_started, second_done)
    finally:
        release_first.set()
    _join(first_thread, first_errors)
    _join(second_thread, second_errors)

    assert responses["fall"].status_code in (302, 303)
    assert responses["direct"].status_code == 409
    assert responses["direct"].headers["X-Retryable"] == "true"
    with app.app_context():
        event = db.session.get(Event, seeded["event_id"])
        match = BirlingBracket(event)._find_match(seeded["match_id"])
        assert match["winner"] is None
        assert match["loser"] is None
        assert [fall["winner"] for fall in match["falls"]] == [seeded["fall_winner_id"]]


def test_late_job_completion_cannot_overwrite_reconciliation(
    app,
    monkeypatch,
):
    from datetime import timedelta

    from models import BackgroundJob
    from services import background_jobs
    from services.time_utils import utc_now_naive

    old_boot_id = uuid4().hex
    replacement_boot_id = uuid4().hex
    job_id = uuid4().hex
    now = utc_now_naive()
    with app.app_context():
        db.session.add(
            BackgroundJob(
                id=job_id,
                label="Synthetic late completion",
                status="running",
                owner_boot_id=old_boot_id,
                owner_heartbeat_at=now - timedelta(seconds=31),
                submitted_at=now - timedelta(minutes=1),
                started_at=now - timedelta(seconds=45),
            )
        )
        db.session.commit()
        db.session.remove()

    reconciliation_flushed = threading.Event()
    release_reconciliation = threading.Event()
    completion_started = threading.Event()
    reconciliation_done = threading.Event()
    completion_done = threading.Event()
    outcomes = {}
    original_commit = db.session.commit

    def hold_reconciliation_commit():
        if threading.current_thread().name == "job-reconciliation":
            db.session.flush()
            reconciliation_flushed.set()
            assert release_reconciliation.wait(_WAIT_SECONDS)
        return original_commit()

    monkeypatch.setattr(db.session, "commit", hold_reconciliation_commit)
    monkeypatch.setattr(background_jobs, "_app", app)
    monkeypatch.setattr(background_jobs, "_BOOT_ID", replacement_boot_id)

    def reconcile():
        outcomes["reconciled"] = background_jobs.reconcile_interrupted_jobs(
            app,
            now=now,
            lease_timeout_seconds=30,
        )

    def complete_late():
        completion_started.set()
        outcomes["persisted"] = background_jobs._persist_job(
            job_id,
            status="completed",
            finished_at=now,
            result={"ok": True, "source": "expired worker"},
            owner_heartbeat_at=now,
            require_active_owner=True,
        )

    reconciliation_thread, reconciliation_errors = _start_thread(
        name="job-reconciliation",
        target=reconcile,
        done=reconciliation_done,
    )
    assert reconciliation_flushed.wait(_WAIT_SECONDS)
    monkeypatch.setattr(background_jobs, "_BOOT_ID", old_boot_id)
    completion_thread, completion_errors = _start_thread(
        name="job-late-completion",
        target=complete_late,
        done=completion_done,
    )
    try:
        _assert_peer_waits(completion_started, completion_done)
    finally:
        release_reconciliation.set()
    _join(reconciliation_thread, reconciliation_errors)
    _join(completion_thread, completion_errors)

    assert outcomes["reconciled"] == 1
    assert outcomes["persisted"] is False
    with app.app_context():
        row = db.session.get(BackgroundJob, job_id)
        assert row.status == "interrupted"
        assert row.finished_at == now
        assert row.result_json is None
        assert "cannot resume" in row.error_text
