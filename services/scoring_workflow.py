from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from typing import Mapping
from uuid import UUID

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm.exc import StaleDataError

import services.scoring_engine as engine
import strings as text
from database import db
from models import AuditLog, Event, EventResult, Heat, ScoreSubmissionReceipt, User
from models.competitor import CollegeCompetitor, ProCompetitor
from services.audit import log_action
from services.cache_invalidation import invalidate_tournament_caches
from services.flight_builder import (
    lock_tournament_schedule,
    serialize_sqlite_schedule_writer,
)

logger = logging.getLogger(__name__)

_PAYLOAD_TRANSPORT_FIELDS = frozenset({
    'csrf_token',
    'replay_token',
    'payload_sha256',
    'tournament_id',
    'heat_id',
})


def _form_pairs(form_data: Mapping[str, object]) -> list[tuple[str, str]]:
    """Return stable string pairs without losing MultiDict repeated values."""
    pairs: list[tuple[str, str]] = []
    if hasattr(form_data, 'lists'):
        for raw_key, raw_values in form_data.lists():
            key = str(raw_key)
            for raw_value in raw_values:
                pairs.append((key, str(raw_value or '')))
        return pairs
    for raw_key, raw_value in form_data.items():
        key = str(raw_key)
        if isinstance(raw_value, (list, tuple)):
            pairs.extend((key, str(value or '')) for value in raw_value)
        else:
            pairs.append((key, str(raw_value or '')))
    return pairs


def canonical_score_payload_sha256(
    form_data: Mapping[str, object],
    *,
    tournament_id: int,
    heat_id: int,
) -> str:
    """Hash the complete score payload while excluding transport credentials."""
    pairs = [
        (key, value)
        for key, value in _form_pairs(form_data)
        if key not in _PAYLOAD_TRANSPORT_FIELDS
    ]
    pairs.extend((
        ('heat_id', str(int(heat_id))),
        ('tournament_id', str(int(tournament_id))),
    ))
    canonical = json.dumps(
        sorted(pairs),
        ensure_ascii=False,
        separators=(',', ':'),
    )
    return hashlib.sha256(canonical.encode('utf-8')).hexdigest()


def heat_submission_identity(heat: Heat) -> str:
    """Return a lock-independent identity for one generated heat instance."""
    payload = {
        'event_id': heat.event_id,
        'flight_id': heat.flight_id,
        'flight_position': heat.flight_position,
        'heat_id': heat.id,
        'heat_number': heat.heat_number,
        'roster': heat.get_competitors(),
        'run_number': heat.run_number,
        'stands': heat.get_stand_assignments(),
    }
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
    )
    return hashlib.sha256(canonical.encode('utf-8')).hexdigest()


def heat_scoring_state_digest(
    heat: Heat,
    event: Event,
    results: object | None = None,
) -> str:
    """Hash score-relevant state without including the transient judge lock."""
    competitor_ids = _normalize_competitor_ids(heat.get_competitors())
    if results is None:
        rows = EventResult.query.filter(
            EventResult.event_id == event.id,
            EventResult.competitor_id.in_(competitor_ids),
            EventResult.competitor_type == event.event_type,
        ).all()
    else:
        rows = list(results)

    result_fields = (
        'id',
        'competitor_id',
        'competitor_type',
        'version_id',
        'result_value',
        'result_unit',
        'run1_value',
        'run2_value',
        'run3_value',
        'best_run',
        'tiebreak_value',
        't1_run1',
        't2_run1',
        't1_run2',
        't2_run2',
        'status',
        'status_reason',
        'is_flagged',
        'throwoff_pending',
    )

    def _stable_value(value: object) -> object:
        if value is None or isinstance(value, (bool, int, str)):
            return value
        return str(value)

    payload = {
        'heat_identity': heat_submission_identity(heat),
        'heat_status': heat.status,
        'results': [
            {
                field: _stable_value(getattr(result, field))
                for field in result_fields
            }
            for result in sorted(
                rows,
                key=lambda result: (result.competitor_id, result.id or 0),
            )
        ],
    }
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
    )
    return hashlib.sha256(canonical.encode('utf-8')).hexdigest()


def _normalized_request_id(form_data: Mapping[str, object]) -> str | None:
    raw = str(form_data.get('request_id') or '').strip()
    if not raw:
        return None
    try:
        return str(UUID(raw))
    except (TypeError, ValueError, AttributeError):
        return ''


def _receipt_public(receipt: ScoreSubmissionReceipt) -> dict:
    return {
        'request_id': receipt.request_id,
        'tournament_id': receipt.tournament_id,
        'heat_id': receipt.heat_id,
        'issuing_user_id': receipt.issuing_user_id,
        'payload_sha256': receipt.canonical_payload_sha256,
        'accepted': True,
    }


def _receipt_conflict(
    *,
    event_id: int,
    heat_id: int,
    code: str,
    message: str,
) -> dict:
    return {
        'ok': False,
        'category': 'error',
        'message': message,
        'error_code': code,
        'redirect_kind': 'heat_entry',
        'redirect_event_id': event_id,
        'redirect_heat_id': heat_id,
        'status_code': 409,
    }


def _outcome_from_receipt(
    receipt: ScoreSubmissionReceipt,
    *,
    tournament_id: int,
    heat_id: int,
    event_id: int,
    judge_user_id: int | None,
    payload_sha256: str,
) -> dict:
    if receipt.issuing_user_id is None:
        return _receipt_conflict(
            event_id=event_id,
            heat_id=heat_id,
            code='request_id_issuer_deleted',
            message=(
                'The account that issued this score request no longer exists. '
                'The retained request ID cannot be replayed.'
            ),
        )
    if (
        receipt.tournament_id != tournament_id
        or receipt.heat_id != heat_id
        or receipt.issuing_user_id != judge_user_id
    ):
        return _receipt_conflict(
            event_id=event_id,
            heat_id=heat_id,
            code='request_id_binding_mismatch',
            message=(
                'This score request ID is already bound to another user, '
                'tournament, or heat. The queued entry was not applied.'
            ),
        )
    if receipt.canonical_payload_sha256 != payload_sha256:
        return _receipt_conflict(
            event_id=event_id,
            heat_id=heat_id,
            code='request_id_payload_mismatch',
            message=(
                'This score request ID was already used with different values. '
                'The queued entry was not applied.'
            ),
        )
    outcome = dict(receipt.accepted_outcome_json or {})
    if outcome.get('receipt_revoked') is True:
        return _receipt_conflict(
            event_id=event_id,
            heat_id=heat_id,
            code='request_id_revoked',
            message=(
                'This score request was superseded by a heat undo. '
                'Reload the heat before entering a new score.'
            ),
        )
    outcome['receipt'] = _receipt_public(receipt)
    outcome['receipt_replayed'] = True
    return outcome


def _add_scoring_audit(
    action: str,
    entity_type: str,
    entity_id: int | None,
    *,
    judge_user_id: int | None,
    details: dict,
) -> None:
    """Add score audit evidence to the score transaction itself."""
    actor_user_id = judge_user_id
    if actor_user_id is not None and db.session.get(User, actor_user_id) is None:
        actor_user_id = None
    db.session.add(AuditLog(
        actor_user_id=actor_user_id,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        details_json=json.dumps(details, sort_keys=True),
    ))


def _create_submission_receipt(
    *,
    request_id: str,
    tournament_id: int,
    heat_id: int,
    judge_user_id: int,
    payload_sha256: str,
    outcome: dict,
) -> ScoreSubmissionReceipt:
    receipt = ScoreSubmissionReceipt(
        request_id=request_id,
        tournament_id=tournament_id,
        heat_id=heat_id,
        issuing_user_id=judge_user_id,
        canonical_payload_sha256=payload_sha256,
        accepted_outcome_json=outcome,
    )
    db.session.add(receipt)
    return receipt


def revoke_heat_submission_receipts_for_undo(
    *,
    tournament_id: int,
    heat_id: int,
    event_id: int,
    judge_user_id: int | None,
) -> list[str]:
    """Supersede accepted request IDs in the same transaction as a heat undo."""
    receipts = (
        ScoreSubmissionReceipt.query
        .filter_by(tournament_id=tournament_id, heat_id=heat_id)
        .with_for_update()
        .all()
    )
    revoked_at = datetime.now(timezone.utc).isoformat()
    revoked_request_ids: list[str] = []
    for receipt in receipts:
        accepted_outcome = dict(receipt.accepted_outcome_json or {})
        if accepted_outcome.get('receipt_revoked') is True:
            continue
        receipt.accepted_outcome_json = {
            'receipt_revoked': True,
            'reason': 'heat_undo',
            'revoked_at': revoked_at,
            'superseded_outcome': accepted_outcome,
        }
        revoked_request_ids.append(receipt.request_id)

    if revoked_request_ids:
        _add_scoring_audit(
            'score_submission_receipts_revoked',
            'heat',
            heat_id,
            judge_user_id=judge_user_id,
            details={
                'event_id': event_id,
                'request_ids': sorted(revoked_request_ids),
                'reason': 'heat_undo',
            },
        )
    _add_scoring_audit(
        'heat_undo',
        'heat',
        heat_id,
        judge_user_id=judge_user_id,
        details={
            'event_id': event_id,
            'judge_user_id': judge_user_id,
            'revoked_request_ids': sorted(revoked_request_ids),
        },
    )
    return revoked_request_ids


def _capture_shadow_outcomes(event: Event, judge_user_id: int | None) -> None:
    from services.shadow_settlement import capture_shadow_outcome_revisions

    capture_shadow_outcome_revisions(event, actor_id=judge_user_id)


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
    request_id = _normalized_request_id(form_data)
    if request_id == '':
        return {
            'ok': False,
            'category': 'error',
            'message': 'Score request ID must be a valid UUID.',
            'error_code': 'request_id_invalid',
            'redirect_kind': 'heat_entry',
            'redirect_event_id': original_event_id,
            'redirect_heat_id': original_heat_id,
            'status_code': 400,
        }
    if request_id and judge_user_id is None:
        return {
            'ok': False,
            'category': 'error',
            'message': 'Login is required to submit an offline score request.',
            'error_code': 'session_required',
            'redirect_kind': 'heat_entry',
            'redirect_event_id': original_event_id,
            'redirect_heat_id': original_heat_id,
            'status_code': 401,
        }
    posted_issuer = _form_int(form_data, 'issuer_user_id')
    payload_sha256 = canonical_score_payload_sha256(
        form_data,
        tournament_id=tournament_id,
        heat_id=original_heat_id,
    )
    posted_payload_sha256 = str(form_data.get('payload_sha256') or '').strip()
    if posted_payload_sha256 and posted_payload_sha256 != payload_sha256:
        return _receipt_conflict(
            event_id=original_event_id,
            heat_id=original_heat_id,
            code='payload_fingerprint_mismatch',
            message='The queued score payload fingerprint does not match its values.',
        )

    if request_id:
        existing_receipt = db.session.get(ScoreSubmissionReceipt, request_id)
        if existing_receipt is not None:
            return _outcome_from_receipt(
                existing_receipt,
                tournament_id=tournament_id,
                heat_id=original_heat_id,
                event_id=original_event_id,
                judge_user_id=judge_user_id,
                payload_sha256=payload_sha256,
            )
    if posted_issuer is not None and posted_issuer != judge_user_id:
        return {
            'ok': False,
            'category': 'error',
            'message': 'This queued score belongs to another user.',
            'error_code': 'authorization_denied',
            'redirect_kind': 'heat_entry',
            'redirect_event_id': original_event_id,
            'redirect_heat_id': original_heat_id,
            'status_code': 403,
        }

    lock_tournament_schedule(tournament_id)
    # A matching request can commit while this transaction waits for the
    # tournament writer lock. Recheck after acquiring the lock so the waiting
    # duplicate receives the durable outcome instead of a transient stale-form
    # conflict caused by the first request's heat update.
    if request_id:
        committed_receipt = db.session.get(ScoreSubmissionReceipt, request_id)
        if committed_receipt is not None:
            return _outcome_from_receipt(
                committed_receipt,
                tournament_id=tournament_id,
                heat_id=original_heat_id,
                event_id=original_event_id,
                judge_user_id=judge_user_id,
                payload_sha256=payload_sha256,
            )
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

    if heat.is_locked() and heat.locked_by_user_id != (judge_user_id or -1):
        lock_owner = db.session.get(User, heat.locked_by_user_id)
        owner_name = (
            lock_owner.username
            if lock_owner is not None
            else f'User #{heat.locked_by_user_id}'
        )
        return {
            'ok': False,
            'category': 'warning',
            'message': (
                f'Heat is currently being edited by {owner_name}. '
                'Your submission was not saved.'
            ),
            'error_code': 'heat_locked',
            'redirect_kind': 'heat_entry',
            'redirect_event_id': event.id,
            'redirect_heat_id': heat.id,
            'status_code': 423,
        }

    posted_identity = form_data.get('heat_identity')
    current_identity = heat_submission_identity(heat)
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
    posted_state_digest = str(form_data.get('scoring_state_digest') or '')
    if posted_state_digest or not _testing_mode_enabled():
        if posted_state_digest != heat_scoring_state_digest(heat, event):
            return {
                'ok': False,
                'category': 'error',
                'message': (
                    'This heat or its scores changed in another session. '
                    'Reload and reconcile before saving.'
                ),
                'error_code': 'scoring_state_changed',
                'redirect_kind': 'heat_entry',
                'redirect_event_id': event.id,
                'redirect_heat_id': heat.id,
                'status_code': 409,
            }
    else:
        posted_version = _form_int(form_data, 'heat_version')
        if posted_version is None or posted_version != heat.version_id:
            return {
                'ok': False,
                'category': 'error',
                'message': (
                    'This heat changed in another session. '
                    'Reload and re-enter results.'
                ),
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
                _add_scoring_audit(
                    'score_edited',
                    'event_result',
                    result.id,
                    judge_user_id=judge_user_id,
                    details={
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
                    _capture_shadow_outcomes(event, judge_user_id)
            except Exception as exc:
                logger.error('auto-finalize failed for event %s: %s', event.id, exc)
                event.is_finalized = False
                event.status = 'in_progress'
                finalize_failed = True

        _add_scoring_audit(
            'heat_results_saved',
            'heat',
            heat.id,
            judge_user_id=judge_user_id,
            details={
                'event_id': event.id,
                'result_updates': changes,
                'judge_user_id': judge_user_id,
            },
        )

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
            outcome = {
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
        elif finalize_deferred:
            shown = ', '.join(partial_names[:5])
            more = (f' (+{len(partial_names) - 5} more)'
                    if len(partial_names) > 5 else '')
            extra = (f' {len(invalid)} invalid value(s) were also skipped.'
                     if invalid else '')
            outcome = {
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
        elif invalid:
            outcome = {
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
        else:
            outcome = {
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

        receipt = None
        if request_id:
            receipt = _create_submission_receipt(
                request_id=request_id,
                tournament_id=tournament_id,
                heat_id=heat.id,
                judge_user_id=judge_user_id,
                payload_sha256=payload_sha256,
                outcome=outcome,
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
        if request_id:
            raced_receipt = db.session.get(ScoreSubmissionReceipt, request_id)
            if raced_receipt is not None:
                return _outcome_from_receipt(
                    raced_receipt,
                    tournament_id=tournament_id,
                    heat_id=original_heat_id,
                    event_id=original_event_id,
                    judge_user_id=judge_user_id,
                    payload_sha256=payload_sha256,
                )
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
    if receipt is not None:
        outcome['receipt'] = _receipt_public(receipt)
        outcome['receipt_replayed'] = False
    return outcome


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
            _capture_shadow_outcomes(event, judge_user_id)
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
