"""Service-level parity checks for the normalized relay projection."""

import pytest
import sqlalchemy as sa

from services.proam_relay import ProAmRelay
from tests.conftest import make_college_competitor, make_pro_competitor, make_team, make_tournament


def test_manual_teams_dual_write_to_normalized_relay_tables(db_session):
    tournament = make_tournament(db_session)
    school = make_team(db_session, tournament)
    pro = make_pro_competitor(db_session, tournament, "Pro One", gender="M")
    college = make_college_competitor(
        db_session, tournament, school, "College One", gender="F"
    )
    pro.pro_am_lottery_opt_in = True
    college.pro_am_lottery_opt_in = True
    db_session.flush()

    relay = ProAmRelay(tournament)
    relay.set_teams_manually([{
        "pro_member_ids": [pro.id],
        "college_member_ids": [college.id],
    }])

    state = db_session.execute(sa.text(
        "SELECT id, status FROM relay_states")).one()
    assert state.status == "drawn"
    assert db_session.execute(sa.text(
        "SELECT uid FROM relay_team_members WHERE relay_state_id = :state_id "
        "ORDER BY uid"), {"state_id": state.id}).scalars().all() == sorted([
            pro.uid, college.uid,
        ])
    assert db_session.execute(sa.text(
        "SELECT count(*) FROM relay_team_events")).scalar() == 4

    relay.record_event_result(1, "partnered_sawing", 20.5)
    assert db_session.execute(sa.text(
        "SELECT result, status FROM relay_team_events "
        "WHERE event_key = 'partnered_sawing'"
    )).one() == (20.5, "completed")


def test_replacement_rejects_competitor_already_on_another_relay_team(db_session):
    tournament = make_tournament(db_session)
    school = make_team(db_session, tournament)
    pros = [
        make_pro_competitor(db_session, tournament, f"Pro {number}", gender="M")
        for number in (1, 2)
    ]
    colleges = [
        make_college_competitor(db_session, tournament, school, f"College {number}", gender="F")
        for number in (1, 2)
    ]
    for competitor in pros + colleges:
        competitor.pro_am_lottery_opt_in = True
    db_session.flush()

    relay = ProAmRelay(tournament)
    relay.set_teams_manually([
        {"pro_member_ids": [pros[0].id], "college_member_ids": [colleges[0].id]},
        {"pro_member_ids": [pros[1].id], "college_member_ids": [colleges[1].id]},
    ])

    with pytest.raises(ValueError, match="already assigned"):
        relay.replace_competitor(1, pros[0].id, pros[1].id, "pro")
