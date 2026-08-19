"""
Pro-Am Relay lottery and management service.

The Pro-Am Relay pairs college and professional competitors into teams.
Each team has 8 members:
- 2 Professional Men
- 2 Professional Women
- 2 College Men
- 2 College Women
"""
import copy
import hashlib
import hmac
import json
import math
import random
from functools import wraps

from flask import has_app_context, has_request_context

from database import db
from models import Event, EventResult, Tournament
from models.competitor import CollegeCompetitor, ProCompetitor
from models.relay import RelayState, RelayTeam, RelayTeamEvent, RelayTeamMember


class RelayStateConflict(RuntimeError):
    """The relay state no longer matches the operator's reviewed snapshot."""


def _relay_writer(func):
    """Serialize a relay write and reload its snapshot after taking the lock."""
    @wraps(func)
    def locked(self, *args, **kwargs):
        if not has_app_context():
            return func(self, *args, **kwargs)

        from services.flight_builder import (
            lock_tournament_schedule,
            sqlite_schedule_writer_guard,
        )

        with sqlite_schedule_writer_guard(self.tournament):
            self.tournament = lock_tournament_schedule(self.tournament)
            self.relay_data = self._load_relay_data()
            return func(self, *args, **kwargs)

    return locked


def relay_payout_summary(tournament: Tournament) -> dict:
    """Return payable, team-level Relay awards for a completed Relay.

    Relay money is intentionally separate from individual EventResult payouts.
    A team earns the configured amount for its final placement, and its
    RelayTeam row carries the binary settlement state.
    """
    relay_event = Event.query.filter_by(
        tournament_id=tournament.id,
        name='Pro-Am Relay',
    ).first()
    if relay_event is None:
        return _empty_relay_payout_summary()

    state = RelayState.query.filter_by(event_id=relay_event.id).first()
    # Partial relay times are provisional. Do not expose a payable placement
    # until the whole relay has a final ordering.
    if state is None or state.status != 'completed':
        return _empty_relay_payout_summary()

    payouts = relay_event.get_payouts()
    rows = []
    teams = RelayTeam.query.filter_by(relay_state_id=state.id).filter(
        RelayTeam.total_time.isnot(None)
    ).order_by(RelayTeam.total_time, RelayTeam.team_number).all()
    for placement, team in enumerate(teams, start=1):
        try:
            amount = float(payouts.get(str(placement), 0) or 0)
        except (TypeError, ValueError):
            amount = 0.0
        if amount <= 0:
            continue
        rows.append({
            'team': team,
            'placement': placement,
            'payout_amount': amount,
        })

    total_owed = sum(row['payout_amount'] for row in rows)
    total_settled = sum(
        row['payout_amount'] for row in rows if row['team'].payout_settled
    )
    return {
        'rows': rows,
        'total_owed': total_owed,
        'total_settled': total_settled,
        'total_outstanding': total_owed - total_settled,
    }


def _empty_relay_payout_summary() -> dict:
    return {
        'rows': [],
        'total_owed': 0.0,
        'total_settled': 0.0,
        'total_outstanding': 0.0,
    }


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
        """Load relay data from tournament or create new.

        Reads normalized Relay tables first, then event_state or payouts only
        for legacy documents that could not be projected safely.
        """
        relay_event = Event.query.filter_by(
            tournament_id=self.tournament.id,
            name='Pro-Am Relay'
        ).first()

        if relay_event:
            if has_app_context():
                table_data = self._load_table_relay_data(relay_event)
                if table_data is not None:
                    return table_data
            # Primary: event_state column (new path)
            raw = relay_event.event_state
            if not raw:
                # Fallback: payouts column (legacy path)
                raw = relay_event.payouts
            try:
                return json.loads(raw or '{}')
            except (json.JSONDecodeError, TypeError):
                # A legacy state field may be empty or malformed. Other
                # decoder failures are operational errors and must surface.
                pass

        return {
            'status': 'not_drawn',  # not_drawn, drawn, in_progress, completed
            'teams': [],
            'eligible_college': [],
            'eligible_pro': [],
            'drawn_college': [],
            'drawn_pro': []
        }

    def _load_table_relay_data(self, relay_event: Event) -> dict | None:
        """Rebuild the existing relay view payload from its row projection.

        A missing or incomplete projection remains a JSON-reader case. This is
        deliberate during the staged migration: a legacy document that could
        not safely project is still usable, rather than becoming an empty
        relay because it lacks rows.
        """
        state = RelayState.query.filter_by(event_id=relay_event.id).first()
        if state is None:
            return None

        teams = RelayTeam.query.filter_by(relay_state_id=state.id).order_by(
            RelayTeam.team_number
        ).all()
        team_ids = [team.id for team in teams]
        members_by_team = {team_id: [] for team_id in team_ids}
        for member in RelayTeamMember.query.filter(
            RelayTeamMember.relay_team_id.in_(team_ids)
        ).order_by(RelayTeamMember.id):
            members_by_team[member.relay_team_id].append(member.uid)

        all_uids = {uid for uids in members_by_team.values() for uid in uids}
        pro_by_uid = {
            competitor.uid: competitor
            for competitor in ProCompetitor.query.filter(ProCompetitor.uid.in_(all_uids)).all()
        }
        college_by_uid = {
            competitor.uid: competitor
            for competitor in CollegeCompetitor.query.filter(
                CollegeCompetitor.uid.in_(all_uids)
            ).all()
        }
        if len(pro_by_uid) + len(college_by_uid) != len(all_uids):
            return None

        events_by_team = {team_id: {} for team_id in team_ids}
        for team_event in RelayTeamEvent.query.filter(
            RelayTeamEvent.relay_team_id.in_(team_ids)
        ).all():
            events_by_team[team_event.relay_team_id][team_event.event_key] = {
                'result': team_event.result,
                'status': team_event.status,
            }

        rendered_teams = []
        drawn_pro = []
        drawn_college = []
        for team in teams:
            event_data = events_by_team[team.id]
            if set(event_data) != set(self.RELAY_EVENTS):
                return None
            pro_members = []
            college_members = []
            for uid in members_by_team[team.id]:
                pro = pro_by_uid.get(uid)
                if pro is not None:
                    member = {'id': pro.id, 'name': pro.name, 'gender': pro.gender}
                    pro_members.append(member)
                    drawn_pro.append(member)
                    continue
                college = college_by_uid[uid]
                member = {
                    'id': college.id,
                    'name': college.name,
                    'gender': college.gender,
                    'team': college.team.team_code if college.team else 'N/A',
                }
                college_members.append(member)
                drawn_college.append(member)
            rendered_teams.append({
                'team_number': team.team_number,
                'name': team.name,
                'pro_members': pro_members,
                'college_members': college_members,
                'events': event_data,
                'total_time': team.total_time,
            })

        return {
            'status': state.status,
            'teams': rendered_teams,
            'eligible_college': [],
            'eligible_pro': [],
            'drawn_college': drawn_college,
            'drawn_pro': drawn_pro,
        }

    def _save_relay_data(self, commit: bool = True):
        """Save relay data to normalized tables when its shape is complete.

        A successful table projection clears ``event_state`` so new Relay
        writes have one authoritative store. An incomplete legacy document
        stays in JSON because it cannot yet be represented without losing
        data. Payouts are never used for Relay state on this path.

        Args:
            commit: When True (default) commits immediately so existing
                callers are unaffected.  Pass commit=False to flush into
                the current session without committing so the caller can
                wrap multiple saves in a single outer transaction.
        """
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

        # A first write creates the Event above, so establish its id before
        # creating RelayState with the event foreign key.
        db.session.flush()
        if self._has_projectable_teams():
            member_uids = self._relay_member_uids()
            self._sync_relay_tables(relay_event, member_uids)
            relay_event.event_state = None
        else:
            relay_event.event_state = json.dumps(self.relay_data)
        if commit:
            db.session.commit()
        else:
            db.session.flush()

    def _has_projectable_teams(self) -> bool:
        """Return whether current state has the complete team shape for row sync.

        The service's transaction API also supports saving a deliberately
        minimal state document before a draw exists. It remains valid legacy
        state, but it is not a roster that normalized tables can represent.
        """
        teams = self.relay_data.get("teams", [])
        if not isinstance(teams, list):
            return False
        for team in teams:
            if not isinstance(team, dict) or not isinstance(team.get("name"), str):
                return False
            if not isinstance(team.get("pro_members"), list):
                return False
            if not isinstance(team.get("college_members"), list):
                return False
            events = team.get("events")
            if not isinstance(events, dict) or set(events) != set(self.RELAY_EVENTS):
                return False
        return True

    def _relay_member_uids(self) -> dict[str, dict[int, int]]:
        """Resolve legacy member ids to identity uids before mutating either store."""
        member_ids = {"pro": set(), "college": set()}
        for team in self.relay_data.get("teams", []):
            for member in team.get("pro_members", []):
                member_ids["pro"].add(member.get("id"))
            for member in team.get("college_members", []):
                member_ids["college"].add(member.get("id"))

        resolved = {"pro": {}, "college": {}}
        for kind, model in (("pro", ProCompetitor), ("college", CollegeCompetitor)):
            ids = {member_id for member_id in member_ids[kind] if isinstance(member_id, int)}
            if not ids:
                continue
            rows = model.query.filter(
                model.id.in_(ids), model.tournament_id == self.tournament.id
            ).all()
            resolved[kind] = {row.id: row.uid for row in rows}
            missing = ids - set(resolved[kind])
            if missing:
                raise ValueError("Relay roster contains a competitor outside this tournament")
        return resolved

    def _sync_relay_tables(self, relay_event: Event, member_uids: dict[str, dict[int, int]]):
        """Replace the row projection in the same transaction as legacy state.

        Membership records carry durable competitor identities. A team-event
        record deliberately carries only the team, event key, result, and
        completion state: teams choose their own relay legs and those results
        never enter individual or college scoring.
        """
        state = RelayState.query.filter_by(event_id=relay_event.id).first()
        if state is None:
            state = RelayState(event_id=relay_event.id)
            db.session.add(state)
            db.session.flush()

        state.status = self.relay_data.get("status", "not_drawn")
        existing_teams = RelayTeam.query.filter_by(relay_state_id=state.id).all()
        existing_uids = {row.id: set() for row in existing_teams}
        for member in RelayTeamMember.query.filter(
            RelayTeamMember.relay_team_id.in_(existing_uids)
        ):
            existing_uids[member.relay_team_id].add(member.uid)
        existing_events = {row.id: {} for row in existing_teams}
        for event in RelayTeamEvent.query.filter(
            RelayTeamEvent.relay_team_id.in_(existing_events)
        ):
            existing_events[event.relay_team_id][event.event_key] = {
                'result': event.result,
                'status': event.status,
            }
        existing_settlements = {
            row.team_number: (
                row.payout_settled,
                self._relay_team_fingerprint(
                    row.name,
                    existing_uids[row.id],
                    row.total_time,
                    existing_events[row.id],
                ),
            )
            for row in existing_teams
        }
        if existing_teams:
            # Use ORM deletion so the identity map cannot keep a deleted team
            # under an id SQLite may immediately reuse for its replacement.
            # Relationships own the member and leg cascades.
            for existing_team in existing_teams:
                db.session.delete(existing_team)
            db.session.flush()

        seen_uids = set()
        for team_data in self.relay_data.get("teams", []):
            incoming_uids = set()
            for key, kind in (("pro_members", "pro"), ("college_members", "college")):
                for member in team_data.get(key, []):
                    uid = member_uids[kind].get(member.get("id"))
                    if uid is None:
                        raise ValueError("Relay roster contains an unresolved competitor")
                    incoming_uids.add(uid)
            previous = existing_settlements.get(team_data["team_number"])
            fingerprint = self._relay_team_fingerprint(
                team_data["name"],
                incoming_uids,
                team_data.get("total_time"),
                team_data["events"],
            )
            team = RelayTeam(
                relay_state_id=state.id,
                team_number=team_data["team_number"],
                name=team_data["name"],
                total_time=team_data.get("total_time"),
                payout_settled=bool(previous and previous[0] and previous[1] == fingerprint),
            )
            db.session.add(team)
            db.session.flush()

            for key, kind in (("pro_members", "pro"), ("college_members", "college")):
                for member in team_data.get(key, []):
                    uid = member_uids[kind].get(member.get("id"))
                    if uid in seen_uids:
                        raise ValueError("A relay competitor cannot appear on multiple teams")
                    seen_uids.add(uid)
                    db.session.add(RelayTeamMember(
                        relay_state_id=state.id,
                        relay_team_id=team.id,
                        uid=uid,
                    ))

            for event_key in self.RELAY_EVENTS:
                event_data = team_data["events"][event_key]
                db.session.add(RelayTeamEvent(
                    relay_team_id=team.id,
                    event_key=event_key,
                    result=event_data.get("result"),
                    status=event_data.get("status", "pending"),
                ))

    def _relay_team_fingerprint(
        self,
        name: str,
        member_uids: set[int],
        total_time: float | None,
        events: dict,
    ) -> tuple:
        """Return the immutable payment identity for one finalized relay team.

        A settlement acknowledges a particular team and result, not merely a
        team number. Correcting the roster, display name, aggregate time, or
        any leg result must return the payout to pending review.
        """
        legs = tuple(
            (
                event_key,
                (events.get(event_key) or {}).get('result'),
                (events.get(event_key) or {}).get('status', 'pending'),
            )
            for event_key in self.RELAY_EVENTS
        )
        return (name, tuple(sorted(member_uids)), total_time, legs)

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

    @staticmethod
    def _state_hash(payload) -> str:
        encoded = json.dumps(
            payload,
            sort_keys=True,
            separators=(',', ':'),
            ensure_ascii=True,
        ).encode('ascii')
        return hashlib.sha256(encoded).hexdigest()

    def state_digest(self) -> str:
        """Digest the full relay roster/results snapshot for replacement writes."""
        return self._state_hash({
            'status': self.get_status(),
            'teams': sorted(
                self.get_teams(), key=lambda team: team.get('team_number', 0)
            ),
        })

    def team_state_digest(self, team_number: int) -> str:
        """Digest one team so writes to different teams remain compatible."""
        team = next(
            (
                row for row in self.get_teams()
                if row.get('team_number') == int(team_number)
            ),
            None,
        )
        return self._state_hash({'team_number': int(team_number), 'team': team})

    def require_state_digest(
        self,
        expected_digest: str | None,
        *,
        team_number: int | None = None,
    ) -> None:
        """Reject a missing or stale full-state/team-state operator token."""
        current = (
            self.team_state_digest(team_number)
            if team_number is not None
            else self.state_digest()
        )
        if not expected_digest or not hmac.compare_digest(expected_digest, current):
            raise RelayStateConflict(
                'Relay state changed since this page was loaded. '
                'Refresh and review the current relay before saving again.'
            )

    def require_request_state_digest(
        self,
        expected_digest: str | None,
        *,
        team_number: int | None = None,
    ) -> None:
        """Fence every HTTP mutation while preserving trusted setup callers."""
        if has_request_context() or expected_digest is not None:
            self.require_state_digest(
                expected_digest,
                team_number=team_number,
            )

    @_relay_writer
    def run_lottery(
        self,
        num_teams: int = 2,
        expected_digest: str | None = None,
    ) -> dict:
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
        self.require_request_state_digest(expected_digest)

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

    @_relay_writer
    def redraw_lottery(
        self,
        num_teams: int = 2,
        expected_digest: str | None = None,
    ) -> dict:
        """Clear and redraw the lottery.

        The clear is in memory only. It used to be written through
        _save_relay_data(), which commits, and only then was run_lottery
        called — and run_lottery raises ValueError when any gender bucket is
        short. The route catches that and flashes the message, by which point
        the drawn teams were already gone from the database. The shuffle is
        unseeded, so the rosters that had been announced could not be
        reproduced, in the app or out of it.

        Nothing here touches the database until run_lottery's own save, which
        runs after its four bucket checks pass. If the draw cannot be made,
        the previous state is restored in memory and the stored state was
        never modified at all.
        """
        self.require_request_state_digest(expected_digest)

        previous = copy.deepcopy(self.relay_data)
        self.relay_data = {
            'status': 'not_drawn',
            'teams': [],
            'eligible_college': [],
            'eligible_pro': [],
            'drawn_college': [],
            'drawn_pro': []
        }
        try:
            return self.run_lottery(
                num_teams=num_teams,
                expected_digest=expected_digest,
            )
        except Exception:
            # Also covers a StaleDataError out of run_lottery's commit, where
            # the caller rolls back the session but this object would
            # otherwise keep serving the half-built draw to whatever holds it.
            self.relay_data = previous
            raise

    def get_teams(self) -> list:
        """Get the current teams."""
        return self.relay_data.get('teams', [])

    def get_status(self) -> str:
        """Get the current lottery status."""
        return self.relay_data.get('status', 'not_drawn')

    @_relay_writer
    def record_total_time(
        self,
        team_number: int,
        total_time: float,
        expected_digest: str | None = None,
    ):
        """
        Record the total relay time for a team directly.

        Args:
            team_number: Team number
            total_time: Total relay time in seconds
        """
        self.require_request_state_digest(
            expected_digest,
            team_number=team_number,
        )

        teams = self.relay_data.get('teams', [])
        total_time = self._validated_relay_time(total_time)
        team = next((team for team in teams if team['team_number'] == team_number), None)
        if team is None:
            raise ValueError(f'Unknown relay team: {team_number}')

        team['total_time'] = total_time
        self.relay_data['status'] = 'in_progress'

        # Mark completed when all teams have a total time
        if teams and all(t.get('total_time') is not None for t in teams):
            self.relay_data['status'] = 'completed'

        self._save_relay_data()

    @_relay_writer
    def record_event_result(
        self,
        team_number: int,
        event_name: str,
        time_seconds: float,
        expected_digest: str | None = None,
    ):
        """
        Record a result for a team's event.

        Args:
            team_number: Team number (1 or 2)
            event_name: One of the configured relay event keys
            time_seconds: Time in seconds
        """
        self.require_request_state_digest(
            expected_digest,
            team_number=team_number,
        )

        teams = self.relay_data.get('teams', [])
        if event_name not in self.RELAY_EVENTS:
            raise ValueError(f'Unknown relay event: {event_name}')
        time_seconds = self._validated_relay_time(time_seconds)
        team_found = False

        for team in teams:
            if team['team_number'] == team_number:
                team_found = True
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

        if not team_found:
            raise ValueError(f'Unknown relay team: {team_number}')

        # Check if relay is complete
        all_teams_complete = all(
            all(evt['status'] == 'completed' for evt in t['events'].values())
            for t in teams
        )

        if all_teams_complete:
            self.relay_data['status'] = 'completed'

        self._save_relay_data()

    @staticmethod
    def _validated_relay_time(value: float) -> float:
        """Normalize a finite, non-negative relay time before mutation."""
        try:
            parsed = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError('Relay time must be a non-negative number') from exc
        if not math.isfinite(parsed) or parsed < 0:
            raise ValueError('Relay time must be a finite, non-negative number')
        return parsed

    def get_results(self) -> list:
        """Get relay results sorted by total time."""
        teams = self.relay_data.get('teams', [])
        completed = [t for t in teams if t.get('total_time') is not None]
        return sorted(completed, key=lambda t: t['total_time'])

    @_relay_writer
    def set_teams_manually(
        self,
        team_assignments: list[dict],
        expected_digest: str | None = None,
    ) -> dict:
        """
        Set relay teams manually instead of using the lottery.

        Args:
            team_assignments: list of dicts, each with:
                {
                    'pro_member_ids': [int, ...],
                    'college_member_ids': [int, ...],
                }

        Returns:
            Dict with result message.
        """
        self.require_request_state_digest(expected_digest)
        if not team_assignments:
            raise ValueError("At least one team is required.")

        teams = []
        all_pro_ids: set[int] = set()
        all_college_ids: set[int] = set()

        # Pre-pass: collect every pro/college ID across all teams, then resolve
        # them in TWO bulk queries instead of one query per ID. The previous
        # per-id loop issued len(teams) * 8 queries for the typical 2-team
        # 4-pro+4-college lottery (~16 queries) — small absolute count but
        # the manual-team-builder UX path runs interactively.
        for assignment in team_assignments:
            for pid in assignment.get('pro_member_ids', []):
                all_pro_ids.add(pid)
            for cid in assignment.get('college_member_ids', []):
                all_college_ids.add(cid)

        pro_lookup: dict[int, ProCompetitor] = {}
        if all_pro_ids:
            for comp in ProCompetitor.query.filter(
                ProCompetitor.id.in_(all_pro_ids),
                ProCompetitor.tournament_id == self.tournament.id,
                ProCompetitor.status == 'active',
                ProCompetitor.pro_am_lottery_opt_in.is_(True),
            ).all():
                pro_lookup[comp.id] = comp

        college_lookup: dict[int, CollegeCompetitor] = {}
        if all_college_ids:
            for comp in CollegeCompetitor.query.filter(
                CollegeCompetitor.id.in_(all_college_ids),
                CollegeCompetitor.tournament_id == self.tournament.id,
                CollegeCompetitor.status == 'active',
            ).all():
                college_lookup[comp.id] = comp

        # Re-validate dup-across-teams using the bulk-resolved set; same error
        # surface as the original per-id loop, just rolled into one pass.
        seen_pro: set[int] = set()
        seen_college: set[int] = set()

        for idx, assignment in enumerate(team_assignments, start=1):
            pro_ids = assignment.get('pro_member_ids', [])
            college_ids = assignment.get('college_member_ids', [])

            for pid in pro_ids:
                if pid in seen_pro:
                    raise ValueError(f"Pro competitor {pid} is assigned to multiple teams.")
                seen_pro.add(pid)
            for cid in college_ids:
                if cid in seen_college:
                    raise ValueError(f"College competitor {cid} is assigned to multiple teams.")
                seen_college.add(cid)

            pro_members = []
            for pid in pro_ids:
                comp = pro_lookup.get(pid)
                if not comp:
                    raise ValueError(
                        f"Pro competitor ID {pid} not found, not active, "
                        f"or not opted into Pro-Am Relay."
                    )
                pro_members.append({'id': comp.id, 'name': comp.name, 'gender': comp.gender})

            college_members = []
            for cid in college_ids:
                comp = college_lookup.get(cid)
                if not comp:
                    raise ValueError(
                        f"College competitor ID {cid} not found in this "
                        f"tournament or not active."
                    )
                if not getattr(comp, 'pro_am_lottery_opt_in', False):
                    raise ValueError(
                        f"College competitor {comp.name} is not opted into Pro-Am Relay."
                    )
                college_members.append({
                    'id': comp.id, 'name': comp.name, 'gender': comp.gender,
                    'team': comp.team.team_code if comp.team else 'N/A',
                })

            team = {
                'team_number': idx,
                'name': f'Team {idx}',
                'pro_members': pro_members,
                'college_members': college_members,
                'events': {
                    'partnered_sawing':       {'status': 'pending', 'result': None},
                    'standing_butcher_block':  {'status': 'pending', 'result': None},
                    'underhand_butcher_block': {'status': 'pending', 'result': None},
                    'team_axe_throw':          {'status': 'pending', 'result': None},
                },
                'total_time': None,
            }
            teams.append(team)

        # Store results.
        self.relay_data['status'] = 'drawn'
        self.relay_data['teams'] = teams
        self.relay_data['eligible_pro'] = self.get_eligible_pro_competitors()
        self.relay_data['eligible_college'] = self.get_eligible_college_competitors()
        self.relay_data['drawn_pro'] = [m for t in teams for m in t['pro_members']]
        self.relay_data['drawn_college'] = [m for t in teams for m in t['college_members']]

        self._save_relay_data()

        return {
            'success': True,
            'teams': teams,
            'message': f'Manually set {len(teams)} team(s).',
        }

    @_relay_writer
    def replace_competitor(
        self,
        team_number: int,
        old_competitor_id: int,
        new_competitor_id: int,
        competitor_type: str,
        expected_digest: str | None = None,
    ):
        """
        Replace a competitor on a team (e.g., due to injury).

        Args:
            team_number: Team number
            old_competitor_id: ID of competitor to replace
            new_competitor_id: ID of replacement competitor
            competitor_type: 'pro' or 'college'
        """
        self.require_request_state_digest(
            expected_digest,
            team_number=team_number,
        )

        teams = self.relay_data.get('teams', [])

        # Get new competitor info. Filter on tournament_id + active status +
        # pro_am_lottery_opt_in so a tampered POST cannot swap in a
        # competitor from another tournament, a scratched athlete, or
        # someone who never opted into the relay.
        if competitor_type == 'pro':
            new_comp = ProCompetitor.query.filter_by(
                id=new_competitor_id,
                tournament_id=self.tournament.id,
                status='active',
                pro_am_lottery_opt_in=True,
            ).first()
            member_key = 'pro_members'
        else:
            new_comp = CollegeCompetitor.query.filter_by(
                id=new_competitor_id,
                tournament_id=self.tournament.id,
                status='active',
            ).first()
            member_key = 'college_members'
            if new_comp is not None and not getattr(new_comp, 'pro_am_lottery_opt_in', False):
                raise ValueError(
                    "Replacement college competitor is not opted into the Pro-Am Relay"
                )

        if not new_comp:
            raise ValueError(
                "Replacement competitor not found in this tournament or not eligible"
            )

        new_comp_data = {
            'id': new_comp.id,
            'name': new_comp.name,
            'gender': new_comp.gender
        }

        affected_team = None
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
                        for existing_team in teams:
                            for existing_member in existing_team.get(member_key, []):
                                if (
                                    existing_member.get('id') == new_competitor_id
                                    and existing_member is not member
                                ):
                                    raise ValueError(
                                        "Replacement competitor is already assigned to a relay team"
                                    )
                        team[member_key][i] = new_comp_data
                        affected_team = team
                        break

        self._save_relay_data()

        # Compute and return health — warn if red but always allow (judge may have no choice)
        if affected_team is not None:
            health = compute_team_health(affected_team, self.tournament)
            return {'health': health}
        return {'health': {'status': 'green', 'detail': 'No team found for given team_number'}}


def compute_team_health(team_data: dict, tournament) -> dict:
    """
    Compute health status for a relay team.

    Args:
        team_data: dict with pro_members and college_members lists
        tournament: Tournament instance (unused directly; reserved for future
                    multi-tournament lookups)

    Returns:
        dict with 'status': 'green'|'yellow'|'red', 'detail': str

    Rules:
        Green: All 8 members have Competitor.status == 'active'
        Yellow: 1-2 members scratched/inactive but team meets minimum:
                at least 3 active per division (pro/college) with
                at least 1M and 1F in each division
        Red: Below minimum threshold or gender/division balance broken
    """
    pro_members = team_data.get('pro_members', [])
    college_members = team_data.get('college_members', [])

    inactive_names = []

    def _member_status(member, model_cls):
        obj = db.session.get(model_cls, member['id'])
        if obj is None:
            return 'unknown'
        return obj.status

    # Tally active counts per gender per division
    pro_active_m = 0
    pro_active_f = 0
    for m in pro_members:
        st = _member_status(m, ProCompetitor)
        if st == 'active':
            if m.get('gender') == 'M':
                pro_active_m += 1
            else:
                pro_active_f += 1
        else:
            inactive_names.append(m.get('name', str(m.get('id'))))

    col_active_m = 0
    col_active_f = 0
    for m in college_members:
        st = _member_status(m, CollegeCompetitor)
        if st == 'active':
            if m.get('gender') == 'M':
                col_active_m += 1
            else:
                col_active_f += 1
        else:
            inactive_names.append(m.get('name', str(m.get('id'))))

    total_members = len(pro_members) + len(college_members)
    inactive_count = len(inactive_names)

    pro_active_total = pro_active_m + pro_active_f
    col_active_total = col_active_m + col_active_f

    # Determine status
    if inactive_count == 0:
        return {'status': 'green', 'detail': 'Full roster active'}

    # Check minimums: each division needs >= 3 active with >= 1M and >= 1F
    pro_ok = pro_active_total >= 3 and pro_active_m >= 1 and pro_active_f >= 1
    col_ok = col_active_total >= 3 and col_active_m >= 1 and col_active_f >= 1
    within_yellow_limit = inactive_count <= 2

    if pro_ok and col_ok and within_yellow_limit:
        names_str = ', '.join(inactive_names)
        return {
            'status': 'yellow',
            'detail': f'{inactive_count} inactive member(s): {names_str}',
        }

    # Build red detail
    reasons = []
    if not pro_ok:
        reasons.append(
            f'Pro division below minimum (active: {pro_active_m}M/{pro_active_f}F)'
        )
    if not col_ok:
        reasons.append(
            f'College division below minimum (active: {col_active_m}M/{col_active_f}F)'
        )
    if inactive_count > 2:
        reasons.append(f'{inactive_count} members inactive')
    detail = '; '.join(reasons) if reasons else f'{inactive_count} inactive members'
    return {'status': 'red', 'detail': detail}


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
