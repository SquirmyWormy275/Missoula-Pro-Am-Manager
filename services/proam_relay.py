"""
Pro-Am Relay lottery and management service.

The Pro-Am Relay pairs college and professional competitors into teams.
Each team has 8 members:
- 2 Professional Men
- 2 Professional Women
- 2 College Men
- 2 College Women
"""
from database import db
from models import Tournament, Event, EventResult
from models.competitor import CollegeCompetitor, ProCompetitor
import random
import json


class ProAmRelay:
    """Manages the Pro-Am Relay lottery and teams."""
    RELAY_EVENTS = (
        'partnered_sawing',
        'standing_butcher_block',
        'underhand_butcher_block',
        'team_axe_throw',
    )

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
        Get active college competitors who opted into the relay lottery.
        """
        college = CollegeCompetitor.query.filter_by(tournament_id=self.tournament.id, status='active').all()
        college = [c for c in college if c.pro_am_lottery_opt_in]

        return [{'id': c.id, 'name': c.name, 'gender': c.gender,
                 'team': c.team.team_code if c.team else 'N/A'} for c in college]

    def get_lottery_capacity(self) -> dict:
        """Return gender pool counts and max number of valid 8-person teams."""
        eligible_pro = self.get_eligible_pro_competitors()
        eligible_college = self.get_eligible_college_competitors()

        pro_male = len([p for p in eligible_pro if p['gender'] == 'M'])
        pro_female = len([p for p in eligible_pro if p['gender'] == 'F'])
        college_male = len([c for c in eligible_college if c['gender'] == 'M'])
        college_female = len([c for c in eligible_college if c['gender'] == 'F'])

        max_teams = min(pro_male // 2, pro_female // 2, college_male // 2, college_female // 2)
        return {
            'pro_male': pro_male,
            'pro_female': pro_female,
            'college_male': college_male,
            'college_female': college_female,
            'max_teams': max_teams,
        }

    def run_lottery(self, num_teams: int = 2) -> dict:
        """
        Run the Pro-Am Relay lottery to create teams.

        Each team needs:
        - 2 pro men
        - 2 pro women
        - 2 college men
        - 2 college women

        Args:
            num_teams: Number of teams to create (default 2)

        Returns:
            Dict with lottery results
        """
        eligible_pro = self.get_eligible_pro_competitors()
        eligible_college = self.get_eligible_college_competitors()

        # Separate by gender for balanced teams
        pro_male = [p for p in eligible_pro if p['gender'] == 'M']
        pro_female = [p for p in eligible_pro if p['gender'] == 'F']
        college_male = [c for c in eligible_college if c['gender'] == 'M']
        college_female = [c for c in eligible_college if c['gender'] == 'F']

        required_per_bucket = num_teams * 2
        if len(pro_male) < required_per_bucket:
            raise ValueError(
                f"Not enough pro men opted in. Need {required_per_bucket}, have {len(pro_male)}"
            )
        if len(pro_female) < required_per_bucket:
            raise ValueError(
                f"Not enough pro women opted in. Need {required_per_bucket}, have {len(pro_female)}"
            )
        if len(college_male) < required_per_bucket:
            raise ValueError(
                f"Not enough college men opted in. Need {required_per_bucket}, have {len(college_male)}"
            )
        if len(college_female) < required_per_bucket:
            raise ValueError(
                f"Not enough college women opted in. Need {required_per_bucket}, have {len(college_female)}"
            )

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
                    'partnered_sawing': {'result': None, 'status': 'pending'},
                    'standing_butcher_block': {'result': None, 'status': 'pending'},
                    'underhand_butcher_block': {'result': None, 'status': 'pending'},
                    'team_axe_throw': {'result': None, 'status': 'pending'},
                },
                'total_time': None
            }

            # Exactly 2 male + 2 female from each division per team.
            team['pro_members'].append(pro_male.pop(0))
            team['pro_members'].append(pro_male.pop(0))
            team['pro_members'].append(pro_female.pop(0))
            team['pro_members'].append(pro_female.pop(0))
            team['college_members'].append(college_male.pop(0))
            team['college_members'].append(college_male.pop(0))
            team['college_members'].append(college_female.pop(0))
            team['college_members'].append(college_female.pop(0))

            random.shuffle(team['pro_members'])
            random.shuffle(team['college_members'])

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
            'message': f'Successfully drew {num_teams} team(s) of 8 competitors each.'
        }

    def redraw_lottery(self, num_teams: int = 2) -> dict:
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
        return self.run_lottery(num_teams=num_teams)

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
            event_name: One of the configured relay event keys
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
                    self.relay_data['status'] = 'in_progress'

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
                        if member.get('gender') != new_comp_data['gender']:
                            raise ValueError("Replacement competitor must match the same gender")
                        if competitor_type == 'pro' and not new_comp.pro_am_lottery_opt_in:
                            raise ValueError("Replacement pro competitor must be opted into Pro-Am lottery")
                        if competitor_type == 'college' and not new_comp.pro_am_lottery_opt_in:
                            raise ValueError("Replacement college competitor must be opted into Pro-Am lottery")
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
