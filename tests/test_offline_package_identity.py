"""A judge edit lock is not a schedule replacement."""
from routes.scoring import _offline_schedule_snapshot
from services.scoring_workflow import heat_submission_identity
from tests.test_offline_scoring_u2 import _make_user, _seed_score


def test_edit_lock_keeps_prepared_schedule_but_stand_change_invalidates_it(client, db_session):
    user = _make_user(db_session)
    tournament, event, heat, competitor, _request, _payload = _seed_score(db_session, user_id=user.id)
    db_session.commit()
    with client.session_transaction() as session:
        session['_user_id'] = str(user.id)
    before, _ = _offline_schedule_snapshot(tournament.id)
    identity = heat_submission_identity(heat)
    version = heat.version_id
    response = client.get(f'/scoring/{tournament.id}/heat/{heat.id}/enter')
    assert response.status_code == 200
    db_session.expire_all()
    assert heat.version_id > version  # the real lock path did execute
    assert heat_submission_identity(heat) == identity
    after, _ = _offline_schedule_snapshot(tournament.id)
    assert after == before

    stands = heat.get_stand_assignments()
    stands[str(competitor.id)] = 2
    heat.set_roster(event.event_type, heat.get_competitors(), stands)
    db_session.flush()
    changed, _ = _offline_schedule_snapshot(tournament.id)
    assert changed != before
    assert heat_submission_identity(heat) != identity
