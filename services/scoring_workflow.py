from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Mapping

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm.exc import StaleDataError

import services.scoring_engine as engine
import strings as text
from database import db
from models import Event, EventResult, Heat
from models.competitor import CollegeCompetitor, ProCompetitor
from services.audit import log_action
from services.cache_invalidation import invalidate_tournament_caches

logger = logging.getLogger(__name__)


def _normalize_competitor_ids(competitor_ids: list[object]) -> list[int]:
    normalized: list[int] = []
    for entry in competitor_ids:
        if isinstance(entry, dict):
            entry = entry.get('id')
        if entry in (None, ''):
            continue
        normalized.append(int(entry))
    return normalized


def competitor_lookup_for_event(event: Event, competitor_ids: list[int]) -> dict[int, object]:
    competitor_ids = _normalize_competitor_ids(competitor_ids)
    if event.event_type == 'college':
        comps = CollegeCompetitor.query.filter(CollegeCompetitor.id.in_(competitor_ids)).all()
    else:
        comps = ProCompetitor.query.filter(ProCompetitor.id.in_(competitor_ids)).all()
    return {c.id: c for c in comps}


def existing_results_for_event(event: Event, competitor_ids: list[int]) -> dict[int, EventResult]:
    competitor_ids = _normalize_competitor_ids(competitor_ids)
    rows = EventResult.query.filter(
        EventResult.event_id == event.id,
        EventResult.competitor_id.in_(competitor_ids),
        EventResult.competitor_type == event.event_type,
    ).all()
    return {r.competitor_id: r for r in rows}


def _form_int(form_data: Mapping[str, object], key: str) -> int | None:
    raw = form_data.get(key)
    if raw in (None, ''):
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _parse_dual_timer(
    form_data: Mapping[str, object],
    comp_id: int,
    run_suffix: str,
    invalid: list[tuple[int, object]],
) -> tuple[float | None, float | None, float | None]:
    raw_t1 = form_data.get(f't1_{run_suffix}_{comp_id}')
    raw_t2 = form_data.get(f't2_{run_suffix}_{comp_id}')

    def _try_parse(raw):
        if raw in (None, ''):
            return None
        try:
            val = float(raw)
        except (TypeError, ValueError):
            invalid.append((comp_id, raw))
            return None
        if val < 0:
            invalid.append((comp_id, raw))
            return None
        return val

    t1 = _try_parse(raw_t1)
    t2 = _try_parse(raw_t2)
    if t1 is not None and t2 is not None:
        return (t1, t2, (t1 + t2) / 2.0)
    return (t1, t2, None)


def save_heat_results_submission(
    *,
    tournament_id: int,
    heat: Heat,
    event: Event,
    form_data: Mapping[str, object],
    judge_user_id: int | None,
) -> dict:
    competitor_ids = _normalize_competitor_ids(heat.get_competitors())
    posted_version = _form_int(form_data, 'heat_version')
    if posted_version is None or posted_version != heat.version_id:
        return {
            'ok': False,
            'category': 'error',
            'message': 'This heat changed in another session. Reload and re-enter results.',
            'redirect_kind': 'heat_entry',
            'redirect_event_id': event.id,
            'redirect_heat_id': heat.id,
            'status_code': 409,
        }

    result_by_comp = existing_results_for_event(event, competitor_ids)
    comp_lookup = competitor_lookup_for_event(event, competitor_ids)
    changes = 0
    invalid: list[tuple[int, object]] = []
    is_dual_timer_event = (
        event.scoring_type in ('time', 'distance')
        and not event.requires_triple_runs
    )

    try:
        for comp_id in competitor_ids:
            status = form_data.get(f'status_{comp_id}', 'completed')

            if is_dual_timer_event:
                run_suffix = 'run2' if (event.requires_dual_runs and heat.run_number == 2) else 'run1'
                t1, t2, average = _parse_dual_timer(form_data, comp_id, run_suffix, invalid)
                if t1 is None and t2 is None:
                    continue

                result = result_by_comp.get(comp_id)
                if not result:
                    comp = comp_lookup.get(comp_id)
                    result = EventResult(
                        event_id=event.id,
                        competitor_id=comp_id,
                        competitor_type=event.event_type,
                        competitor_name=comp.display_name if comp else f'Unknown ({comp_id})',
                    )
                    db.session.add(result)
                    result_by_comp[comp_id] = result

                if run_suffix == 'run1':
                    result.t1_run1 = t1
                    result.t2_run1 = t2
                else:
                    result.t1_run2 = t1
                    result.t2_run2 = t2

                if average is not None:
                    if event.requires_dual_runs:
                        if heat.run_number == 1:
                            result.run1_value = average
                        else:
                            result.run2_value = average
                        result.calculate_best_run(event.scoring_order)
                    else:
                        result.result_value = average
                        result.run1_value = average
                elif status == 'completed':
                    status = 'partial'

            elif event.requires_triple_runs:
                raw = form_data.get(f'result_{comp_id}')
                if not raw:
                    continue
                try:
                    parsed = float(raw)
                except (TypeError, ValueError):
                    invalid.append((comp_id, raw))
                    continue
                if parsed < 0:
                    invalid.append((comp_id, raw))
                    continue

                result = result_by_comp.get(comp_id)
                if not result:
                    comp = comp_lookup.get(comp_id)
                    result = EventResult(
                        event_id=event.id,
                        competitor_id=comp_id,
                        competitor_type=event.event_type,
                        competitor_name=comp.display_name if comp else f'Unknown ({comp_id})',
                    )
                    db.session.add(result)
                    result_by_comp[comp_id] = result

                run_slot = form_data.get(f'run_slot_{comp_id}', '1')
                if run_slot == '2':
                    result.run2_value = parsed
                elif run_slot == '3':
                    result.run3_value = parsed
                else:
                    result.run1_value = parsed

                for slot, field in [('2', f'result2_{comp_id}'), ('3', f'result3_{comp_id}')]:
                    raw2 = form_data.get(field)
                    if not raw2:
                        continue
                    try:
                        v = float(raw2)
                    except (TypeError, ValueError):
                        continue
                    if slot == '2':
                        result.run2_value = v
                    else:
                        result.run3_value = v
                result.calculate_cumulative_score()

            else:
                raw = form_data.get(f'result_{comp_id}')
                if not raw:
                    continue
                try:
                    parsed = float(raw)
                except (TypeError, ValueError):
                    invalid.append((comp_id, raw))
                    continue

                result = result_by_comp.get(comp_id)
                if not result:
                    comp = comp_lookup.get(comp_id)
                    result = EventResult(
                        event_id=event.id,
                        competitor_id=comp_id,
                        competitor_type=event.event_type,
                        competitor_name=comp.display_name if comp else f'Unknown ({comp_id})',
                    )
                    db.session.add(result)
                    result_by_comp[comp_id] = result

                result.result_value = parsed

            if event.is_hard_hit:
                raw_tb = form_data.get(f'tiebreak_{comp_id}')
                if raw_tb:
                    try:
                        result.tiebreak_value = float(raw_tb)
                    except (TypeError, ValueError):
                        pass

            result.status = status
            raw_reason = str(form_data.get(f'reason_{comp_id}', '') or '').strip()
            if status in ('scratched', 'dnf', 'dq'):
                result.status_reason = raw_reason or None
            else:
                result.status_reason = None

            if result.id:
                log_action(
                    'score_edited',
                    'event_result',
                    result.id,
                    {
                        'event_id': event.id,
                        'heat_id': heat.id,
                        'new_value': result.result_value,
                        'judge_user_id': judge_user_id,
                    },
                )
                if event.is_finalized:
                    event.is_finalized = False
                    event.status = 'in_progress'

            changes += 1

        if changes == 0:
            return {
                'ok': False,
                'category': 'warning',
                'message': 'No result values were entered; heat remains pending.',
                'redirect_kind': 'heat_entry',
                'redirect_event_id': event.id,
                'redirect_heat_id': heat.id,
                'status_code': 400,
            }

        heat.status = 'completed'
        heat.release_lock(judge_user_id or 0)

        all_heats_complete = all(h.status == 'completed' for h in event.heats.all())
        finalize_failed = False
        if all_heats_complete:
            try:
                with db.session.begin_nested():
                    engine.calculate_positions(event)
            except Exception as exc:
                logger.error('auto-finalize failed for event %s: %s', event.id, exc)
                event.is_finalized = False
                event.status = 'in_progress'
                finalize_failed = True

        log_action(
            'heat_results_saved',
            'heat',
            heat.id,
            {
                'event_id': event.id,
                'result_updates': changes,
                'judge_user_id': judge_user_id,
            },
        )
        db.session.commit()

    except StaleDataError:
        db.session.rollback()
        return {
            'ok': False,
            'category': 'warning',
            'message': 'These scores were updated by another judge while you were entering results. '
                       'Please reload to see the latest values before saving again.',
            'redirect_kind': 'heat_entry',
            'redirect_event_id': event.id,
            'redirect_heat_id': heat.id,
            'status_code': 409,
        }
    except IntegrityError:
        db.session.rollback()
        return {
            'ok': False,
            'category': 'error',
            'message': 'A database constraint was violated while saving results. '
                       'Check for duplicate entries and try again.',
            'redirect_kind': 'heat_entry',
            'redirect_event_id': event.id,
            'redirect_heat_id': heat.id,
            'status_code': 409,
        }

    invalidate_tournament_caches(tournament_id)
    undo_token = {
        'heat_id': heat.id,
        'event_id': event.id,
        'saved_at': datetime.now(timezone.utc).isoformat(),
    }

    if finalize_failed:
        return {
            'ok': True,
            'category': 'warning',
            'message': ('Heat saved, but auto-finalization failed. The event '
                        'results page will let you retry - your timer values '
                        'are safe.'),
            'redirect_kind': 'event_results',
            'redirect_event_id': event.id,
            'redirect_heat_id': heat.id,
            'status_code': 200,
            'undo_heat_id': heat.id,
            'undo_token': undo_token,
        }

    if invalid:
        return {
            'ok': True,
            'category': 'warning',
            'message': f'Heat saved with {len(invalid)} invalid value(s) skipped.',
            'redirect_kind': 'event_results',
            'redirect_event_id': event.id,
            'redirect_heat_id': heat.id,
            'status_code': 200,
            'undo_heat_id': heat.id,
            'undo_token': undo_token,
        }

    return {
        'ok': True,
        'category': 'success',
        'message': text.FLASH['heat_saved'],
        'redirect_kind': 'event_results',
        'redirect_event_id': event.id,
        'redirect_heat_id': heat.id,
        'status_code': 200,
        'undo_heat_id': heat.id,
        'undo_token': undo_token,
    }


def finalize_event_results(
    *,
    event: Event,
    tournament_id: int,
    judge_user_id: int | None,
) -> dict:
    warnings = engine.validate_finalization(event)

    try:
        with db.session.begin_nested():
            engine.calculate_positions(event)
            log_action(
                'event_finalized',
                'event',
                event.id,
                {
                    'tournament_id': tournament_id,
                    'judge_user_id': judge_user_id,
                },
            )
        db.session.commit()
    except StaleDataError:
        db.session.rollback()
        return {
            'ok': False,
            'warnings': warnings,
            'message': 'Results were modified by another judge during finalization. Reload and finalize again.',
            'status_code': 409,
        }
    except IntegrityError:
        db.session.rollback()
        return {
            'ok': False,
            'warnings': warnings,
            'message': 'A database constraint error occurred during finalization. Contact an admin if this persists.',
            'status_code': 409,
        }

    invalidate_tournament_caches(tournament_id)
    return {
        'ok': True,
        'warnings': warnings,
        'message': f'{event.display_name} finalized.',
        'status_code': 200,
    }
