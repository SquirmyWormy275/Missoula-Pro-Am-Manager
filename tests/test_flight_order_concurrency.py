"""Database and route guards for concurrent flight-order writers."""
import threading
from pathlib import Path
from unittest.mock import patch

import pytest
from sqlalchemy import event as sqlalchemy_event
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Query

from database import db
from tests.db_test_utils import create_test_app, drop_test_db


@pytest.fixture(scope='module')
def app():
    test_app, handle = create_test_app()
    with test_app.app_context():
        from models.user import User

        admin = User(username='flight_order_admin', role='admin')
        admin.set_password('flight_order_password')
        db.session.add(admin)
        db.session.commit()
        test_app.config['_FLIGHT_ORDER_ADMIN_ID'] = admin.id
    yield test_app
    with test_app.app_context():
        db.session.remove()
        db.engine.dispose()
    drop_test_db(handle)


@pytest.fixture()
def auth_client(app):
    client = app.test_client()
    with client.session_transaction() as session:
        session['_user_id'] = str(app.config['_FLIGHT_ORDER_ADMIN_ID'])
        session['_fresh'] = True
    return client


def _event(tournament_id, name, event_type='pro'):
    from models import Event

    return Event(
        tournament_id=tournament_id,
        name=name,
        event_type=event_type,
        gender='M',
        scoring_type='time',
        scoring_order='lowest_wins',
        stand_type=None,
        max_stands=5,
        status='pending',
    )


def _seed_schedule(app, label, heat_counts=(2,)):
    from models import Flight, Heat, Tournament
    from routes.scheduling.flights import flight_order_digest

    with app.app_context():
        tournament = Tournament(name=label, year=2029, status='setup')
        db.session.add(tournament)
        db.session.flush()

        flights = []
        heat_ids_by_flight = []
        for flight_number, heat_count in enumerate(heat_counts, start=1):
            flight = Flight(
                tournament_id=tournament.id,
                flight_number=flight_number,
                status='pending',
            )
            db.session.add(flight)
            db.session.flush()
            flights.append(flight)
            heat_ids = []
            for position in range(1, heat_count + 1):
                event = _event(
                    tournament.id,
                    f'{label} Event {flight_number}-{position}',
                )
                db.session.add(event)
                db.session.flush()
                heat = Heat(
                    event_id=event.id,
                    heat_number=1,
                    run_number=1,
                    status='pending',
                    flight_id=flight.id,
                    flight_position=position,
                )
                heat.set_roster('pro', [])
                db.session.add(heat)
                db.session.flush()
                heat_ids.append(heat.id)
            heat_ids_by_flight.append(heat_ids)

        db.session.commit()
        return {
            'tournament_id': tournament.id,
            'flight_ids': [flight.id for flight in flights],
            'heat_ids_by_flight': heat_ids_by_flight,
            'order_digests': [flight_order_digest(flight.id) for flight in flights],
        }


def _lock_calls_for_flights(monkeypatch):
    from models import Flight

    calls = []
    original = Query.with_for_update

    def recording_with_for_update(query, *args, **kwargs):
        if any(
            description.get('entity') is Flight
            for description in query.column_descriptions
        ):
            calls.append(True)
        return original(query, *args, **kwargs)

    monkeypatch.setattr(Query, 'with_for_update', recording_with_for_update)
    return calls


def _authenticated_client(app):
    client = app.test_client()
    with client.session_transaction() as session:
        session['_user_id'] = str(app.config['_FLIGHT_ORDER_ADMIN_ID'])
        session['_fresh'] = True
    return client


def _assert_writer_holds_parent_lock_until_release(
    app,
    tournament_id,
    first_request,
    writer_entered,
    release_writer,
):
    """Prove a peer tournament writer waits while the first route is open."""
    with app.app_context():
        if db.engine.dialect.name != 'postgresql':
            pytest.skip('PostgreSQL parent-lock serialization test')

    peer_finished = threading.Event()
    responses = {}
    errors = []

    def first_writer():
        try:
            responses['first'] = first_request(_authenticated_client(app))
        except Exception as exc:  # pragma: no cover - asserted in parent thread
            errors.append(exc)
            writer_entered.set()
            release_writer.set()

    def peer_writer():
        try:
            responses['peer'] = _authenticated_client(app).post(
                f'/scoring/{tournament_id}/pro/payout-manager',
                data={'action': 'unknown'},
                follow_redirects=False,
            )
        except Exception as exc:  # pragma: no cover - asserted in parent thread
            errors.append(exc)
        finally:
            peer_finished.set()

    first_thread = threading.Thread(target=first_writer)
    peer_thread = threading.Thread(target=peer_writer)
    first_thread.start()
    assert writer_entered.wait(10)
    peer_thread.start()
    try:
        assert not peer_finished.wait(0.5), (
            'peer writer entered before the first writer reached commit'
        )
    finally:
        release_writer.set()
    first_thread.join(10)
    peer_thread.join(10)

    assert not first_thread.is_alive()
    assert not peer_thread.is_alive()
    assert errors == []
    assert responses['first'].status_code in (302, 303)
    assert responses['peer'].status_code in (302, 303)


def _lock_calls_for_tournaments(monkeypatch):
    from models import Tournament

    calls = []
    original = Query.with_for_update

    def recording_with_for_update(query, *args, **kwargs):
        if any(
            description.get('entity') is Tournament
            for description in query.column_descriptions
        ):
            calls.append(True)
        return original(query, *args, **kwargs)

    monkeypatch.setattr(Query, 'with_for_update', recording_with_for_update)
    return calls


def test_model_and_migrated_schema_declare_unique_flight_positions(app):
    from models import Flight, Heat

    model_uniques = {
        (constraint.name, tuple(column.name for column in constraint.columns))
        for constraint in Heat.__table__.constraints
        if constraint.__class__.__name__ == 'UniqueConstraint'
    }
    assert (
        'uq_heats_flight_position',
        ('flight_id', 'flight_position'),
    ) in model_uniques

    with app.app_context():
        migrated_uniques = {
            (constraint['name'], tuple(constraint['column_names']))
            for constraint in inspect(db.engine).get_unique_constraints('heats')
        }
    assert (
        'uq_heats_flight_position',
        ('flight_id', 'flight_position'),
    ) in migrated_uniques

    model_flight_uniques = {
        (constraint.name, tuple(column.name for column in constraint.columns))
        for constraint in Flight.__table__.constraints
        if constraint.__class__.__name__ == 'UniqueConstraint'
    }
    assert (
        'uq_flights_tournament_number',
        ('tournament_id', 'flight_number'),
    ) in model_flight_uniques
    with app.app_context():
        migrated_flight_uniques = {
            (constraint['name'], tuple(constraint['column_names']))
            for constraint in inspect(db.engine).get_unique_constraints('flights')
        }
    assert (
        'uq_flights_tournament_number',
        ('tournament_id', 'flight_number'),
    ) in migrated_flight_uniques


def test_unique_constraint_rejects_duplicate_non_null_position(app):
    from models import Heat

    seeded = _seed_schedule(app, 'Duplicate Position Constraint', (1,))
    with app.app_context():
        first = db.session.get(Heat, seeded['heat_ids_by_flight'][0][0])
        other_event = _event(
            seeded['tournament_id'], 'Duplicate Position Insert Event',
        )
        db.session.add(other_event)
        db.session.flush()
        duplicate = Heat(
            event_id=other_event.id,
            heat_number=1,
            run_number=1,
            status='pending',
            flight_id=seeded['flight_ids'][0],
            flight_position=first.flight_position,
        )
        duplicate.set_roster('pro', [])
        db.session.add(duplicate)
        with pytest.raises(IntegrityError):
            db.session.flush()
        db.session.rollback()


def test_unique_constraint_allows_multiple_unassigned_positions(app):
    from models import Heat

    seeded = _seed_schedule(app, 'Nullable Position Constraint', (1,))
    with app.app_context():
        for suffix in ('A', 'B'):
            event = _event(
                seeded['tournament_id'], f'Nullable Position Event {suffix}',
            )
            db.session.add(event)
            db.session.flush()
            heat = Heat(
                event_id=event.id,
                heat_number=1,
                run_number=1,
                status='pending',
                flight_id=seeded['flight_ids'][0],
                flight_position=None,
            )
            heat.set_roster('pro', [])
            db.session.add(heat)
        db.session.commit()


def test_unique_constraint_rejects_duplicate_flight_number(app):
    from models import Flight

    seeded = _seed_schedule(app, 'Duplicate Flight Number Constraint', (1,))
    with app.app_context():
        db.session.add(Flight(
            tournament_id=seeded['tournament_id'],
            flight_number=1,
            status='pending',
        ))
        with pytest.raises(IntegrityError):
            db.session.flush()
        db.session.rollback()


def test_single_reorder_locks_parent_and_swaps_under_unique_constraint(
    app, auth_client, monkeypatch,
):
    from models import Heat

    seeded = _seed_schedule(app, 'Single Reorder Lock', (2,))
    calls = _lock_calls_for_flights(monkeypatch)
    tournament_calls = _lock_calls_for_tournaments(monkeypatch)
    expected = list(reversed(seeded['heat_ids_by_flight'][0]))

    from routes.scheduling.flights import flight_order_digest

    with app.app_context():
        expected_digest = flight_order_digest(seeded['flight_ids'][0])
    response = auth_client.post(
        f"/scheduling/{seeded['tournament_id']}/flights/"
        f"{seeded['flight_ids'][0]}/reorder",
        json={'heat_ids': expected, 'expected_digest': expected_digest},
    )

    assert response.status_code == 200
    assert calls
    assert tournament_calls
    with app.app_context():
        actual = [
            heat.id for heat in Heat.query.filter_by(
                flight_id=seeded['flight_ids'][0],
            ).order_by(Heat.flight_position).all()
        ]
    assert actual == expected


def test_stale_single_flight_reorder_is_rejected(app, auth_client):
    from models import Heat
    from routes.scheduling.flights import flight_order_digest

    seeded = _seed_schedule(app, 'Stale Single Reorder', (3,))
    original = seeded['heat_ids_by_flight'][0]
    with app.app_context():
        expected_digest = flight_order_digest(seeded['flight_ids'][0])

    first_order = [original[1], original[0], original[2]]
    first = auth_client.post(
        f"/scheduling/{seeded['tournament_id']}/flights/"
        f"{seeded['flight_ids'][0]}/reorder",
        json={'heat_ids': first_order, 'expected_digest': expected_digest},
    )
    stale = auth_client.post(
        f"/scheduling/{seeded['tournament_id']}/flights/"
        f"{seeded['flight_ids'][0]}/reorder",
        json={
            'heat_ids': [original[0], original[2], original[1]],
            'expected_digest': expected_digest,
        },
    )

    assert first.status_code == 200
    assert stale.status_code == 409
    assert stale.get_json()['code'] == 'stale_flight_order'
    with app.app_context():
        assert [
            heat.id for heat in Heat.query.filter_by(
                flight_id=seeded['flight_ids'][0]
            ).order_by(Heat.flight_position).all()
        ] == first_order


def test_single_reorder_returns_fresh_digest_for_consecutive_reorder(
    app, auth_client,
):
    from models import Heat

    seeded = _seed_schedule(app, 'Consecutive Single Reorders', (3,))
    flight_id = seeded['flight_ids'][0]
    original = seeded['heat_ids_by_flight'][0]
    endpoint = (
        f"/scheduling/{seeded['tournament_id']}/flights/{flight_id}/reorder"
    )

    first_order = [original[1], original[0], original[2]]
    first = auth_client.post(
        endpoint,
        json={
            'heat_ids': first_order,
            'expected_digest': seeded['order_digests'][0],
        },
    )

    assert first.status_code == 200
    first_payload = first.get_json()
    first_digest = first_payload['order_digests'][str(flight_id)]
    assert first_digest != seeded['order_digests'][0]

    second_order = [original[1], original[2], original[0]]
    second = auth_client.post(
        endpoint,
        json={'heat_ids': second_order, 'expected_digest': first_digest},
    )

    assert second.status_code == 200
    second_digest = second.get_json()['order_digests'][str(flight_id)]
    assert second_digest != first_digest
    with app.app_context():
        assert [
            heat.id for heat in Heat.query.filter_by(
                flight_id=flight_id,
            ).order_by(Heat.flight_position).all()
        ] == second_order


def test_bulk_reorder_returns_fresh_digests_for_consecutive_page_drags(
    app, auth_client,
):
    seeded = _seed_schedule(app, 'Consecutive Bulk Reorders', (2, 2))
    flight_ids = seeded['flight_ids']
    original = seeded['heat_ids_by_flight']
    endpoint = f"/scheduling/{seeded['tournament_id']}/flights/bulk-reorder"

    first_orders = [list(reversed(ids)) for ids in original]
    first = auth_client.post(
        endpoint,
        json={'flights': [
            {
                'flight_id': flight_id,
                'heat_ids': heat_ids,
                'expected_digest': seeded['order_digests'][index],
            }
            for index, (flight_id, heat_ids) in enumerate(
                zip(flight_ids, first_orders, strict=True)
            )
        ]},
    )

    assert first.status_code == 200
    first_digests = first.get_json()['order_digests']
    assert set(first_digests) == {str(flight_id) for flight_id in flight_ids}
    assert all(
        first_digests[str(flight_id)] != seeded['order_digests'][index]
        for index, flight_id in enumerate(flight_ids)
    )

    second = auth_client.post(
        endpoint,
        json={'flights': [
            {
                'flight_id': flight_id,
                'heat_ids': original[index],
                'expected_digest': first_digests[str(flight_id)],
            }
            for index, flight_id in enumerate(flight_ids)
        ]},
    )

    assert second.status_code == 200
    second_digests = second.get_json()['order_digests']
    assert set(second_digests) == set(first_digests)
    assert all(
        second_digests[flight_id] != first_digests[flight_id]
        for flight_id in first_digests
    )


def test_flights_page_applies_returned_order_digests_after_success():
    template = Path('templates/pro/flights.html').read_text(encoding='utf-8')

    assert 'function applyOrderDigests(orderDigests)' in template
    assert 'grid.dataset.orderDigest = orderDigests[flightId];' in template
    assert 'applyOrderDigests(data.order_digests);' in template


def test_flight_lifecycle_cannot_skip_or_regress(app, auth_client):
    from models import Flight

    seeded = _seed_schedule(app, 'Monotonic Flight Lifecycle', (1,))
    base = (
        f"/scheduling/{seeded['tournament_id']}/flights/"
        f"{seeded['flight_ids'][0]}"
    )

    out_of_order = auth_client.post(f'{base}/complete')
    assert out_of_order.status_code == 302
    with app.app_context():
        assert db.session.get(Flight, seeded['flight_ids'][0]).status == 'pending'

    assert auth_client.post(f'{base}/start').status_code == 302
    assert auth_client.post(f'{base}/complete').status_code == 302
    stale_start = auth_client.post(f'{base}/start')
    assert stale_start.status_code == 302
    with app.app_context():
        assert db.session.get(Flight, seeded['flight_ids'][0]).status == 'completed'


def test_bulk_reorder_locks_parent_rows_before_cross_flight_write(
    app, auth_client, monkeypatch,
):
    from models import Heat

    seeded = _seed_schedule(app, 'Bulk Reorder Lock', (1, 1))
    calls = _lock_calls_for_flights(monkeypatch)
    tournament_calls = _lock_calls_for_tournaments(monkeypatch)
    first_heat = seeded['heat_ids_by_flight'][0][0]
    second_heat = seeded['heat_ids_by_flight'][1][0]

    response = auth_client.post(
        f"/scheduling/{seeded['tournament_id']}/flights/bulk-reorder",
        json={'flights': [
            {
                'flight_id': seeded['flight_ids'][0],
                'heat_ids': [second_heat],
                'expected_digest': seeded['order_digests'][0],
            },
            {
                'flight_id': seeded['flight_ids'][1],
                'heat_ids': [first_heat],
                'expected_digest': seeded['order_digests'][1],
            },
        ]},
    )

    assert response.status_code == 200
    assert calls
    assert tournament_calls
    with app.app_context():
        db.session.expire_all()
        assert db.session.get(Heat, second_heat).flight_id == seeded['flight_ids'][0]
        assert db.session.get(Heat, first_heat).flight_id == seeded['flight_ids'][1]


@pytest.mark.parametrize('bulk', [False, True])
def test_uniqueness_race_returns_retryable_409(
    app, auth_client, monkeypatch, bulk,
):
    seeded = _seed_schedule(app, f'Integrity Race {bulk}', (2,))
    heat_ids = list(reversed(seeded['heat_ids_by_flight'][0]))
    conflict = IntegrityError(
        'flight position collision', {}, Exception('uq_heats_flight_position'),
    )
    monkeypatch.setattr(db.session, 'commit', lambda: (_ for _ in ()).throw(conflict))

    if bulk:
        response = auth_client.post(
            f"/scheduling/{seeded['tournament_id']}/flights/bulk-reorder",
            json={'flights': [{
                'flight_id': seeded['flight_ids'][0],
                'heat_ids': heat_ids,
                'expected_digest': seeded['order_digests'][0],
            }]},
        )
    else:
        response = auth_client.post(
            f"/scheduling/{seeded['tournament_id']}/flights/"
            f"{seeded['flight_ids'][0]}/reorder",
            json={
                'heat_ids': heat_ids,
                'expected_digest': seeded['order_digests'][0],
            },
        )

    assert response.status_code == 409
    assert response.get_json() == {
        'ok': False,
        'code': 'flight_order_conflict',
        'error': 'Flight order changed concurrently. Refresh and try again.',
    }


def test_migration_resequences_existing_assigned_heats_deterministically():
    from flask_migrate import downgrade, upgrade

    migration_app, handle = create_test_app(use_migrations=True)
    try:
        with migration_app.app_context():
            downgrade(revision='y4c5d6e7f8a9')
            db.session.execute(text(
                "INSERT INTO tournaments (name, year, status, created_at, updated_at) "
                "VALUES ('Migration Resequence', 2030, 'setup', CURRENT_TIMESTAMP, "
                "CURRENT_TIMESTAMP)"
            ))
            tournament_id = db.session.execute(text(
                "SELECT id FROM tournaments WHERE name = 'Migration Resequence'"
            )).scalar_one()
            db.session.execute(text(
                "INSERT INTO events "
                "(tournament_id, name, event_type, gender, scoring_type, "
                "scoring_order, is_open, is_handicap, is_partnered, "
                "requires_dual_runs, requires_triple_runs, has_prelims, payouts, "
                "status, is_finalized) "
                "VALUES (:t, 'Migration Event', 'pro', 'M', 'time', "
                "'lowest_wins', false, false, false, false, false, false, '{}', "
                "'pending', false)"
            ), {'t': tournament_id})
            event_id = db.session.execute(text(
                "SELECT id FROM events WHERE tournament_id = :t"
            ), {'t': tournament_id}).scalar_one()
            db.session.execute(text(
                "INSERT INTO flights (tournament_id, flight_number, status) "
                "VALUES (:t, 1, 'pending')"
            ), {'t': tournament_id})
            flight_id = db.session.execute(text(
                "SELECT id FROM flights WHERE tournament_id = :t"
            ), {'t': tournament_id}).scalar_one()
            for heat_number, position in ((1, 2), (2, 2), (3, None), (4, 1)):
                db.session.execute(text(
                    "INSERT INTO heats "
                    "(event_id, heat_number, run_number, status, version_id, "
                    "flight_id, flight_position) "
                    "VALUES (:e, :n, 1, 'pending', 1, :f, :p)"
                ), {
                    'e': event_id,
                    'n': heat_number,
                    'f': flight_id,
                    'p': position,
                })
            db.session.execute(text(
                "INSERT INTO flights (tournament_id, flight_number, status) "
                "VALUES (:t, 2, 'completed')"
            ), {'t': tournament_id})
            historical_flight_id = db.session.execute(text(
                "SELECT id FROM flights WHERE tournament_id = :t "
                "AND flight_number = 2"
            ), {'t': tournament_id}).scalar_one()
            db.session.execute(text(
                "INSERT INTO heats "
                "(event_id, heat_number, run_number, status, version_id, "
                "flight_id, flight_position) "
                "VALUES (:e, 5, 1, 'completed', 1, :f, 7)"
            ), {'e': event_id, 'f': historical_flight_id})
            db.session.commit()
            db.session.remove()

            statements = []

            def capture_statement(
                _conn, _cursor, statement, _parameters, _context, _executemany,
            ):
                statements.append(' '.join(statement.lower().split()))

            sqlalchemy_event.listen(
                db.engine,
                'before_cursor_execute',
                capture_statement,
            )
            try:
                upgrade()
            finally:
                sqlalchemy_event.remove(
                    db.engine,
                    'before_cursor_execute',
                    capture_statement,
                )

            if db.engine.dialect.name == 'postgresql':
                lock_index = next(
                    index for index, statement in enumerate(statements)
                    if statement.startswith(
                        'lock table flights, heats in share row exclusive mode'
                    )
                )
                scan_index = next(
                    index for index, statement in enumerate(statements)
                    if 'group by flight_id, flight_position' in statement
                )
                assert lock_index < scan_index

            repaired = db.session.execute(text(
                "SELECT heat_number, flight_position FROM heats "
                "WHERE flight_id = :f ORDER BY flight_position"
            ), {'f': flight_id}).all()
            assert repaired == [(4, 1), (1, 2), (2, 3), (3, 4)]
            historical_position = db.session.execute(text(
                "SELECT flight_position FROM heats WHERE flight_id = :f"
            ), {'f': historical_flight_id}).scalar_one()
            assert historical_position == 7
            uniques = {
                (constraint['name'], tuple(constraint['column_names']))
                for constraint in inspect(db.engine).get_unique_constraints('heats')
            }
            assert (
                'uq_heats_flight_position',
                ('flight_id', 'flight_position'),
            ) in uniques
    finally:
        with migration_app.app_context():
            db.session.remove()
            db.engine.dispose()
        drop_test_db(handle)


def test_migration_refuses_to_resequence_historical_duplicate_positions(capfd):
    from flask_migrate import downgrade, upgrade

    migration_app, handle = create_test_app(use_migrations=True)
    try:
        with migration_app.app_context():
            downgrade(revision='y4c5d6e7f8a9')
            db.session.execute(text(
                "INSERT INTO tournaments (name, year, status, created_at, updated_at) "
                "VALUES ('Historical Migration Guard', 2031, 'setup', "
                "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            ))
            tournament_id = db.session.execute(text(
                "SELECT id FROM tournaments WHERE name = 'Historical Migration Guard'"
            )).scalar_one()
            db.session.execute(text(
                "INSERT INTO events "
                "(tournament_id, name, event_type, gender, scoring_type, "
                "scoring_order, is_open, is_handicap, is_partnered, "
                "requires_dual_runs, requires_triple_runs, has_prelims, payouts, "
                "status, is_finalized) "
                "VALUES (:t, 'Historical Event', 'pro', 'M', 'time', "
                "'lowest_wins', false, false, false, false, false, false, '{}', "
                "'pending', false)"
            ), {'t': tournament_id})
            event_id = db.session.execute(text(
                "SELECT id FROM events WHERE tournament_id = :t"
            ), {'t': tournament_id}).scalar_one()
            db.session.execute(text(
                "INSERT INTO flights (tournament_id, flight_number, status) "
                "VALUES (:t, 1, 'completed')"
            ), {'t': tournament_id})
            flight_id = db.session.execute(text(
                "SELECT id FROM flights WHERE tournament_id = :t"
            ), {'t': tournament_id}).scalar_one()
            for heat_number, status in ((1, 'completed'), (2, 'pending')):
                db.session.execute(text(
                    "INSERT INTO heats "
                    "(event_id, heat_number, run_number, status, version_id, "
                    "flight_id, flight_position) "
                    "VALUES (:e, :n, 1, :s, 1, :f, 1)"
                ), {
                    'e': event_id,
                    'n': heat_number,
                    's': status,
                    'f': flight_id,
                })
            db.session.commit()
            db.session.remove()

            with pytest.raises(SystemExit):
                upgrade()
            captured = capfd.readouterr()
            assert 'historical or active placements' in captured.err
            db.session.rollback()
            db.session.remove()

            rows = db.session.execute(text(
                "SELECT heat_number, status, flight_position FROM heats "
                "WHERE flight_id = :flight_id ORDER BY heat_number"
            ), {'flight_id': flight_id}).all()
            assert rows == [
                (1, 'completed', 1),
                (2, 'pending', 1),
            ]
            assert db.session.execute(text(
                "SELECT version_num FROM alembic_version"
            )).scalar_one() == 'y4c5d6e7f8a9'
            uniques = {
                constraint['name']
                for constraint in inspect(db.engine).get_unique_constraints('heats')
            }
            assert 'uq_heats_flight_position' not in uniques
    finally:
        with migration_app.app_context():
            db.session.remove()
            db.engine.dispose()
        drop_test_db(handle)


def test_migration_refuses_duplicate_flight_numbers_without_partial_upgrade(capfd):
    from flask_migrate import downgrade, upgrade

    migration_app, handle = create_test_app(use_migrations=True)
    try:
        with migration_app.app_context():
            downgrade(revision='y4c5d6e7f8a9')
            db.session.execute(text(
                "INSERT INTO tournaments (name, year, status, created_at, updated_at) "
                "VALUES ('Duplicate Flight Migration Guard', 2032, 'setup', "
                "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            ))
            tournament_id = db.session.execute(text(
                "SELECT id FROM tournaments "
                "WHERE name = 'Duplicate Flight Migration Guard'"
            )).scalar_one()
            for _ in range(2):
                db.session.execute(text(
                    "INSERT INTO flights (tournament_id, flight_number, status) "
                    "VALUES (:tournament_id, 1, 'pending')"
                ), {'tournament_id': tournament_id})
            db.session.commit()
            db.session.remove()

            with pytest.raises(SystemExit):
                upgrade()
            captured = capfd.readouterr()
            assert 'resolve duplicate operator-defined numbers' in captured.err
            db.session.rollback()
            db.session.remove()

            assert db.session.execute(text(
                "SELECT COUNT(*) FROM flights "
                "WHERE tournament_id = :tournament_id AND flight_number = 1"
            ), {'tournament_id': tournament_id}).scalar_one() == 2
            assert db.session.execute(text(
                "SELECT version_num FROM alembic_version"
            )).scalar_one() == 'y4c5d6e7f8a9'
            uniques = {
                constraint['name']
                for constraint in inspect(db.engine).get_unique_constraints('flights')
            }
            assert 'uq_flights_tournament_number' not in uniques
    finally:
        with migration_app.app_context():
            db.session.remove()
            db.engine.dispose()
        drop_test_db(handle)


def test_build_pro_flights_acquires_tournament_parent_lock(app, monkeypatch):
    from models import Tournament
    from services.flight_builder import build_pro_flights

    seeded = _seed_schedule(app, 'Build Parent Lock', (1,))
    with app.app_context():
        calls = _lock_calls_for_tournaments(monkeypatch)
        tournament = db.session.get(Tournament, seeded['tournament_id'])
        build_pro_flights(tournament, commit=False)
        assert calls
        db.session.rollback()


def test_scoring_acquires_same_tournament_parent_lock(app, monkeypatch):
    from models import Heat
    from services import scoring_workflow

    seeded = _seed_schedule(app, 'Scoring Parent Lock', (1,))
    calls = []
    monkeypatch.setattr(
        scoring_workflow,
        'lock_tournament_schedule',
        lambda tournament_id: calls.append(int(tournament_id)),
    )

    with app.app_context():
        heat = db.session.get(Heat, seeded['heat_ids_by_flight'][0][0])
        result = scoring_workflow.save_heat_results_submission(
            tournament_id=seeded['tournament_id'],
            heat=heat,
            event=heat.event,
            form_data={'heat_version': str(heat.version_id)},
            judge_user_id=app.config['_FLIGHT_ORDER_ADMIN_ID'],
        )

    assert calls == [seeded['tournament_id']]
    assert result['ok'] is False


def test_scoring_rejects_heat_replaced_before_parent_lock(app):
    from models import Heat
    from services.scoring_workflow import save_heat_results_submission

    seeded = _seed_schedule(app, 'Scoring Replaced Heat', (1,))
    with app.app_context():
        heat = db.session.get(Heat, seeded['heat_ids_by_flight'][0][0])
        event = heat.event
        heat_id = heat.id
        version = heat.version_id
        db.session.expunge(heat)
        Heat.query.filter_by(id=heat_id).delete(synchronize_session=False)
        db.session.commit()

        result = save_heat_results_submission(
            tournament_id=seeded['tournament_id'],
            heat=heat,
            event=event,
            form_data={'heat_version': str(version)},
            judge_user_id=app.config['_FLIGHT_ORDER_ADMIN_ID'],
        )

    assert result['ok'] is False
    assert result['status_code'] == 409
    assert 'replaced while the schedule was being rebuilt' in result['message']


def test_scoring_rejects_stale_form_when_replacement_reuses_heat_id(app):
    from models import Heat
    from services.scoring_workflow import save_heat_results_submission
    from services.time_utils import utc_now_naive

    seeded = _seed_schedule(app, 'Scoring Reused Heat ID', (1,))
    with app.app_context():
        heat_id = seeded['heat_ids_by_flight'][0][0]
        old_heat = db.session.get(Heat, heat_id)
        old_heat.locked_by_user_id = app.config['_FLIGHT_ORDER_ADMIN_ID']
        old_heat.locked_at = utc_now_naive()
        db.session.commit()
        stale_identity = old_heat.locked_at.isoformat(timespec='microseconds')
        event_id = old_heat.event_id
        db.session.expunge(old_heat)

        Heat.query.filter_by(id=heat_id).delete(synchronize_session=False)
        db.session.flush()
        replacement = Heat(
            id=heat_id,
            event_id=event_id,
            heat_number=1,
            run_number=1,
            status='pending',
            version_id=1,
        )
        replacement.set_roster('pro', [])
        db.session.add(replacement)
        db.session.commit()

        result = save_heat_results_submission(
            tournament_id=seeded['tournament_id'],
            heat=replacement,
            event=replacement.event,
            form_data={
                'heat_version': '1',
                'heat_identity': stale_identity,
            },
            judge_user_id=app.config['_FLIGHT_ORDER_ADMIN_ID'],
        )

    assert result['ok'] is False
    assert result['status_code'] == 409
    assert 'older heat instance' in result['message']


@pytest.mark.parametrize(
    ('endpoint', 'payload'),
    (
        ('reorder-friday', {'event_ids': []}),
        ('reorder-saturday', {'event_ids': []}),
        ('reset-order', {'day': 'friday'}),
    ),
)
def test_event_order_config_writes_acquire_tournament_parent_lock(
    app, auth_client, monkeypatch, endpoint, payload,
):
    seeded = _seed_schedule(app, f'Config Lock {endpoint}', (1,))
    calls = _lock_calls_for_tournaments(monkeypatch)

    response = auth_client.post(
        f"/scheduling/{seeded['tournament_id']}/events/{endpoint}",
        json=payload,
    )

    assert response.status_code == 200
    assert calls


def test_sqlite_reorder_waits_for_shared_schedule_guard(app):
    with app.app_context():
        if db.engine.dialect.name != 'sqlite':
            pytest.skip('SQLite process-lock regression')

    from services.flight_builder import sqlite_schedule_writer_guard

    seeded = _seed_schedule(app, 'SQLite Shared Writer Guard', (2,))
    guard_held = threading.Event()
    release_guard = threading.Event()
    reorder_finished = threading.Event()
    response_box = {}

    def guard_holder():
        with app.app_context():
            with sqlite_schedule_writer_guard(seeded['tournament_id']):
                guard_held.set()
                release_guard.wait(10)

    def reorder_writer():
        client = app.test_client()
        with client.session_transaction() as session:
            session['_user_id'] = str(app.config['_FLIGHT_ORDER_ADMIN_ID'])
            session['_fresh'] = True
        response_box['response'] = client.post(
            f"/scheduling/{seeded['tournament_id']}/flights/"
            f"{seeded['flight_ids'][0]}/reorder",
            json={
                'heat_ids': list(reversed(seeded['heat_ids_by_flight'][0])),
                'expected_digest': seeded['order_digests'][0],
            },
        )
        reorder_finished.set()

    holder_thread = threading.Thread(target=guard_holder)
    reorder_thread = threading.Thread(target=reorder_writer)
    holder_thread.start()
    assert guard_held.wait(10)
    reorder_thread.start()
    try:
        assert not reorder_finished.wait(0.5)
    finally:
        release_guard.set()
    holder_thread.join(10)
    reorder_thread.join(10)

    assert not holder_thread.is_alive()
    assert not reorder_thread.is_alive()
    assert response_box['response'].status_code == 200


def test_postgres_integration_and_reorder_serialize_on_flight_parent_rows(
    app, auth_client,
):
    with app.app_context():
        dialect_name = db.engine.dialect.name
    if dialect_name != 'postgresql':
        pytest.skip('PostgreSQL row-lock integration test')

    from models import Heat

    seeded = _seed_schedule(app, 'Postgres Writer Serialization', (1, 2))
    with app.app_context():
        spillover_event = _event(
            seeded['tournament_id'],
            'Postgres Concurrent Spillover',
            event_type='college',
        )
        db.session.add(spillover_event)
        db.session.flush()
        spillover_heat = Heat(
            event_id=spillover_event.id,
            heat_number=1,
            run_number=1,
            status='pending',
        )
        spillover_heat.set_roster('college', [])
        db.session.add(spillover_heat)
        db.session.commit()
        spillover_event_id = spillover_event.id
        spillover_heat_id = spillover_heat.id

    integration_holds_lock = threading.Event()
    release_integration = threading.Event()
    reorder_finished = threading.Event()
    errors = []
    response_box = {}

    def integration_writer():
        from models import Tournament
        from services.flight_builder import integrate_college_spillover_into_flights

        try:
            with app.app_context():
                tournament = db.session.get(Tournament, seeded['tournament_id'])
                integrate_college_spillover_into_flights(
                    tournament,
                    [spillover_event_id],
                    commit=False,
                )
                integration_holds_lock.set()
                if not release_integration.wait(10):
                    raise TimeoutError('test did not release integration transaction')
                db.session.commit()
        except Exception as exc:  # pragma: no cover - asserted in parent thread
            errors.append(exc)
            integration_holds_lock.set()
            release_integration.set()

    def reorder_writer():
        try:
            client = app.test_client()
            with client.session_transaction() as session:
                session['_user_id'] = str(app.config['_FLIGHT_ORDER_ADMIN_ID'])
                session['_fresh'] = True
            response_box['response'] = client.post(
                f"/scheduling/{seeded['tournament_id']}/flights/"
                f"{seeded['flight_ids'][1]}/reorder",
                json={
                    'heat_ids': list(reversed(seeded['heat_ids_by_flight'][1])),
                    'expected_digest': seeded['order_digests'][1],
                },
            )
        except Exception as exc:  # pragma: no cover - asserted in parent thread
            errors.append(exc)
        finally:
            reorder_finished.set()

    integration_thread = threading.Thread(target=integration_writer)
    reorder_thread = threading.Thread(target=reorder_writer)
    integration_thread.start()
    assert integration_holds_lock.wait(10)
    reorder_thread.start()
    try:
        assert not reorder_finished.wait(0.5), (
            'manual reorder did not wait for the integration transaction lock'
        )
    finally:
        release_integration.set()
    integration_thread.join(10)
    reorder_thread.join(10)

    assert not integration_thread.is_alive()
    assert not reorder_thread.is_alive()
    assert errors == []
    assert response_box['response'].status_code == 200
    with app.app_context():
        rows = db.session.execute(text(
            "SELECT flight_id, flight_position FROM heats "
            "WHERE flight_id IN (:first_flight, :second_flight) "
            "AND flight_position IS NOT NULL "
            "ORDER BY flight_id, flight_position"
        ), {
            'first_flight': seeded['flight_ids'][0],
            'second_flight': seeded['flight_ids'][1],
        }).all()
        positions_by_flight = {}
        for flight_id, position in rows:
            positions_by_flight.setdefault(flight_id, []).append(position)
        assert all(
            len(positions) == len(set(positions))
            for positions in positions_by_flight.values()
        )
        assert db.session.get(Heat, spillover_heat_id).flight_id == seeded['flight_ids'][0]


def test_postgres_payout_writer_holds_tournament_lock_through_commit(app):
    from models import Event, Heat

    seeded = _seed_schedule(app, 'Payout Writer Serialization', (1,))
    with app.app_context():
        heat = db.session.get(Heat, seeded['heat_ids_by_flight'][0][0])
        event_id = heat.event_id

    entered = threading.Event()
    release = threading.Event()
    original_set_payouts = Event.set_payouts

    def holding_set_payouts(event, payouts):
        entered.set()
        if not release.wait(10):
            raise TimeoutError('test did not release payout writer')
        return original_set_payouts(event, payouts)

    with patch.object(Event, 'set_payouts', holding_set_payouts):
        _assert_writer_holds_parent_lock_until_release(
            app,
            seeded['tournament_id'],
            lambda client: client.post(
                f'/scoring/{seeded["tournament_id"]}/event/{event_id}/payouts',
                data={'payout_1': '100'},
                follow_redirects=False,
            ),
            entered,
            release,
        )


def test_postgres_partnered_axe_writer_holds_tournament_lock_through_commit(app):
    import routes.partnered_axe as partnered_axe_routes
    from models import Tournament

    with app.app_context():
        tournament = Tournament(
            name='Partnered Axe Writer Serialization',
            year=2033,
            status='setup',
        )
        db.session.add(tournament)
        db.session.commit()
        tournament_id = tournament.id

    entered = threading.Event()
    release = threading.Event()
    original_get_or_create = partnered_axe_routes.get_or_create_partnered_axe_throw

    def holding_get_or_create(target_tournament_id):
        entered.set()
        if not release.wait(10):
            raise TimeoutError('test did not release Partnered Axe writer')
        return original_get_or_create(target_tournament_id)

    with patch.object(
        partnered_axe_routes,
        'get_or_create_partnered_axe_throw',
        holding_get_or_create,
    ):
        _assert_writer_holds_parent_lock_until_release(
            app,
            tournament_id,
            lambda client: client.post(
                f'/tournament/{tournament_id}/partnered-axe/enable',
                follow_redirects=False,
            ),
            entered,
            release,
        )


def test_postgres_birling_writer_holds_tournament_lock_through_commit(app):
    import routes.scheduling.birling as birling_routes
    from models import Event, Tournament
    from services.birling_bracket import BirlingBracket

    with app.app_context():
        tournament = Tournament(
            name='Birling Writer Serialization', year=2034, status='setup',
        )
        db.session.add(tournament)
        db.session.flush()
        event = Event(
            tournament_id=tournament.id,
            name='Birling',
            event_type='college',
            gender='M',
            scoring_type='bracket',
            scoring_order='lowest_wins',
            status='pending',
        )
        db.session.add(event)
        db.session.commit()
        tournament_id = tournament.id
        event_id = event.id
        bracket_digest = BirlingBracket(event).bracket_state_digest()

    entered = threading.Event()
    release = threading.Event()
    original_clear = birling_routes.birling_rows.clear_event

    def holding_clear(event_id_to_clear):
        entered.set()
        if not release.wait(10):
            raise TimeoutError('test did not release Birling writer')
        return original_clear(event_id_to_clear)

    with patch.object(birling_routes.birling_rows, 'clear_event', holding_clear):
        _assert_writer_holds_parent_lock_until_release(
            app,
            tournament_id,
            lambda client: client.post(
                f'/scheduling/{tournament_id}/event/{event_id}/birling/reset',
                data={'expected_bracket_digest': bracket_digest},
                follow_redirects=False,
            ),
            entered,
            release,
        )


def test_postgres_registration_writer_holds_tournament_lock_through_commit(app):
    from models import CollegeCompetitor, Team, Tournament

    with app.app_context():
        tournament = Tournament(
            name='Registration Writer Serialization', year=2035, status='setup',
        )
        db.session.add(tournament)
        db.session.flush()
        team = Team(
            tournament_id=tournament.id,
            team_code='SER-A',
            school_name='Serialization College',
            school_abbreviation='SC',
        )
        db.session.add(team)
        db.session.flush()
        competitor = CollegeCompetitor(
            tournament_id=tournament.id,
            team_id=team.id,
            name='Registration Writer',
            gender='M',
            status='active',
        )
        db.session.add(competitor)
        db.session.commit()
        tournament_id = tournament.id
        competitor_id = competitor.id

    entered = threading.Event()
    release = threading.Event()

    def holding_validation(_team_ids):
        entered.set()
        if not release.wait(10):
            raise TimeoutError('test did not release registration writer')
        return {}

    with patch(
        'services.excel_io._validate_college_entry_constraints',
        side_effect=holding_validation,
    ):
        _assert_writer_holds_parent_lock_until_release(
            app,
            tournament_id,
            lambda client: client.post(
                f'/registration/{tournament_id}/college/competitor/'
                f'{competitor_id}/set-partner',
                data={
                    'event_name': 'Double Buck',
                    'partner_name': 'Partner Name',
                },
                follow_redirects=False,
            ),
            entered,
            release,
        )
