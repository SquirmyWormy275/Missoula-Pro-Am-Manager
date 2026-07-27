"""
Tests for compute_team_health() and replace_competitor() health warning.

RED phase: these tests fail before the implementation exists.

Run: pytest tests/test_relay_team_health.py -v
"""

from unittest.mock import MagicMock, patch

import pytest

from services.proam_relay import ProAmRelay, compute_team_health

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _relay():
    """Construct a ProAmRelay without hitting the DB."""
    with patch("services.proam_relay.Event") as mock_ev:
        mock_ev.query.filter_by.return_value.first.return_value = None
        relay = ProAmRelay(MagicMock())
    relay.relay_data = {
        "status": "drawn",
        "teams": [],
        "eligible_college": [],
        "eligible_pro": [],
        "drawn_college": [],
        "drawn_pro": [],
    }
    return relay


def _member(mid, name, gender, division="pro"):
    return {"id": mid, "name": name, "gender": gender}


def _make_full_team():
    """8-member team: 2PM, 2PF, 2CM, 2CF — all IDs 1-8."""
    return {
        "team_number": 1,
        "name": "Team 1",
        "pro_members": [
            _member(1, "ProM1", "M"),
            _member(2, "ProM2", "M"),
            _member(3, "ProF1", "F"),
            _member(4, "ProF2", "F"),
        ],
        "college_members": [
            _member(5, "ColM1", "M"),
            _member(6, "ColM2", "M"),
            _member(7, "ColF1", "F"),
            _member(8, "ColF2", "F"),
        ],
        "events": {},
        "total_time": None,
    }


def _mock_competitor_lookup(status_map):
    """
    Return a side_effect function that mocks ProCompetitor.query.get() and
    CollegeCompetitor.query.get() based on {id: status} map.
    """

    def _get(cid):
        mock = MagicMock()
        mock.status = status_map.get(cid, "active")
        return mock

    return _get


# ---------------------------------------------------------------------------
# compute_team_health — happy path: full active roster -> green
# ---------------------------------------------------------------------------


class TestComputeTeamHealthGreen:
    def test_full_active_roster_is_green(self):
        team = _make_full_team()
        tournament = MagicMock()

        # All 8 members active
        status_map = {i: "active" for i in range(1, 9)}

        with (
            patch("services.proam_relay.ProCompetitor") as mock_pro,
            patch("services.proam_relay.CollegeCompetitor") as mock_col,
        ):
            mock_pro.query.get.side_effect = _mock_competitor_lookup(status_map)
            mock_col.query.get.side_effect = _mock_competitor_lookup(status_map)
            result = compute_team_health(team, tournament)

        assert result["status"] == "green"
        assert "detail" in result

    def test_green_detail_mentions_full_roster(self):
        team = _make_full_team()
        tournament = MagicMock()
        status_map = {i: "active" for i in range(1, 9)}

        with (
            patch("services.proam_relay.ProCompetitor") as mock_pro,
            patch("services.proam_relay.CollegeCompetitor") as mock_col,
        ):
            mock_pro.query.get.side_effect = _mock_competitor_lookup(status_map)
            mock_col.query.get.side_effect = _mock_competitor_lookup(status_map)
            result = compute_team_health(team, tournament)

        assert result["status"] == "green"


# ---------------------------------------------------------------------------
# compute_team_health — yellow: 1-2 inactive, minimums still met
# ---------------------------------------------------------------------------


class TestComputeTeamHealthYellow:
    def test_one_scratched_pro_male_still_yellow(self):
        """1 pro male scratched: pro has 1M, 2F active. Minimum 3 active per
        division still met overall if we count 3 active pros (1M+2F) and 4
        active college. At least 1M and 1F per division met."""
        team = _make_full_team()
        tournament = MagicMock()
        # Scratch pro member id=1 (ProM1)
        status_map = {
            1: "scratched",
            2: "active",
            3: "active",
            4: "active",
            5: "active",
            6: "active",
            7: "active",
            8: "active",
        }

        with (
            patch("services.proam_relay.ProCompetitor") as mock_pro,
            patch("services.proam_relay.CollegeCompetitor") as mock_col,
        ):
            mock_pro.query.get.side_effect = _mock_competitor_lookup(status_map)
            mock_col.query.get.side_effect = _mock_competitor_lookup(status_map)
            result = compute_team_health(team, tournament)

        assert result["status"] == "yellow"

    def test_two_scratched_balanced_still_yellow(self):
        """1 pro male + 1 college female scratched: each division still has
        1M+1F active (>= minimums)."""
        team = _make_full_team()
        tournament = MagicMock()
        status_map = {
            1: "scratched",
            2: "active",
            3: "active",
            4: "active",
            5: "active",
            6: "active",
            7: "active",
            8: "scratched",
        }

        with (
            patch("services.proam_relay.ProCompetitor") as mock_pro,
            patch("services.proam_relay.CollegeCompetitor") as mock_col,
        ):
            mock_pro.query.get.side_effect = _mock_competitor_lookup(status_map)
            mock_col.query.get.side_effect = _mock_competitor_lookup(status_map)
            result = compute_team_health(team, tournament)

        assert result["status"] == "yellow"

    def test_yellow_detail_names_scratched_members(self):
        team = _make_full_team()
        tournament = MagicMock()
        status_map = {
            1: "scratched",
            2: "active",
            3: "active",
            4: "active",
            5: "active",
            6: "active",
            7: "active",
            8: "active",
        }

        with (
            patch("services.proam_relay.ProCompetitor") as mock_pro,
            patch("services.proam_relay.CollegeCompetitor") as mock_col,
        ):
            mock_pro.query.get.side_effect = _mock_competitor_lookup(status_map)
            mock_col.query.get.side_effect = _mock_competitor_lookup(status_map)
            result = compute_team_health(team, tournament)

        assert (
            "ProM1" in result["detail"]
            or "1 inactive" in result["detail"]
            or "scratched" in result["detail"].lower()
        )


# ---------------------------------------------------------------------------
# compute_team_health — red: below minimum threshold
# ---------------------------------------------------------------------------


class TestComputeTeamHealthRed:
    def test_three_pro_scratched_is_red(self):
        """3 pro members scratched: only 1 pro active — below minimum of 3."""
        team = _make_full_team()
        tournament = MagicMock()
        # Scratch ids 1,2,3 (both pro males + one pro female)
        status_map = {
            1: "scratched",
            2: "scratched",
            3: "scratched",
            4: "active",
            5: "active",
            6: "active",
            7: "active",
            8: "active",
        }

        with (
            patch("services.proam_relay.ProCompetitor") as mock_pro,
            patch("services.proam_relay.CollegeCompetitor") as mock_col,
        ):
            mock_pro.query.get.side_effect = _mock_competitor_lookup(status_map)
            mock_col.query.get.side_effect = _mock_competitor_lookup(status_map)
            result = compute_team_health(team, tournament)

        assert result["status"] == "red"

    def test_all_college_females_scratched_is_red(self):
        """0 active college females — gender balance broken in college division."""
        team = _make_full_team()
        tournament = MagicMock()
        # Scratch ids 7,8 (both college females)
        status_map = {
            1: "active",
            2: "active",
            3: "active",
            4: "active",
            5: "active",
            6: "active",
            7: "scratched",
            8: "scratched",
        }

        with (
            patch("services.proam_relay.ProCompetitor") as mock_pro,
            patch("services.proam_relay.CollegeCompetitor") as mock_col,
        ):
            mock_pro.query.get.side_effect = _mock_competitor_lookup(status_map)
            mock_col.query.get.side_effect = _mock_competitor_lookup(status_map)
            result = compute_team_health(team, tournament)

        assert result["status"] == "red"

    def test_all_pro_males_scratched_is_red(self):
        """0 active pro males — gender balance broken in pro division."""
        team = _make_full_team()
        tournament = MagicMock()
        status_map = {
            1: "scratched",
            2: "scratched",
            3: "active",
            4: "active",
            5: "active",
            6: "active",
            7: "active",
            8: "active",
        }

        with (
            patch("services.proam_relay.ProCompetitor") as mock_pro,
            patch("services.proam_relay.CollegeCompetitor") as mock_col,
        ):
            mock_pro.query.get.side_effect = _mock_competitor_lookup(status_map)
            mock_col.query.get.side_effect = _mock_competitor_lookup(status_map)
            result = compute_team_health(team, tournament)

        assert result["status"] == "red"

    def test_red_detail_is_non_empty(self):
        team = _make_full_team()
        tournament = MagicMock()
        status_map = {
            1: "scratched",
            2: "scratched",
            3: "scratched",
            4: "active",
            5: "active",
            6: "active",
            7: "active",
            8: "active",
        }

        with (
            patch("services.proam_relay.ProCompetitor") as mock_pro,
            patch("services.proam_relay.CollegeCompetitor") as mock_col,
        ):
            mock_pro.query.get.side_effect = _mock_competitor_lookup(status_map)
            mock_col.query.get.side_effect = _mock_competitor_lookup(status_map)
            result = compute_team_health(team, tournament)

        assert result["detail"]


# ---------------------------------------------------------------------------
# replace_competitor — health warning attached to return value
# ---------------------------------------------------------------------------


class TestReplaceCompetitorHealthWarning:
    def _relay_with_team(self):
        relay = _relay()
        team = _make_full_team()
        relay.relay_data["teams"] = [team]
        return relay

    def test_replace_with_active_new_comp_returns_health(self):
        """replace_competitor() should return a dict with 'health' key."""
        relay = self._relay_with_team()

        new_comp = MagicMock()
        new_comp.id = 99
        new_comp.name = "NewProM"
        new_comp.gender = "M"
        new_comp.pro_am_lottery_opt_in = True

        # All remaining members still active after replacement
        all_active = {i: "active" for i in range(1, 9)}
        all_active[99] = "active"

        with (
            patch("services.proam_relay.ProCompetitor") as mock_pro,
            patch("services.proam_relay.CollegeCompetitor") as mock_col,
            patch.object(relay, "_save_relay_data"),
        ):
            mock_pro.query.filter_by.return_value.first.return_value = new_comp
            mock_pro.query.get.side_effect = _mock_competitor_lookup(all_active)
            mock_col.query.get.side_effect = _mock_competitor_lookup(all_active)
            result = relay.replace_competitor(1, 1, 99, "pro")

        assert result is not None
        assert "health" in result

    def test_replace_creating_red_team_warns_in_health(self):
        """When replacement leaves team red, health status is 'red' in return."""
        relay = self._relay_with_team()

        # New comp is active, but we will scratch many others to force red
        new_comp = MagicMock()
        new_comp.id = 99
        new_comp.name = "NewProM"
        new_comp.gender = "M"
        new_comp.pro_am_lottery_opt_in = True

        # After replacement: ids 2,3 scratched → only 1 active pro male (id 99),
        # 1 active pro female (id 4) → pro active count=2, below minimum of 3
        post_status = {
            99: "active",
            2: "scratched",
            3: "scratched",
            4: "active",
            5: "active",
            6: "active",
            7: "active",
            8: "active",
        }

        with (
            patch("services.proam_relay.ProCompetitor") as mock_pro,
            patch("services.proam_relay.CollegeCompetitor") as mock_col,
            patch.object(relay, "_save_relay_data"),
        ):
            mock_pro.query.filter_by.return_value.first.return_value = new_comp
            mock_pro.query.get.side_effect = _mock_competitor_lookup(post_status)
            mock_col.query.get.side_effect = _mock_competitor_lookup(post_status)
            result = relay.replace_competitor(1, 1, 99, "pro")

        assert result["health"]["status"] == "red"

    def test_replace_still_allows_when_red(self):
        """Replace is allowed even when it results in red — judge may have no choice."""
        relay = self._relay_with_team()

        new_comp = MagicMock()
        new_comp.id = 99
        new_comp.name = "NewProM"
        new_comp.gender = "M"
        new_comp.pro_am_lottery_opt_in = True

        post_status = {
            99: "active",
            2: "scratched",
            3: "scratched",
            4: "active",
            5: "active",
            6: "active",
            7: "active",
            8: "active",
        }

        # Should NOT raise
        with (
            patch("services.proam_relay.ProCompetitor") as mock_pro,
            patch("services.proam_relay.CollegeCompetitor") as mock_col,
            patch.object(relay, "_save_relay_data"),
        ):
            mock_pro.query.filter_by.return_value.first.return_value = new_comp
            mock_pro.query.get.side_effect = _mock_competitor_lookup(post_status)
            mock_col.query.get.side_effect = _mock_competitor_lookup(post_status)
            result = relay.replace_competitor(1, 1, 99, "pro")

        # Replacement happened — new comp is in team
        team = relay.relay_data["teams"][0]
        pro_ids = [m["id"] for m in team["pro_members"]]
        assert 99 in pro_ids
        assert 1 not in pro_ids
