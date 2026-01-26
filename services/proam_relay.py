"""
Pro-Am Relay lottery and management service.

The Pro-Am Relay pairs college and professional competitors into teams.
Each team has 6 members: 3 college competitors and 3 pro competitors.
Teams compete in J&J sawing, Standing Butcher Block, and Underhand Butcher Block.
"""
from database import db
from models import Tournament, Event, EventResult
from models.competitor import CollegeCompetitor, ProCompetitor
import random
import json


class ProAmRelay:
    """Manages the Pro-Am Relay lottery and teams."""

    def __init__(self, tournament: Tournament):
        self.tournament = tournament
        self.relay_data = self._load_relay_data()

    def _load_relay_data(self) -> dict:
        """Load relay data from tournament or create new."""
        # Store in a special event's payouts field
        relay_event = Event.query.filter_by(
            tournament_id=self.tournament.id,
            name='Pro-Am Relay'
        ).first()

        if relay_event:
            try:
                return json.loads(relay_event.payouts or '{}')
            except:
                pass

        return {
            'status': 'not_drawn',  # not_drawn, drawn, in_progress, completed
            'teams': [],
            'eligible_college': [],
            'eligible_pro': [],
            'drawn_college': [],
            'drawn_pro': []
        }

    def _save_relay_data(self):
        """Save relay data to the relay event."""
        relay_event = Event.query.filter_by(
            tournament_id=self.tournament.id,
            name='Pro-Am Relay'
        ).first()

        if not relay_event:
            relay_event = Event(
                tournament_id=self.tournament.id,
                name='Pro-Am Relay',
                event_type='pro',
                scoring_type='time',
                is_partnered=True
            )
            db.session.add(relay_event)

        relay_event.payouts = json.dumps(self.relay_data)
        db.session.commit()

    def get_eligible_pro_competitors(self) -> list:
        """Get pro competitors who opted into the lottery."""
        pros = ProCompetitor.query.filter_by(
            tournament_id=self.tournament.id,
            status='active',
            pro_am_lottery_opt_in=True
        ).all()

        return [{'id': p.id, 'name': p.name, 'gender': p.gender} for p in pros]

    def get_eligible_college_competitors(self) -> list:
        """
        Get all active college competitors.
        All college competitors are eligible for the Pro-Am relay.
        """
        college = CollegeCompetitor.query.filter_by(
            tournament_id=self.tournament.id,
            status='active'
        ).all()

        return [{'id': c.id, 'name': c.name, 'gender': c.gender,
                 'team': c.team.team_code if c.team else 'N/A'} for c in college]

    def run_lottery(self, num_teams: int = 2) -> dict:
        """
        Run the Pro-Am Relay lottery to create teams.

        Each team needs:
        - 3 college competitors (ideally mixed gender)
        - 3 pro competitors (ideally mixed gender)

        Args:
            num_teams: Number of teams to create (default 2)

        Returns:
            Dict with lottery results
        """
        eligible_pro = self.get_eligible_pro_competitors()
        eligible_college = self.get_eligible_college_competitors()

        # Validate we have enough competitors
        pro_needed = num_teams * 3
        college_needed = num_teams * 3

        if len(eligible_pro) < pro_needed:
            raise ValueError(f"Not enough pro competitors opted in. Need {pro_needed}, have {len(eligible_pro)}")

        if len(eligible_college) < college_needed:
            raise ValueError(f"Not enough college competitors. Need {college_needed}, have {len(eligible_college)}")

        # Separate by gender for balanced teams
        pro_male = [p for p in eligible_pro if p['gender'] == 'M']
        pro_female = [p for p in eligible_pro if p['gender'] == 'F']
        college_male = [c for c in eligible_college if c['gender'] == 'M']
        college_female = [c for c in eligible_college if c['gender'] == 'F']

        # Shuffle all pools
        random.shuffle(pro_male)
        random.shuffle(pro_female)
        random.shuffle(college_male)
        random.shuffle(college_female)

        teams = []

        for team_num in range(1, num_teams + 1):
            team = {
                'team_number': team_num,
                'name': f'Team {team_num}',
                'pro_members': [],
                'college_members': [],
                'events': {
                    'jj_sawing': {'result': None, 'status': 'pending'},
                    'standing_block': {'result': None, 'status': 'pending'},
                    'underhand': {'result': None, 'status': 'pending'}
                },
                'total_time': None
            }

            # Draw pro competitors (try to get gender balance)
            for i in range(3):
                if i == 0 and pro_female:
                    team['pro_members'].append(pro_female.pop(0))
                elif pro_male:
                    team['pro_members'].append(pro_male.pop(0))
                elif pro_female:
                    team['pro_members'].append(pro_female.pop(0))

            # Draw college competitors (try to get gender balance)
            for i in range(3):
                if i == 0 and college_female:
                    team['college_members'].append(college_female.pop(0))
                elif college_male:
                    team['college_members'].append(college_male.pop(0))
                elif college_female:
                    team['college_members'].append(college_female.pop(0))

            teams.append(team)

        # Store results
        self.relay_data['status'] = 'drawn'
        self.relay_data['teams'] = teams
        self.relay_data['eligible_pro'] = eligible_pro
        self.relay_data['eligible_college'] = eligible_college
        self.relay_data['drawn_pro'] = [m for t in teams for m in t['pro_members']]
        self.relay_data['drawn_college'] = [m for t in teams for m in t['college_members']]

        self._save_relay_data()

        return {
            'success': True,
            'teams': teams,
            'message': f'Successfully drew {num_teams} teams!'
        }

    def redraw_lottery(self) -> dict:
        """Clear and redraw the lottery."""
        self.relay_data = {
            'status': 'not_drawn',
            'teams': [],
            'eligible_college': [],
            'eligible_pro': [],
            'drawn_college': [],
            'drawn_pro': []
        }
        self._save_relay_data()
        return self.run_lottery()

    def get_teams(self) -> list:
        """Get the current teams."""
        return self.relay_data.get('teams', [])

    def get_status(self) -> str:
        """Get the current lottery status."""
        return self.relay_data.get('status', 'not_drawn')

    def record_event_result(self, team_number: int, event_name: str, time_seconds: float):
        """
        Record a result for a team's event.

        Args:
            team_number: Team number (1 or 2)
            event_name: 'jj_sawing', 'standing_block', or 'underhand'
            time_seconds: Time in seconds
        """
        teams = self.relay_data.get('teams', [])

        for team in teams:
            if team['team_number'] == team_number:
                if event_name in team['events']:
                    team['events'][event_name]['result'] = time_seconds
                    team['events'][event_name]['status'] = 'completed'

                    # Recalculate total time
                    total = 0
                    all_complete = True
                    for evt in team['events'].values():
                        if evt['result'] is not None:
                            total += evt['result']
                        else:
                            all_complete = False

                    team['total_time'] = total if all_complete else None

        # Check if relay is complete
        all_teams_complete = all(
            all(evt['status'] == 'completed' for evt in t['events'].values())
            for t in teams
        )

        if all_teams_complete:
            self.relay_data['status'] = 'completed'

        self._save_relay_data()

    def get_results(self) -> list:
        """Get relay results sorted by total time."""
        teams = self.relay_data.get('teams', [])
        completed = [t for t in teams if t.get('total_time') is not None]
        return sorted(completed, key=lambda t: t['total_time'])

    def replace_competitor(self, team_number: int, old_competitor_id: int,
                          new_competitor_id: int, competitor_type: str):
        """
        Replace a competitor on a team (e.g., due to injury).

        Args:
            team_number: Team number
            old_competitor_id: ID of competitor to replace
            new_competitor_id: ID of replacement competitor
            competitor_type: 'pro' or 'college'
        """
        teams = self.relay_data.get('teams', [])

        # Get new competitor info
        if competitor_type == 'pro':
            new_comp = ProCompetitor.query.get(new_competitor_id)
            member_key = 'pro_members'
        else:
            new_comp = CollegeCompetitor.query.get(new_competitor_id)
            member_key = 'college_members'

        if not new_comp:
            raise ValueError("Replacement competitor not found")

        new_comp_data = {
            'id': new_comp.id,
            'name': new_comp.name,
            'gender': new_comp.gender
        }

        for team in teams:
            if team['team_number'] == team_number:
                for i, member in enumerate(team[member_key]):
                    if member['id'] == old_competitor_id:
                        team[member_key][i] = new_comp_data
                        break

        self._save_relay_data()


def get_proam_relay(tournament: Tournament) -> ProAmRelay:
    """Get the Pro-Am Relay manager for a tournament."""
    return ProAmRelay(tournament)


def create_proam_relay_event(tournament: Tournament) -> Event:
    """Create the Pro-Am Relay event for a tournament."""
    relay_event = Event.query.filter_by(
        tournament_id=tournament.id,
        name='Pro-Am Relay'
    ).first()

    if not relay_event:
        relay_event = Event(
            tournament_id=tournament.id,
            name='Pro-Am Relay',
            event_type='pro',
            scoring_type='time',
            is_partnered=True,
            status='pending'
        )
        db.session.add(relay_event)
        db.session.commit()

    return relay_event
