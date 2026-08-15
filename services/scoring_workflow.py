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
from services.flight_builder import (
    lock_tournament_schedule,
    serialize_sqlite_schedule_writer,
)

logger = logging.getLogger(__name__)


def _testing_mode_enabled() -> bool:
    """Allow legacy unit forms to omit the production heat-instance token."""
    from flask import current_app

    return bool(current_app.testing)


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


def event_partner_pool(event: Event) -> dict[int, object]:
    """Every competitor enrolled in `event`, keyed by id, for partner resolution.

    The heat-local pool is NOT sufficient and the difference is not marginal.
    Measured against the real 2026 mirror, across all eight partnered events:

        pool                      claims resolved
        heat-local                 80 of 146
        event-enrolled            145 of 146

    Two separate reasons for the gap.  Peavey Log Roll, Pulp Toss and Partnered
    Axe Throw carry 53 claims between them and have no heats generated at all,
    so a heat-local lookup has nothing to match against.  And on pro Double Buck
    two of twenty-four pairs are split across heats, which the heat-local pool
    also cannot see.

    Sources unioned here: every heat's competitor list, plus every existing
    EventResult row for the event.  The second source matters after a heat undo,
    which deletes result rows, and the first matters before any row exists.
    """
    ids: set[int] = set()
    for heat in event.heats.all():
        ids.update(_normalize_competitor_ids(heat.get_competitors() or []))
    rows = db.session.query(EventResult.competitor_id).filter(
        EventResult.event_id == event.id,
        EventResult.competitor_type == event.event_type,
    ).all()
    for (cid,) in rows:
        if cid is not None:
            ids.add(int(cid))
    return competitor_lookup_for_event(event, sorted(ids))


def resolve_partner_display_name(event: Event, comp, pool: dict[int, object]) -> str:
    """Return the DISPLAY NAME of `comp`'s partner in `event`, or ''.

    Display name, not bare name, and that choice is load-bearing.
    scoring_engine._pair_key_for is::

        frozenset((result.competitor_name, result.partner_name))

    and `competitor_name` is written as ``comp.display_name`` everywhere.
    ProCompetitor.display_name is the bare name, so the two conventions coincide
    for pro and nobody has had to choose before.  CollegeCompetitor.display_name
    is ``'Nell Horgan (FVC-A)'``.  The claimed partner strings stored in
    ``college_competitors.partners`` are bare, mixed-case first names
    (``"Teagan"``, ``"GREER"``, ``"MATEO"``).  Writing the claim through
    verbatim, which is what the importer does and what the backlog prescribed,
    would produce::

        Greer's row : {"Greer Swoboda (MSU-A)", "Teagan"}
        Teagan's row: {"Teagan Wigen (MSU-A)", "GREER"}

    Those frozensets are never equal, so the pair never collapses and the fix
    would appear to work while changing nothing.  Resolving to the partner's
    display_name makes both rows carry {"Greer Swoboda (MSU-A)",
    "Teagan Wigen (MSU-A)"} and the engine collapses them with no change to the
    ranking code.

    It is also the invariant scratch_cascade.py:143 already assumes, where the
    partner's row is looked up by ``competitor_name == fr.partner_name``.

    Resolution uses the app's own three-tier matcher (exact, first-token,
    Levenshtein <= 2) so ``"GREER"`` finds ``"Greer Swoboda"``.  Returns '' on
    no match or ambiguity; callers must treat '' as "leave the stored value
    alone" so a resolution failure never erases a good imported value.
    """
    from services.partner_resolver import (
        _resolve_partner_name_local,
        lookup_partner_cid,
    )

    if comp is None:
        return ''
    claimed = _resolve_partner_name_local(comp, event)
    if not claimed:
        return ''
    partner_id = lookup_partner_cid(claimed, pool, comp.id)
    if partner_id is not None and partner_id in pool:
        return pool[partner_id].display_name
    return ''


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


@serialize_sqlite_schedule_writer
def save_heat_results_submission(
    *,
    tournament_id: int,
    heat: Heat,
    event: Event,
    form_data: Mapping[str, object],
    judge_user_id: int | None,
) -> dict:
    # Scoring and schedule rebuilds share this parent lock. A rebuild that
    # starts second must observe the completed heat and fail closed instead of
    # clearing the placement while the judge is saving it.
    original_heat_id = heat.id
    original_event_id = event.id
    lock_tournament_schedule(tournament_id)
    heat = (
        Heat.query
        .filter_by(id=original_heat_id)
        .populate_existing()
        .with_for_update()
        .one_or_none()
    )
    event = (
        Event.query
        .filter_by(id=original_event_id, tournament_id=tournament_id)
        .populate_existing()
        .with_for_update()
        .one_or_none()
    )
    if heat is None or event is None or heat.event_id != event.id:
        return {
            'ok': False,
            'category': 'warning',
            'message': (
                'This heat was replaced while the schedule was being rebuilt. '
                'Reload the current schedule before entering results.'
            ),
            'redirect_kind': 'event_results',
            'redirect_event_id': original_event_id,
            'redirect_heat_id': original_heat_id,
            'status_code': 409,
        }

    posted_identity = form_data.get('heat_identity')
    current_identity = (
        heat.locked_at.isoformat(timespec='microseconds')
        if heat.locked_at is not None else None
    )
    if posted_identity is not None or not _testing_mode_enabled():
        if not posted_identity or posted_identity != current_identity:
            return {
                'ok': False,
                'category': 'warning',
                'message': (
                    'This scoring form belongs to an older heat instance. '
                    'Reload the current heat before entering results.'
                ),
                'redirect_kind': 'heat_entry',
                'redirect_event_id': event.id,
                'redirect_heat_id': heat.id,
                'status_code': 409,
            }
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
    from services.mark_assignment import get_unreviewed_handicap_competitor_ids

    unreviewed_mark_ids = get_unreviewed_handicap_competitor_ids(event, competitor_ids)
    if unreviewed_mark_ids:
        names = ', '.join(
            getattr(comp_lookup.get(competitor_id), 'display_name', str(competitor_id))
            for competitor_id in unreviewed_mark_ids[:5]
        )
        suffix = f' (+{len(unreviewed_mark_ids) - 5} more)' if len(unreviewed_mark_ids) > 5 else ''
        return {
            'ok': False,
            'category': 'error',
            'message': (
                'Scoring is blocked until handicap marks are reviewed for: '
                f'{names}{suffix}.'
            ),
            'redirect_kind': 'mark_assignment',
            'redirect_event_id': event.id,
            'redirect_heat_id': heat.id,
            'status_code': 409,
        }

    # One extra query, only for partnered events, only on a heat save.  See
    # event_partner_pool for why the heat-local pool cannot be reused here.
    partner_pool = event_partner_pool(event) if event.is_partnered else {}
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

            # Pair identity, on BOTH the create and the update path.
            #
            # The backlog filed this as "persist partner_name on the INSERT
            # branch".  That is not enough.  On the real mirror all eight rows
            # of college Double Buck already exist with status='pending', and
            # existing_results_for_event returns rows regardless of status, so
            # every one of them takes the UPDATE branch and the insert branch is
            # never reached for the population the bug is filed against.
            #
            # Guarded on a truthy derivation: a resolution failure must leave a
            # good imported value in place rather than blanking it.
            if event.is_partnered:
                derived = resolve_partner_display_name(
                    event,
                    comp_lookup.get(comp_id) or partner_pool.get(comp_id),
                    partner_pool,
                )
                if derived:
                    result.partner_name = derived

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

        # A dual-timer row where only one watch was read is stored as
        # status='partial' (see the is_dual_timer_event branch above, which
        # downgrades 'completed' to 'partial' when no average could be
        # computed).  Auto-finalize used to run anyway.
        #
        # calculate_positions places only status=='completed' rows
        # (scoring_engine.py) but still sets is_finalized=True, so the partial
        # competitor finalized at position None with 0.00 points and vanished
        # from the public results feed, which filters on the same status
        # (routes/api.py:190).  The save returned the ordinary success flash,
        # so nothing on the operator's screen said an event had just been
        # published one competitor short.
        #
        # Deferring rather than blocking is deliberate.  The heat still counts
        # as run, the values the judge did enter are still saved, and the
        # moment the second watch is read and the heat re-saved there are no
        # partial rows left and the ordinary auto-finalize fires.  Manual
        # finalize is untouched on purpose: an operator who knows a timer is
        # gone for good needs a way to close the event, and the honest way to
        # record that competitor is DNF on the entry form, not a partial row.
        # validate_finalization now names the partial rows on that path.
        db.session.flush()
        partial_rows = [r for r in event.results.all() if r.status == 'partial']
        # Read the names now, while the objects are live. commit() expires
        # them and the caller formats this message after the commit.
        partial_names = [r.competitor_name or f'competitor {r.competitor_id}'
                         for r in partial_rows]

        all_heats_complete = all(h.status == 'completed' for h in event.heats.all())
        finalize_deferred = bool(all_heats_complete and partial_rows)
        finalize_failed = False
        if all_heats_complete and not partial_rows:
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
        'heat_version': heat.version_id,
        'result_versions': {
            str(result.id): result.version_id
            for result in EventResult.query.filter(
                EventResult.event_id == event.id,
                EventResult.competitor_id.in_(competitor_ids),
                EventResult.competitor_type == event.event_type,
            ).all()
        },
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

    if finalize_deferred:
        shown = ', '.join(partial_names[:5])
        more = (f' (+{len(partial_names) - 5} more)'
                if len(partial_names) > 5 else '')
        extra = (f' {len(invalid)} invalid value(s) were also skipped.'
                 if invalid else '')
        return {
            'ok': True,
            'category': 'warning',
            'message': (f'Heat saved. The event was NOT finalized because '
                        f'{len(partial_names)} result(s) still have only one '
                        f'timer entered: {shown}{more}. Enter the second timer '
                        f'and save again to finalize, or set them to DNF if '
                        f'the time is gone.{extra}'),
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
