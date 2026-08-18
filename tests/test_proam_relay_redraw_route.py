"""
Route-level tests for the Pro-Am Relay redraw endpoint.

Covers the num_teams form field added so judges can switch from an initial
2-team draw to 3 teams (or back) without manually clearing state. Uses a
local module-scoped app + login-based auth_client so committing routes
(run_lottery, redraw_lottery) do not collide with the shared admin_user
fixture in conftest.
"""

import os
import re

import pytest

from database import db as _db


@pytest.fixture(scope="module")
def app():
    from tests.db_test_utils import create_test_app

    _app, db_path = create_test_app()
    with _app.app_context():
        _seed_admin()
        yield _app
        _db.session.remove()
    try:
        os.unlink(db_path)
    except OSError:
        pass


def _seed_admin():
    from models.user import User

    if not User.query.filter_by(username="relay_admin").first():
        u = User(username="relay_admin", role="admin")
        u.set_password("relay_pass")
        _db.session.add(u)
        _db.session.commit()


@pytest.fixture(autouse=True)
def db_session(app):
    with app.app_context():
        _db.session.begin_nested()
        yield _db.session
        _db.session.rollback()


@pytest.fixture()
def auth_client(app):
    c = app.test_client()
    c.post(
        "/auth/login",
        data={"username": "relay_admin", "password": "relay_pass"},
        follow_redirects=True,
    )
    return c


def _seed_relay_tournament(session, pros_per_gender=6, cols_per_gender=6):
    """Seed a tournament with enough opted-in competitors for ``min`` teams.

    With 6M/6F pro + 6M/6F college the lottery capacity is 3. Callers can
    then run_lottery at 1/2/3 teams and exercise the redraw path.
    """
    from models import Tournament
    from models.competitor import CollegeCompetitor, ProCompetitor
    from models.team import Team

    t = Tournament(name="Relay Redraw Test", year=2026, status="pro_active")
    session.add(t)
    session.flush()

    team = Team(
        tournament_id=t.id, team_code="U-A", school_name="U", school_abbreviation="U"
    )
    session.add(team)
    session.flush()

    for i in range(pros_per_gender):
        session.add(
            ProCompetitor(
                tournament_id=t.id,
                name=f"PM{i}",
                gender="M",
                pro_am_lottery_opt_in=True,
                status="active",
            )
        )
        session.add(
            ProCompetitor(
                tournament_id=t.id,
                name=f"PF{i}",
                gender="F",
                pro_am_lottery_opt_in=True,
                status="active",
            )
        )

    for i in range(cols_per_gender):
        for g in ("M", "F"):
            c = CollegeCompetitor(
                tournament_id=t.id,
                team_id=team.id,
                name=f"C{g}{i}",
                gender=g,
                status="active",
            )
            c.pro_am_lottery_opt_in = True
            session.add(c)

    session.flush()
    return t


class TestRedrawNumTeams:
    """Redraw route must honor a judge-chosen num_teams, not the existing count."""

    def test_redraw_escalates_team_count(self, app, auth_client, db_session):
        """After a 2-team draw, POST num_teams=3 should redraw at 3."""
        from services.proam_relay import ProAmRelay

        t = _seed_relay_tournament(db_session)
        relay = ProAmRelay(t)
        relay.run_lottery(num_teams=2)
        assert len(relay.get_teams()) == 2

        r = auth_client.post(
            f"/tournament/{t.id}/proam-relay/redraw",
            data={
                "num_teams": "3",
                "expected_relay_digest": relay.state_digest(),
            },
        )
        assert r.status_code == 302

        assert len(ProAmRelay(t).get_teams()) == 3

    def test_redraw_without_num_teams_falls_back_to_existing(
        self, app, auth_client, db_session
    ):
        """The real redraw form may omit num_teams while retaining its token."""
        from services.proam_relay import ProAmRelay

        t = _seed_relay_tournament(db_session)
        relay = ProAmRelay(t)
        relay.run_lottery(num_teams=2)

        r = auth_client.post(
            f"/tournament/{t.id}/proam-relay/redraw",
            data={"expected_relay_digest": relay.state_digest()},
        )
        assert r.status_code == 302

        assert len(ProAmRelay(t).get_teams()) == 2

    @pytest.mark.parametrize("bad", ["five", "0", "-1", "3.5", ""])
    def test_redraw_rejects_invalid_num_teams_without_crash(
        self, app, auth_client, db_session, bad
    ):
        """Non-integer, zero, or negative input flashes error and preserves state.

        Empty string ("") is treated as missing and falls back to existing count
        (same as the no-num_teams case) — the assertion below covers both shapes.
        """
        from services.proam_relay import ProAmRelay

        t = _seed_relay_tournament(db_session)
        relay = ProAmRelay(t)
        relay.run_lottery(num_teams=2)

        r = auth_client.post(
            f"/tournament/{t.id}/proam-relay/redraw",
            data={
                "num_teams": bad,
                "expected_relay_digest": relay.state_digest(),
            },
        )
        assert r.status_code == 302, f"bad input {bad!r} did not redirect"
        assert (
            len(ProAmRelay(t).get_teams()) == 2
        ), f"bad input {bad!r} should not alter team count"

    def test_dashboard_surfaces_num_teams_selector_when_drawn(
        self, app, auth_client, db_session
    ):
        """Dashboard in drawn state must render the num_teams selector inside the redraw form."""
        from services.proam_relay import ProAmRelay

        t = _seed_relay_tournament(db_session)
        ProAmRelay(t).run_lottery(num_teams=2)

        r = auth_client.get(f"/tournament/{t.id}/proam-relay/")
        assert r.status_code == 200
        html = r.get_data(as_text=True)
        assert 'name="num_teams"' in html, "num_teams select missing from dashboard"
        assert "Max 3 based on current opt-ins" in html, "capacity note missing"
        assert re.search(
            r'name="expected_relay_digest" value="[0-9a-f]{64}"', html
        )


def test_stale_relay_service_instances_merge_disjoint_team_times(
    app, db_session,
):
    """Two judges saving different teams must not replace the other's time."""
    from services.proam_relay import ProAmRelay

    tournament = _seed_relay_tournament(db_session)
    ProAmRelay(tournament).run_lottery(num_teams=2)

    first_judge = ProAmRelay(tournament)
    second_judge = ProAmRelay(tournament)
    first_judge.record_total_time(
        1,
        91.25,
        expected_digest=first_judge.team_state_digest(1),
    )
    second_judge.record_total_time(
        2,
        94.75,
        expected_digest=second_judge.team_state_digest(2),
    )

    persisted = ProAmRelay(tournament)
    assert [team['total_time'] for team in persisted.get_teams()] == [91.25, 94.75]


def test_relay_results_rejects_stale_same_team_token(
    app, auth_client, db_session,
):
    """A stale overwrite of one relay team is refused after the first save."""
    from services.proam_relay import ProAmRelay

    tournament = _seed_relay_tournament(db_session)
    relay = ProAmRelay(tournament)
    relay.run_lottery(num_teams=2)
    expected_digest = relay.team_state_digest(1)

    first = auth_client.post(
        f'/tournament/{tournament.id}/proam-relay/results',
        data={
            'team_number': '1',
            'time_seconds': '91.25',
            'expected_relay_digest': expected_digest,
        },
    )
    stale = auth_client.post(
        f'/tournament/{tournament.id}/proam-relay/results',
        data={
            'team_number': '1',
            'time_seconds': '99.00',
            'expected_relay_digest': expected_digest,
        },
    )

    assert first.status_code == 302
    assert stale.status_code == 409
    assert 'changed since this page was loaded' in stale.get_data(as_text=True)
    assert ProAmRelay(tournament).get_teams()[0]['total_time'] == 91.25


def test_relay_results_rejects_omitted_token_without_mutation(
    app, auth_client, db_session,
):
    """A handcrafted relay result POST cannot bypass the concurrency fence."""
    from services.proam_relay import ProAmRelay

    tournament = _seed_relay_tournament(db_session)
    relay = ProAmRelay(tournament)
    relay.run_lottery(num_teams=2)

    response = auth_client.post(
        f'/tournament/{tournament.id}/proam-relay/results',
        data={
            'team_number': '1',
            'time_seconds': '91.25',
        },
    )

    assert response.status_code == 409
    assert ProAmRelay(tournament).get_teams()[0]['total_time'] is None


@pytest.mark.parametrize(
    ('route_suffix', 'data'),
    [
        ('draw', {'num_teams': '2'}),
        ('redraw', {'num_teams': '2'}),
        ('results', {'team_number': '1', 'time_seconds': '91.25'}),
        ('manual-teams/save', {'teams_json': '[]'}),
        (
            'replace-competitor',
            {
                'team_number': '1',
                'old_competitor_id': '1',
                'new_competitor_id': '2',
                'competitor_type': 'pro',
            },
        ),
    ],
)
def test_all_relay_mutation_routes_reject_omitted_token(
    app,
    auth_client,
    db_session,
    route_suffix,
    data,
):
    """Every relay write endpoint fails closed before parsing its payload."""
    from services.proam_relay import ProAmRelay

    tournament = _seed_relay_tournament(db_session)
    relay = ProAmRelay(tournament)
    relay.run_lottery(num_teams=2)
    state_before = relay.state_digest()

    response = auth_client.post(
        f'/tournament/{tournament.id}/proam-relay/{route_suffix}',
        data=data,
    )

    assert response.status_code == 409
    assert ProAmRelay(tournament).state_digest() == state_before


@pytest.mark.parametrize(
    'route_suffix',
    ['results', 'manual-teams'],
)
def test_relay_mutation_forms_render_nonblank_server_token(
    app, auth_client, db_session, route_suffix,
):
    from services.proam_relay import ProAmRelay

    tournament = _seed_relay_tournament(db_session)
    ProAmRelay(tournament).run_lottery(num_teams=2)

    response = auth_client.get(
        f'/tournament/{tournament.id}/proam-relay/{route_suffix}'
    )

    assert response.status_code == 200
    assert re.search(
        r'name="expected_relay_digest" value="[0-9a-f]{64}"',
        response.get_data(as_text=True),
    )


def test_relay_teams_renders_fenced_replacement_form_for_each_member(
    app, auth_client, db_session,
):
    from services.proam_relay import ProAmRelay

    tournament = _seed_relay_tournament(db_session)
    relay = ProAmRelay(tournament)
    relay.run_lottery(num_teams=2)

    response = auth_client.get(
        f'/tournament/{tournament.id}/proam-relay/teams'
    )

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert html.count('/proam-relay/replace-competitor') == 16
    for team in relay.get_teams():
        expected_digest = relay.team_state_digest(team['team_number'])
        assert html.count(
            f'name="expected_relay_digest" value="{expected_digest}"'
        ) == 8
        for competitor_type, member_key in (
            ('pro', 'pro_members'),
            ('college', 'college_members'),
        ):
            for member in team[member_key]:
                assert (
                    f'name="old_competitor_id" value="{member["id"]}"'
                    in html
                )
                assert f'name="competitor_type" value="{competitor_type}"' in html
