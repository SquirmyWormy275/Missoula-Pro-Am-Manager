"""
On-demand demo data generation and cleanup.

Creates a fully populated synthetic tournament using the test fixture data
(25 pro competitors, 55 college competitors, 7 teams, all events scored).
Demo tournaments are tagged with the DEMO_PREFIX so they can be cleanly removed.
"""
import json
import logging
from datetime import date

from flask import Blueprint, flash, redirect, url_for, request
from database import db
from models.tournament import Tournament
from models.team import Team
from models.competitor import CollegeCompetitor, ProCompetitor
from models.event import Event, EventResult

logger = logging.getLogger(__name__)

demo_bp = Blueprint('demo', __name__)

DEMO_PREFIX = '[DEMO] '


# ---------------------------------------------------------------------------
# Synthetic data (inlined from tests/fixtures/synthetic_data.py)
# ---------------------------------------------------------------------------

def _load_synthetic():
    """Import synthetic data from the test fixtures module."""
    import importlib
    import sys
    import os

    # Ensure the tests directory is importable
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    tests_dir = os.path.join(project_root, 'tests')
    if tests_dir not in sys.path:
        sys.path.insert(0, tests_dir)

    # Force reimport to get fresh data
    mod_name = 'fixtures.synthetic_data'
    if mod_name in sys.modules:
        del sys.modules[mod_name]

    mod = importlib.import_module(mod_name)
    return mod


def _map_event_name_to_config(event_name, event_type, gender):
    """Determine scoring_type, scoring_order, stand_type, and flags from the synthetic event name."""
    import config as cfg

    # Try to match against known event lists
    all_events = []
    if event_type == 'college':
        for e in cfg.COLLEGE_OPEN_EVENTS + cfg.COLLEGE_CLOSED_EVENTS:
            all_events.append(e)
    else:
        for e in cfg.PRO_EVENTS:
            all_events.append(e)

    # Direct name match (strip gender prefix for pro events)
    base_name = event_name
    for prefix in ("Men's ", "Women's ", "Int "):
        if base_name.startswith(prefix):
            base_name = base_name[len(prefix):]
            break

    for e in all_events:
        if e['name'] == base_name or e['name'] == event_name:
            return {
                'scoring_type': e.get('scoring_type', 'time'),
                'scoring_order': 'highest_wins' if e.get('scoring_type') in ('score', 'distance') else 'lowest_wins',
                'stand_type': e.get('stand_type'),
                'max_stands': cfg.STAND_CONFIGS.get(e.get('stand_type', ''), {}).get('total', None),
                'is_partnered': e.get('is_partnered', False),
                'partner_gender_requirement': e.get('partner_gender', None),
                'requires_dual_runs': e.get('requires_dual_runs', False),
                'requires_triple_runs': e.get('requires_triple_runs', False),
                'is_open': e in cfg.COLLEGE_OPEN_EVENTS if event_type == 'college' else False,
                'has_prelims': e.get('has_prelims', False),
            }

    # Fallback for synthetic names that don't directly match config
    defaults = {
        'scoring_type': 'time', 'scoring_order': 'lowest_wins',
        'stand_type': None, 'max_stands': None, 'is_partnered': False,
        'partner_gender_requirement': None, 'requires_dual_runs': False,
        'requires_triple_runs': False, 'is_open': False, 'has_prelims': False,
    }
    return defaults


# ---------------------------------------------------------------------------
# Generate
# ---------------------------------------------------------------------------

@demo_bp.route('/generate', methods=['POST'])
def generate():
    """Create a full demo tournament with all synthetic data."""
    syn = _load_synthetic()

    # Prevent duplicate demo tournaments
    existing = Tournament.query.filter(Tournament.name.like(f'{DEMO_PREFIX}%')).first()
    if existing:
        flash('A demo tournament already exists. Clear it first.', 'warning')
        return redirect(request.referrer or url_for('main.judge_dashboard'))

    try:
        # --- Tournament ---
        tournament = Tournament(
            name=f'{DEMO_PREFIX}Missoula Pro-Am 2026',
            year=2026,
            college_date=date(2026, 4, 17),
            pro_date=date(2026, 4, 18),
            friday_feature_date=date(2026, 4, 17),
            status='setup',
            providing_shirts=True,
        )
        db.session.add(tournament)
        db.session.flush()  # get tournament.id

        tid = tournament.id

        # --- College Teams & Competitors ---
        team_map = {}  # team_code -> Team
        college_comp_map = {}  # name -> CollegeCompetitor

        for team_code, team_data in syn.COLLEGE_TEAMS.items():
            team = Team(
                tournament_id=tid,
                team_code=team_code,
                school_name=team_data['school'],
                school_abbreviation=team_data['abbrev'],
                status='active',
            )
            db.session.add(team)
            db.session.flush()
            team_map[team_code] = team

            for member in team_data['members']:
                comp = CollegeCompetitor(
                    tournament_id=tid,
                    team_id=team.id,
                    name=member['name'],
                    gender=member['gender'],
                    status='active',
                )
                db.session.add(comp)
                db.session.flush()
                college_comp_map[member['name']] = comp

        # --- Pro Competitors ---
        pro_comp_map = {}  # name -> ProCompetitor

        for p in syn.PRO_COMPETITORS:
            comp = ProCompetitor(
                tournament_id=tid,
                name=p['name'],
                gender=p['gender'],
                email=p.get('email', ''),
                is_ala_member=p.get('is_ala_member', False),
                pro_am_lottery_opt_in=p.get('lottery', False),
                gear_sharing_details=p.get('gear_sharing_text', ''),
                status='active',
            )
            db.session.add(comp)
            db.session.flush()
            pro_comp_map[p['name']] = comp

        # --- College Events & Scores ---
        college_event_map = {}  # event_key -> Event

        for event_key, event_data in syn.COLLEGE_SCORES.items():
            scoring_type = event_data.get('scoring_type', 'time')
            scoring_order = event_data.get('scoring_order', 'lowest_wins')
            gender = event_data.get('gender')
            stand_type = event_data.get('stand_type')
            is_partnered = event_data.get('is_partnered', False)
            requires_dual_runs = event_data.get('requires_dual_runs', False)

            import config as cfg
            max_stands = cfg.STAND_CONFIGS.get(stand_type, {}).get('total', None)

            event = Event(
                tournament_id=tid,
                name=event_key,
                event_type='college',
                gender=gender,
                scoring_type=scoring_type,
                scoring_order=scoring_order,
                stand_type=stand_type,
                max_stands=max_stands,
                is_partnered=is_partnered,
                requires_dual_runs=requires_dual_runs,
                status='completed',
                is_finalized=True,
            )
            db.session.add(event)
            db.session.flush()
            college_event_map[event_key] = event

            # Create results
            for row in event_data['results']:
                comp_name = row[0]
                team_code = row[1]
                result_value = row[2]
                position = row[3]
                points = row[4]
                partner_name = row[5] if len(row) > 5 else None

                comp = college_comp_map.get(comp_name)
                if not comp:
                    continue

                status = 'completed' if result_value is not None else 'dnf'

                er = EventResult(
                    event_id=event.id,
                    competitor_id=comp.id,
                    competitor_type='college',
                    competitor_name=comp_name,
                    partner_name=partner_name,
                    result_value=result_value if result_value is not None else 0.0,
                    result_unit='seconds' if scoring_type == 'time' else ('hits' if scoring_type == 'hits' else 'points'),
                    final_position=position,
                    points_awarded=points,
                    status=status,
                )
                db.session.add(er)

                # Track events entered on the competitor
                events_list = comp.get_events_entered()
                if event.id not in events_list:
                    events_list.append(event.id)
                    comp.set_events_entered(events_list)

                # Award points
                if points and points > 0:
                    comp.individual_points = (comp.individual_points or 0) + points

        db.session.flush()

        # Recalculate team totals
        for team in team_map.values():
            total = sum(
                (m.individual_points or 0)
                for m in CollegeCompetitor.query.filter_by(team_id=team.id).all()
            )
            team.total_points = total

        # --- Pro Events & Scores ---
        # First, create all pro events from the PRO_SCORES keys
        pro_event_map = {}  # event_name -> Event

        for event_name, results in syn.PRO_SCORES.items():
            # Determine gender from event name
            gender = None
            if event_name.startswith("Men's") or event_name.startswith("Men's"):
                gender = 'M'
            elif event_name.startswith("Women's") or event_name.startswith("Women's"):
                gender = 'F'

            event_cfg = _map_event_name_to_config(event_name, 'pro', gender)

            event = Event(
                tournament_id=tid,
                name=event_name,
                event_type='pro',
                gender=gender,
                scoring_type=event_cfg['scoring_type'],
                scoring_order=event_cfg['scoring_order'],
                stand_type=event_cfg['stand_type'],
                max_stands=event_cfg['max_stands'],
                is_partnered=event_cfg['is_partnered'],
                requires_dual_runs=event_cfg['requires_dual_runs'],
                requires_triple_runs=event_cfg['requires_triple_runs'],
                has_prelims=event_cfg['has_prelims'],
                status='completed',
                is_finalized=True,
            )
            db.session.add(event)
            db.session.flush()
            pro_event_map[event_name] = event

            # Create results
            for i, row in enumerate(results):
                comp_name = row[0]
                result_value = row[1]
                result_status = row[2]
                partner_name = row[3] if len(row) > 3 else None

                comp = pro_comp_map.get(comp_name)
                if not comp:
                    continue

                status = 'completed' if result_status == 'completed' else 'dnf'
                position = (i + 1) if result_status == 'completed' else None

                er = EventResult(
                    event_id=event.id,
                    competitor_id=comp.id,
                    competitor_type='pro',
                    competitor_name=comp_name,
                    partner_name=partner_name,
                    result_value=result_value if result_value is not None else 0.0,
                    result_unit='seconds' if event_cfg['scoring_type'] == 'time' else 'points',
                    final_position=position,
                    status=status,
                )
                db.session.add(er)

                # Track events entered
                events_list = comp.get_events_entered()
                if event.id not in events_list:
                    events_list.append(event.id)
                    comp.set_events_entered(events_list)

        # --- Set partners on pro competitors ---
        for p in syn.PRO_COMPETITORS:
            comp = pro_comp_map.get(p['name'])
            if not comp or 'partners' not in p:
                continue
            partners_dict = {}
            for event_name, partner_name in p['partners'].items():
                event = pro_event_map.get(event_name)
                if event:
                    partners_dict[str(event.id)] = partner_name
            if partners_dict:
                comp.partners = json.dumps(partners_dict)

        db.session.commit()

        n_college = CollegeCompetitor.query.filter_by(tournament_id=tid).count()
        n_pro = ProCompetitor.query.filter_by(tournament_id=tid).count()
        n_events = Event.query.filter_by(tournament_id=tid).count()
        n_results = db.session.query(EventResult).join(Event).filter(Event.tournament_id == tid).count()

        flash(
            f'Demo tournament created: {n_college} college + {n_pro} pro competitors, '
            f'{n_events} events, {n_results} results.',
            'success'
        )
        logger.info('Demo tournament created: id=%s, %d events, %d results', tid, n_events, n_results)

    except Exception as e:
        db.session.rollback()
        logger.exception('Failed to generate demo data')
        flash(f'Failed to generate demo data: {e}', 'error')

    return redirect(request.referrer or url_for('main.judge_dashboard'))


# ---------------------------------------------------------------------------
# Clear
# ---------------------------------------------------------------------------

@demo_bp.route('/clear', methods=['POST'])
def clear():
    """Remove all demo tournaments and their cascaded data."""
    try:
        demos = Tournament.query.filter(Tournament.name.like(f'{DEMO_PREFIX}%')).all()
        if not demos:
            flash('No demo tournaments found.', 'info')
            return redirect(request.referrer or url_for('main.judge_dashboard'))

        count = 0
        for tournament in demos:
            tid = tournament.id

            # Delete in dependency order to avoid FK issues
            # EventResults via events
            event_ids = [e.id for e in Event.query.filter_by(tournament_id=tid).all()]
            if event_ids:
                EventResult.query.filter(EventResult.event_id.in_(event_ids)).delete(synchronize_session=False)

            # Heats and HeatAssignments
            from models.heat import Heat, HeatAssignment, Flight
            heat_ids = [h.id for h in Heat.query.filter(Heat.event_id.in_(event_ids)).all()] if event_ids else []
            if heat_ids:
                HeatAssignment.query.filter(HeatAssignment.heat_id.in_(heat_ids)).delete(synchronize_session=False)
                Heat.query.filter(Heat.id.in_(heat_ids)).delete(synchronize_session=False)

            # Flights
            Flight.query.filter_by(tournament_id=tid).delete(synchronize_session=False)

            # Events
            Event.query.filter_by(tournament_id=tid).delete(synchronize_session=False)

            # Competitors
            CollegeCompetitor.query.filter_by(tournament_id=tid).delete(synchronize_session=False)
            ProCompetitor.query.filter_by(tournament_id=tid).delete(synchronize_session=False)

            # ProEventRanks
            from models.pro_event_rank import ProEventRank
            ProEventRank.query.filter_by(tournament_id=tid).delete(synchronize_session=False)

            # SchoolCaptains
            from models.school_captain import SchoolCaptain
            SchoolCaptain.query.filter_by(tournament_id=tid).delete(synchronize_session=False)

            # WoodConfigs
            from models.wood_config import WoodConfig
            WoodConfig.query.filter_by(tournament_id=tid).delete(synchronize_session=False)

            # Teams
            Team.query.filter_by(tournament_id=tid).delete(synchronize_session=False)

            # Audit logs referencing this tournament (best-effort)
            try:
                from models.audit_log import AuditLog
                AuditLog.query.filter(
                    AuditLog.entity_type == 'tournament',
                    AuditLog.entity_id == tid
                ).delete(synchronize_session=False)
            except Exception:
                pass

            # Tournament itself
            db.session.delete(tournament)
            count += 1

        db.session.commit()
        flash(f'Cleared {count} demo tournament(s) and all associated data.', 'success')
        logger.info('Cleared %d demo tournament(s)', count)

    except Exception as e:
        db.session.rollback()
        logger.exception('Failed to clear demo data')
        flash(f'Failed to clear demo data: {e}', 'error')

    return redirect(request.referrer or url_for('main.judge_dashboard'))
