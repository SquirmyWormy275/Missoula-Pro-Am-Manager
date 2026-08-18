"""Application services for preflight repair and full-show generation."""
from __future__ import annotations

import logging
import math
from collections.abc import Mapping

from sqlalchemy.orm.exc import NoResultFound

from database import db
from models import Event, EventResult, Flight, Heat, Tournament
from services.flight_builder import (
    lock_tournament_schedule,
    serialize_sqlite_schedule_writer,
)

logger = logging.getLogger(__name__)


FLIGHT_SIZING_MODE_MINUTES = 'minutes'
FLIGHT_SIZING_MODE_COUNT = 'count'
VALID_FLIGHT_SIZING_MODES = {
    FLIGHT_SIZING_MODE_MINUTES,
    FLIGHT_SIZING_MODE_COUNT,
}
FLIGHT_SIZING_DEFAULTS = {
    'mode': FLIGHT_SIZING_MODE_MINUTES,
    'target_minutes_per_flight': 60,
    'minutes_per_heat': 5.5,
    'num_flights': 4,
}
FLIGHT_COUNT_MIN = 2
FLIGHT_COUNT_MAX = 10
MINUTES_PER_FLIGHT_MIN = 30
MINUTES_PER_FLIGHT_MAX = 180
MINUTES_PER_HEAT_MIN = 1.0
MINUTES_PER_HEAT_MAX = 15.0


class ScheduleBuildError(RuntimeError):
    """Operator-safe full-show failure with a stable machine-readable code."""

    def __init__(
        self,
        code: str,
        phase: str,
        message: str,
        *,
        details: list[dict] | tuple[dict, ...] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.phase = phase
        self.details = tuple(details or ())

    def to_summary(self) -> dict:
        return {
            'ok': False,
            'code': self.code,
            'phase': self.phase,
            'message': str(self),
            'details': list(self.details),
        }


class ScheduleReadinessError(ScheduleBuildError):
    """Readiness gate failure carrying issue codes and repair details."""

    def __init__(self, phase: str, issues: list[dict]) -> None:
        issue_codes = tuple(dict.fromkeys(
            str(issue.get('code') or 'unknown_readiness_issue')
            for issue in issues
        ))
        timing = 'before generation' if phase == 'pre_generation' else 'after generation'
        shown = []
        for issue in issues[:3]:
            code = str(issue.get('code') or 'unknown_readiness_issue')
            detail = ' '.join(str(
                issue.get('detail') or issue.get('title') or 'Repair required.'
            ).split())
            shown.append(f'{code}: {detail[:240]}')
        suffix = f' (+{len(issues) - 3} more)' if len(issues) > 3 else ''
        message = (
            f'Schedule build blocked {timing} by Preflight issue(s): '
            f'{"; ".join(shown)}{suffix} Open Preflight, repair the listed '
            f'people or events, then run Generate again.'
        )
        super().__init__(
            'schedule_readiness_blocked',
            phase,
            message,
            details=[_readiness_issue_detail(issue) for issue in issues],
        )
        self.issues = tuple(issues)
        self.issue_codes = issue_codes

    def to_summary(self) -> dict:
        summary = super().to_summary()
        summary['issue_codes'] = list(self.issue_codes)
        return summary


def _readiness_issue_detail(issue: dict) -> dict:
    return {
        'code': str(issue.get('code') or 'unknown_readiness_issue'),
        'title': str(issue.get('title') or 'Schedule readiness issue'),
        'detail': str(issue.get('detail') or ''),
        'event_id': issue.get('event_id'),
        'event_ids': list(issue.get('event_ids') or []),
    }


def read_flight_sizing_config(tournament: Tournament) -> dict:
    """Return saved flight-sizing config merged over operator-safe defaults."""
    cfg = tournament.get_schedule_config() or {}
    mode = cfg.get('flight_sizing_mode', FLIGHT_SIZING_DEFAULTS['mode'])
    if mode not in VALID_FLIGHT_SIZING_MODES:
        mode = FLIGHT_SIZING_DEFAULTS['mode']
    try:
        target_minutes = int(cfg.get(
            'target_minutes_per_flight',
            FLIGHT_SIZING_DEFAULTS['target_minutes_per_flight'],
        ))
    except (TypeError, ValueError):
        target_minutes = FLIGHT_SIZING_DEFAULTS['target_minutes_per_flight']
    try:
        minutes_per_heat = float(cfg.get(
            'minutes_per_heat',
            FLIGHT_SIZING_DEFAULTS['minutes_per_heat'],
        ))
    except (TypeError, ValueError):
        minutes_per_heat = FLIGHT_SIZING_DEFAULTS['minutes_per_heat']
    try:
        saved_num_flights = int(cfg.get(
            'num_flights', FLIGHT_SIZING_DEFAULTS['num_flights'],
        ))
    except (TypeError, ValueError):
        saved_num_flights = FLIGHT_SIZING_DEFAULTS['num_flights']
    return {
        'mode': mode,
        'target_minutes_per_flight': max(
            MINUTES_PER_FLIGHT_MIN,
            min(MINUTES_PER_FLIGHT_MAX, target_minutes),
        ),
        'minutes_per_heat': max(
            MINUTES_PER_HEAT_MIN,
            min(MINUTES_PER_HEAT_MAX, minutes_per_heat),
        ),
        'num_flights': max(
            FLIGHT_COUNT_MIN,
            min(FLIGHT_COUNT_MAX, saved_num_flights),
        ),
    }


def normalize_flight_sizing_input(values: Mapping) -> dict | None:
    """Validate Run Show sizing fields without mutating schedule state."""
    raw_mode = str(values.get('flight_sizing_mode') or '').strip().lower()
    if not raw_mode:
        return None
    if raw_mode not in VALID_FLIGHT_SIZING_MODES:
        raw_mode = FLIGHT_SIZING_DEFAULTS['mode']
    try:
        target_minutes = int(values.get(
            'target_minutes_per_flight',
            FLIGHT_SIZING_DEFAULTS['target_minutes_per_flight'],
        ))
    except (TypeError, ValueError):
        target_minutes = FLIGHT_SIZING_DEFAULTS['target_minutes_per_flight']
    target_minutes = max(
        MINUTES_PER_FLIGHT_MIN,
        min(MINUTES_PER_FLIGHT_MAX, target_minutes),
    )
    try:
        minutes_per_heat = float(values.get(
            'minutes_per_heat', FLIGHT_SIZING_DEFAULTS['minutes_per_heat'],
        ))
    except (TypeError, ValueError):
        minutes_per_heat = FLIGHT_SIZING_DEFAULTS['minutes_per_heat']
    minutes_per_heat = max(
        MINUTES_PER_HEAT_MIN,
        min(MINUTES_PER_HEAT_MAX, minutes_per_heat),
    )
    try:
        requested_num_flights = int(values.get('num_flights', 0))
    except (TypeError, ValueError):
        requested_num_flights = 0
    has_requested_count = requested_num_flights >= 1
    normalized_num_flights = (
        max(
            FLIGHT_COUNT_MIN,
            min(FLIGHT_COUNT_MAX, requested_num_flights),
        )
        if has_requested_count
        else FLIGHT_SIZING_DEFAULTS['num_flights']
    )
    return {
        'mode': raw_mode,
        'target_minutes_per_flight': target_minutes,
        'minutes_per_heat': minutes_per_heat,
        'num_flights': normalized_num_flights,
        'requested_num_flights': (
            normalized_num_flights if has_requested_count else None
        ),
    }


def persist_flight_sizing_config(tournament: Tournament, sizing: Mapping) -> None:
    """Persist validated operator sizing inside the caller's transaction."""
    cfg = tournament.get_schedule_config() or {}
    cfg['flight_sizing_mode'] = sizing['mode']
    cfg['target_minutes_per_flight'] = int(
        sizing['target_minutes_per_flight']
    )
    cfg['minutes_per_heat'] = float(sizing['minutes_per_heat'])
    cfg['num_flights'] = int(sizing['num_flights'])
    tournament.set_schedule_config(cfg)


def compute_num_flights_from_duration(
    total_heats: int,
    minutes_per_heat: float,
    target_minutes_per_flight: int,
) -> tuple[int, bool]:
    """Derive and clamp a duration-based flight count."""
    if (
        total_heats <= 0
        or minutes_per_heat <= 0
        or target_minutes_per_flight <= 0
    ):
        return FLIGHT_COUNT_MIN, False
    ideal = math.ceil(
        (total_heats * minutes_per_heat) / target_minutes_per_flight
    )
    clamped = max(FLIGHT_COUNT_MIN, min(FLIGHT_COUNT_MAX, ideal))
    return clamped, clamped != ideal


def resolve_num_flights(
    tournament: Tournament,
    sizing: Mapping | None = None,
) -> int | None:
    """Resolve persisted or just-submitted sizing after heats are generated."""
    effective = dict(sizing or read_flight_sizing_config(tournament))
    if effective['mode'] == FLIGHT_SIZING_MODE_MINUTES:
        pro_heats = Heat.query.join(Event).filter(
            Event.tournament_id == tournament.id,
            Event.event_type == 'pro',
            Event.name != 'Partnered Axe Throw',
            Heat.run_number == 1,
        ).count()
        if pro_heats <= 0:
            return None
        computed, _ = compute_num_flights_from_duration(
            pro_heats,
            float(effective['minutes_per_heat']),
            int(effective['target_minutes_per_flight']),
        )
        return computed if computed >= 1 else None
    requested = effective.get('requested_num_flights')
    if requested is not None:
        return int(requested) if int(requested) >= 1 else None
    saved = int(effective['num_flights'])
    return saved if saved >= 1 else None


@serialize_sqlite_schedule_writer
def run_preflight_autofix(
    tournament: Tournament,
    saturday_ids: list[int] | None = None,
) -> dict:
    """Apply the one-click preflight autofix workflow and return a summary."""
    from services.flight_builder import (
        integrate_college_spillover_into_flights,
        integrate_proam_relay_into_final_flight,
    )
    from services.gear_sharing import complete_one_sided_pairs, parse_all_gear_details
    from services.partner_matching import auto_assign_partners

    lock_tournament_schedule(tournament)
    gear_parse_result = parse_all_gear_details(tournament)
    pairs_result = complete_one_sided_pairs(tournament)
    partner_summary = auto_assign_partners(tournament)
    relay_result = integrate_proam_relay_into_final_flight(
        tournament, commit=False,
    )
    integration = integrate_college_spillover_into_flights(
        tournament, saturday_ids or [], commit=False,
    )
    return {
        'gear_parsed': gear_parse_result,
        'gear_pairs_completed': pairs_result['completed'],
        'partner_summary': partner_summary,
        'spillover': integration,
        'relay': relay_result,
    }


def _protected_event_reason(event: Event) -> str | None:
    if event.is_finalized:
        return 'finalized'
    if event.status == 'completed':
        return 'completed'
    if event.has_prelims:
        from services.partnered_axe import partnered_axe_history_protection_reason

        reason = partnered_axe_history_protection_reason(event)
        if reason is not None:
            return reason
        if Heat.query.filter_by(
            event_id=event.id, status='completed',
        ).first() is not None:
            return 'completed heat history'
        return None
    if EventResult.query.filter_by(
        event_id=event.id, status='completed',
    ).first() is not None:
        return 'scored'
    if Heat.query.filter_by(
        event_id=event.id, status='completed',
    ).first() is not None:
        return 'completed heat history'
    return None


def _event_schedule_snapshot(event_id: int) -> tuple:
    """Return immutable heat, roster, stand, and flight state for one event."""
    heats = Heat.query.filter_by(event_id=event_id).order_by(
        Heat.run_number, Heat.heat_number, Heat.id,
    ).all()
    return tuple(
        (
            heat.id,
            heat.heat_number,
            heat.run_number,
            heat.status,
            heat.flight_id,
            heat.flight_position,
            tuple(heat.get_competitors()),
            tuple(sorted(heat.get_stand_assignments().items())),
        )
        for heat in heats
    )


def _saturday_event_ids(tournament: Tournament) -> list[int]:
    config = tournament.get_schedule_config() or {}
    ids = []
    for raw_id in config.get('saturday_college_event_ids', []):
        try:
            ids.append(int(raw_id))
        except (TypeError, ValueError):
            logger.warning(
                'Ignoring invalid saturday_college_event_id %r for tournament %s',
                raw_id,
                tournament.id,
            )
    return ids


def _flight_snapshot(tournament_id: int) -> dict:
    flights = Flight.query.filter_by(tournament_id=tournament_id).all()
    return {
        'flight_count': len(flights),
        'total_heats': sum(len(flight.get_heats_ordered()) for flight in flights),
    }


def _ensure_sqlite_write_transaction() -> None:
    """Make per-event savepoints subordinate to the full SQLite transaction.

    Python's sqlite driver can defer the physical BEGIN past SELECTs. Without
    an explicit BEGIN, the first ``begin_nested()`` savepoint may become the
    effective top-level transaction and RELEASE would make that event durable
    before post-build validation. PostgreSQL does not need this workaround.
    """
    connection = db.session.connection()
    if connection.dialect.name != 'sqlite':
        return
    proxied = connection.connection
    driver_connection = getattr(proxied, 'driver_connection', proxied)
    if not getattr(driver_connection, 'in_transaction', True):
        connection.exec_driver_sql('BEGIN IMMEDIATE')


def _phase_failure(phase: str, *, event: Event | None = None) -> ScheduleBuildError:
    labels = {
        'heat_generation': 'generating event heats',
        'flight_generation': 'building pro flights',
        'relay': 'placing the Pro-Am Relay',
        'spillover': 'integrating Saturday spillover',
        'saw_blocks': 'assigning saw blocks',
        'post_generation': 'validating the generated show',
        'commit': 'saving the generated show',
    }
    action = labels.get(phase, 'building the show schedule')
    affected = f' for {event.display_name}' if event is not None else ''
    return ScheduleBuildError(
        f'{phase}_failed',
        phase,
        f'Schedule build failed while {action}{affected} and was rolled back. '
        'Open Preflight to review the affected event, then run Generate again.',
        details=[{
            'event_id': event.id,
            'event_name': event.display_name,
        }] if event is not None else [],
    )


def schedule_operator_messages(summary: Mapping) -> list[dict]:
    """Build route/job-ready messages from a successful structured summary."""
    messages = []
    generated = int(summary.get('generated') or 0)
    if generated:
        messages.append({
            'category': 'success',
            'message': f'Heats generated for {generated} event(s).',
        })
    skipped = list(summary.get('skipped_events') or [])
    if skipped:
        names = ', '.join(item['event_name'] for item in skipped[:8])
        suffix = f' (+{len(skipped) - 8} more)' if len(skipped) > 8 else ''
        messages.append({
            'category': 'warning',
            'message': (
                f'{len(skipped)} event(s) had no entrants and were skipped: '
                f'{names}{suffix}. Add entries in Registration, then Generate again.'
            ),
        })
    protected = list(summary.get('protected') or [])
    if protected:
        names = ', '.join(
            f"{item['event_name']} ({item['reason']})"
            for item in protected[:8]
        )
        suffix = f' (+{len(protected) - 8} more)' if len(protected) > 8 else ''
        messages.append({
            'category': 'warning',
            'message': (
                f'{len(protected)} scored or finalized event(s) were left '
                f'unchanged: {names}{suffix}.'
            ),
        })
    flights = summary.get('flights')
    if flights is not None:
        messages.append({
            'category': 'success',
            'message': f'Built {flights} pro flight(s).',
        })
    relay = summary.get('relay') or {}
    if relay.get('placed'):
        messages.append({
            'category': 'success',
            'message': 'Pro-Am Relay placed in the final flight.',
        })
    return messages


@serialize_sqlite_schedule_writer
def generate_tournament_schedule_artifacts(
    tournament_id: int,
    *,
    flight_sizing: Mapping | None = None,
) -> dict:
    """Build all show artifacts under one lock and one transaction.

    Order is fixed: pre-generation readiness, heats, pro flights, relay,
    spillover, saw blocks, post-generation readiness, then one commit.
    """
    from services.flight_builder import (
        build_pro_flights,
        integrate_college_spillover_into_flights,
        integrate_proam_relay_into_final_flight,
    )
    from services.heat_generator import generate_event_heats
    from services.preflight import (
        build_preflight_report,
        get_post_generation_blocking_issues,
        get_pre_generation_blocking_issues,
    )
    from services.saw_block_assignment import assign_saw_blocks

    try:
        tournament = lock_tournament_schedule(tournament_id)
    except NoResultFound as exc:
        raise ScheduleBuildError(
            'tournament_not_found',
            'lock',
            f'Tournament {tournament_id} was not found; no schedule was changed.',
        ) from exc

    phase = 'pre_generation'
    event_in_progress = None
    try:
        _ensure_sqlite_write_transaction()
        saturday_ids = _saturday_event_ids(tournament)
        preflight_report = build_preflight_report(tournament, saturday_ids)
        blockers = get_pre_generation_blocking_issues(preflight_report)
        if blockers:
            raise ScheduleReadinessError('pre_generation', blockers)

        normalized_sizing = (
            normalize_flight_sizing_input(flight_sizing)
            if flight_sizing is not None
            and 'requested_num_flights' not in flight_sizing
            else dict(flight_sizing) if flight_sizing is not None else None
        )
        if normalized_sizing is not None:
            persist_flight_sizing_config(tournament, normalized_sizing)
            db.session.flush()

        before = _flight_snapshot(tournament.id)
        generated = 0
        skipped_events = []
        protected_events = []
        protected_snapshots = {}
        phase = 'heat_generation'
        events = tournament.events.order_by(
            Event.event_type, Event.name, Event.gender,
        ).all()
        for event in events:
            reason = _protected_event_reason(event)
            if reason is not None:
                protected_events.append({
                    'event_id': event.id,
                    'event_name': event.display_name,
                    'reason': reason,
                })
                protected_snapshots[event.id] = _event_schedule_snapshot(event.id)
                continue
            event_in_progress = event
            try:
                with db.session.begin_nested():
                    generate_event_heats(
                        event, allow_flight_replacement=True,
                    )
                generated += 1
            except Exception as exc:
                if 'No competitors entered' in str(exc):
                    skipped_events.append({
                        'event_id': event.id,
                        'event_name': event.display_name,
                        'event_type': event.event_type,
                    })
                    continue
                raise
        event_in_progress = None

        pro_heat_count = Heat.query.join(Event).filter(
            Event.tournament_id == tournament.id,
            Event.event_type == 'pro',
            Heat.run_number == 1,
        ).count()
        flights_built = None
        relay_result = {'placed': False, 'reason': 'no_pro_heats'}
        spillover_result = {
            'integrated_heats': 0,
            'reason': 'no_pro_heats',
        }
        if pro_heat_count:
            phase = 'flight_generation'
            num_flights = resolve_num_flights(tournament, normalized_sizing)
            flights_built = build_pro_flights(
                tournament,
                num_flights=num_flights,
                commit=False,
            )
            phase = 'relay'
            relay_result = integrate_proam_relay_into_final_flight(
                tournament, commit=False,
            )
            phase = 'spillover'
            spillover_result = integrate_college_spillover_into_flights(
                tournament,
                college_event_ids=saturday_ids,
                commit=False,
            )

        phase = 'saw_blocks'
        saw_block_result = assign_saw_blocks(tournament, commit=False)

        phase = 'post_generation'
        changed_protected = [
            item for item in protected_events
            if _event_schedule_snapshot(item['event_id'])
            != protected_snapshots[item['event_id']]
        ]
        if changed_protected:
            names = ', '.join(
                item['event_name'] for item in changed_protected[:5]
            )
            suffix = (
                f' (+{len(changed_protected) - 5} more)'
                if len(changed_protected) > 5 else ''
            )
            raise ScheduleBuildError(
                'protected_history_changed',
                'post_generation',
                'Schedule build attempted to change scored or finalized event '
                f'history and was rolled back: {names}{suffix}. Open Preflight '
                'and use an unscored tournament schedule for regeneration.',
                details=changed_protected,
            )
        postflight_report = build_preflight_report(tournament, saturday_ids)
        blockers = get_post_generation_blocking_issues(postflight_report)
        if blockers:
            raise ScheduleReadinessError('post_generation', blockers)

        after = _flight_snapshot(tournament.id)
        phase = 'commit'
        db.session.commit()
    except ScheduleBuildError:
        db.session.rollback()
        raise
    except Exception as exc:
        db.session.rollback()
        logger.exception(
            'Full-show generation failed tournament_id=%s phase=%s',
            tournament_id,
            phase,
        )
        raise _phase_failure(phase, event=event_in_progress) from exc

    summary = {
        'ok': True,
        'tournament_id': tournament.id,
        'generated': generated,
        'skipped': len(skipped_events),
        'skipped_events': skipped_events,
        'protected': protected_events,
        'errors': [],
        'flights': flights_built,
        'relay': dict(relay_result or {}),
        'relay_placed': bool((relay_result or {}).get('placed')),
        'spillover': dict(spillover_result or {}),
        'spillover_integrated': int(
            (spillover_result or {}).get('integrated_heats', 0)
        ),
        'saw_blocks': dict(saw_block_result or {}),
        'build_diff': {
            'before_flight_count': before['flight_count'],
            'after_flight_count': after['flight_count'],
            'total_heats': after['total_heats'],
        },
        'readiness': {
            'pre_generation_blockers': 0,
            'post_generation_blockers': 0,
        },
    }
    summary['operator_messages'] = schedule_operator_messages(summary)
    return summary
