"""Database-backed coverage for Birling result finalization.

The bracket document is authoritative for progress, while ``EventResult``
rows are authoritative for scoring. These tests exercise the boundary between
them against the isolated migration-backed test database.
"""
from __future__ import annotations

import logging
from decimal import Decimal

from database import db
from models import EventResult
from models.team import Team
from services.birling_bracket import BirlingBracket
from tests.conftest import (
    make_college_competitor,
    make_event,
    make_team,
    make_tournament,
)


def _completed_college_bracket(session):
    tournament = make_tournament(session, name='Birling Finalization')
    team = make_team(session, tournament, code='BF-A')
    competitors = [
        make_college_competitor(session, tournament, team, f'Chopper {number}')
        for number in range(1, 5)
    ]
    event = make_event(
        session,
        tournament,
        'College Birling',
        event_type='college',
        scoring_type='bracket',
    )
    bracket = BirlingBracket(event)
    bracket.generate_bracket([
        {'id': competitor.id, 'name': competitor.name}
        for competitor in competitors
    ])
    bracket.bracket_data['placements'] = {
        str(competitor.id): position
        for position, competitor in enumerate(competitors, start=1)
    }
    return event, team, competitors, bracket


class TestBirlingFinalization:
    def test_finalization_rebuilds_college_and_team_points(self, db_session):
        event, team, competitors, bracket = _completed_college_bracket(db_session)

        bracket.finalize_to_event_results()
        db.session.expire_all()

        results = (
            EventResult.query.filter_by(event_id=event.id)
            .order_by(EventResult.final_position)
            .all()
        )
        assert [result.final_position for result in results] == [1, 2, 3, 4]
        assert [result.points_awarded for result in results] == [
            Decimal('10'), Decimal('7'), Decimal('5'), Decimal('3')
        ]
        assert all(result.status == 'completed' for result in results)
        assert db.session.get(type(event), event.id).status == 'completed'

        expected = [Decimal('10'), Decimal('7'), Decimal('5'), Decimal('3')]
        actual = [
            db.session.get(type(competitor), competitor.id).individual_points
            for competitor in competitors
        ]
        assert actual == expected
        assert db.session.get(Team, team.id).total_points == Decimal('25')

    def test_finalization_is_idempotent_for_results_and_standings(self, db_session):
        event, team, competitors, bracket = _completed_college_bracket(db_session)

        bracket.finalize_to_event_results()
        bracket.finalize_to_event_results()
        db.session.expire_all()

        assert EventResult.query.filter_by(event_id=event.id).count() == 4
        assert db.session.get(Team, team.id).total_points == Decimal('25')
        assert [
            db.session.get(type(competitor), competitor.id).individual_points
            for competitor in competitors
        ] == [Decimal('10'), Decimal('7'), Decimal('5'), Decimal('3')]

    def test_standings_rebuild_failure_is_logged_without_losing_results(
        self, db_session, monkeypatch, caplog
    ):
        event, team, _competitors, bracket = _completed_college_bracket(db_session)

        def fail_recalculation(_self):
            raise RuntimeError('simulated team total failure')

        monkeypatch.setattr(Team, 'recalculate_points', fail_recalculation)
        logger = logging.getLogger('services.birling_bracket')
        was_disabled = logger.disabled
        logger.disabled = False
        try:
            with caplog.at_level(logging.WARNING, logger=logger.name):
                bracket.finalize_to_event_results()
        finally:
            logger.disabled = was_disabled

        db.session.expire_all()
        assert EventResult.query.filter_by(event_id=event.id).count() == 4
        assert db.session.get(type(event), event.id).status == 'completed'
        assert db.session.get(Team, team.id).total_points == Decimal('0')
        assert any(
            'college standings rebuild failed' in message
            for message in caplog.messages
        )


class TestBirlingFinalizationRoute:
    def test_incomplete_bracket_is_not_finalized(self, db_session, auth_client):
        tournament = make_tournament(db_session, name='Birling Route Guard')
        event = make_event(
            db_session,
            tournament,
            'College Birling',
            event_type='college',
            scoring_type='bracket',
        )

        response = auth_client.post(
            f'/scheduling/{tournament.id}/event/{event.id}/birling/finalize'
        )

        assert response.status_code == 302
        assert EventResult.query.filter_by(event_id=event.id).count() == 0
        assert event.status == 'pending'
        with auth_client.session_transaction() as session:
            assert ('error', 'No placements to finalize. Complete the bracket first.') in session['_flashes']

    def test_completed_bracket_finalizes_through_route(self, db_session, auth_client):
        event, team, _competitors, bracket = _completed_college_bracket(db_session)
        bracket._save_bracket_data()

        response = auth_client.post(
            f'/scheduling/{event.tournament_id}/event/{event.id}/birling/finalize'
        )

        assert response.status_code == 302
        db.session.expire_all()
        assert EventResult.query.filter_by(event_id=event.id).count() == 4
        assert db.session.get(Team, team.id).total_points == Decimal('25')
        with auth_client.session_transaction() as session:
            assert any(
                category == 'success' and 'Bracket finalized with 4 placements.' in message
                for category, message in session['_flashes']
            )
