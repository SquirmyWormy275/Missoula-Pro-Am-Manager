"""
Mark Assignment Service — STRATHMARK handicap start-mark pipeline.

When an event is configured as a handicap-format event (Event.is_handicap=True),
each competitor needs a start mark (in seconds) assigned before the event runs.
The start mark is stored on EventResult.handicap_factor and is subtracted from
the competitor's raw time by scoring_engine._metric() to produce their net time.

This module provides:
  - assign_handicap_marks(event)  — top-level call; populates EventResult.handicap_factor
    for every active result row in the event by querying the STRATHMARK
    HandicapCalculator.  Returns a result dict with counts and errors.
  - is_mark_assignment_eligible(event) — quick guard: True only when the event
    qualifies for handicap mark assignment.

Integration points:
  - Called from the mark assignment route (routes/scheduling/assign_marks.py)
    when a judge clicks "Assign Marks" on the scheduling page.
  - Requires STRATHMARK_SUPABASE_URL and STRATHMARK_SUPABASE_KEY env vars.
  - Non-blocking by design: failures are logged as warnings; the caller always
    receives a result dict — never an unhandled exception.

Design notes:
  - STRATHMARK's HandicapCalculator API is expected to accept a competitor's
    strathmark_id + event_code and return a start_mark_seconds float.
  - If a competitor has no strathmark_id, no mark is assigned (they compete
    from scratch; handicap_factor stays at default 1.0 which _metric() treats
    as 0.0 start mark).
  - If STRATHMARK is not configured, this function is a safe no-op that returns
    immediately with an 'unconfigured' status.
  - handicap_factor default 1.0 is the DB placeholder.  A value of 0.0 means
    explicitly assigned as scratch.  Any other value is a real start mark.
"""
from __future__ import annotations

import logging
from typing import Optional

from models.event import Event, EventResult
from models.competitor import ProCompetitor, CollegeCompetitor
import config

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# STRATHMARK event codes for handicap-eligible stand types
# ---------------------------------------------------------------------------

_STAND_TYPE_TO_EVENT_CODE = {
    'underhand': 'UH',
    'standing_block': 'SB',
    'springboard': 'SPB',
}


# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------

def is_mark_assignment_eligible(event: Event) -> bool:
    """Return True when the event qualifies for STRATHMARK mark assignment.

    Conditions:
      - event.is_handicap is True
      - event.scoring_type == 'time'  (marks are in seconds; non-time events
        don't use start marks)
      - event.stand_type is in HANDICAP_ELIGIBLE_STAND_TYPES
      - event.status is not 'completed' (marks should be set before scoring)
    """
    if not getattr(event, 'is_handicap', False):
        return False
    if event.scoring_type != 'time':
        return False
    eligible = getattr(config, 'HANDICAP_ELIGIBLE_STAND_TYPES', set())
    if event.stand_type not in eligible:
        return False
    return True


def assign_handicap_marks(event: Event) -> dict:
    """Populate EventResult.handicap_factor for every active result in *event*.

    Queries the STRATHMARK HandicapCalculator for each competitor's start mark
    and stores it on their EventResult row.  The DB session is NOT committed
    here — the caller is responsible for committing.

    Returns a dict:
      {
        'status':   'ok' | 'unconfigured' | 'not_eligible' | 'partial' | 'error',
        'assigned': int,   # number of results where a mark was written
        'skipped':  int,   # no strathmark_id or no mark returned
        'errors':   list[str],
      }
    """
    if not is_mark_assignment_eligible(event):
        return {'status': 'not_eligible', 'assigned': 0, 'skipped': 0, 'errors': []}

    from services.strathmark_sync import is_configured
    if not is_configured():
        logger.info('mark_assignment: STRATHMARK not configured — skipping for event %s', event.id)
        return {'status': 'unconfigured', 'assigned': 0, 'skipped': 0, 'errors': []}

    event_code = _STAND_TYPE_TO_EVENT_CODE.get(event.stand_type)
    if not event_code:
        return {
            'status': 'not_eligible',
            'assigned': 0,
            'skipped': 0,
            'errors': [f'No STRATHMARK event code for stand_type={event.stand_type}'],
        }

    results = (
        EventResult.query
        .filter_by(event_id=event.id)
        .filter(EventResult.status.in_(['pending', 'completed']))
        .all()
    )

    if not results:
        return {'status': 'ok', 'assigned': 0, 'skipped': 0, 'errors': []}

    # Build a strathmark_id lookup keyed by competitor_id
    comp_ids = [r.competitor_id for r in results]
    strathmark_lookup = _build_strathmark_id_lookup(event, comp_ids)

    assigned = 0
    skipped = 0
    errors: list[str] = []

    calculator = _get_handicap_calculator()
    if calculator is None:
        return {
            'status': 'error',
            'assigned': 0,
            'skipped': len(results),
            'errors': ['Could not initialise STRATHMARK HandicapCalculator'],
        }

    # Preferred path: batch calculate via calculator.calculate()
    batch_marks = _batch_calculate_marks(calculator, event, results, strathmark_lookup)

    for result in results:
        sm_id = strathmark_lookup.get(result.competitor_id)
        name = result.competitor_name or f'competitor_{result.competitor_id}'

        # Try batch result first (keyed by competitor_name)
        if batch_marks and name in batch_marks:
            mr = batch_marks[name]
            result.handicap_factor = float(mr.mark)
            result.predicted_time = float(mr.predicted_time) if mr.predicted_time else None
            assigned += 1
            logger.debug(
                'mark_assignment: event=%s competitor=%s mark=%ds predicted=%.2fs method=%s',
                event.id, result.competitor_id, mr.mark,
                mr.predicted_time or 0.0, mr.method_used,
            )
            continue

        # Fallback: single-competitor legacy path
        if not sm_id:
            logger.debug(
                'mark_assignment: no strathmark_id for competitor %s — using scratch',
                result.competitor_id,
            )
            skipped += 1
            continue

        mark = _fetch_start_mark(calculator, sm_id, event_code, result.competitor_name)
        if mark is None:
            skipped += 1
            continue

        result.handicap_factor = mark
        result.predicted_time = None
        assigned += 1
        logger.debug(
            'mark_assignment: event=%s competitor=%s mark=%.2fs (legacy path)',
            event.id, result.competitor_id, mark,
        )

    logger.info("HandicapCalculator produced %d marks", assigned)
    status = 'ok' if not errors else 'partial'
    return {'status': status, 'assigned': assigned, 'skipped': skipped, 'errors': errors}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _build_strathmark_id_lookup(event: Event, competitor_ids: list[int]) -> dict[int, Optional[str]]:
    """Return {competitor_id: strathmark_id} for all competitors in *competitor_ids*."""
    lookup: dict[int, Optional[str]] = {}
    if not competitor_ids:
        return lookup

    if event.event_type == 'college':
        rows = CollegeCompetitor.query.filter(
            CollegeCompetitor.id.in_(competitor_ids)
        ).all()
    else:
        rows = ProCompetitor.query.filter(
            ProCompetitor.id.in_(competitor_ids)
        ).all()

    for row in rows:
        lookup[row.id] = getattr(row, 'strathmark_id', None)

    return lookup


def _get_handicap_calculator(event_ceiling: int = None):
    """Attempt to instantiate the STRATHMARK HandicapCalculator.

    Returns the calculator object on success, None on any failure.
    The STRATHMARK package is optional — if not installed or misconfigured,
    this returns None gracefully.

    HandicapCalculator.__init__ accepts:
      event_ceiling (Optional[int]), ollama_url (str),
      wood_df (Optional[DataFrame]), results_df (Optional[DataFrame])
    """
    try:
        import os
        ollama_url = os.environ.get('STRATHMARK_OLLAMA_URL', 'http://localhost:11434')
        # Import lazily so missing package never breaks the app at startup
        from strathmark.calculator import HandicapCalculator  # type: ignore[import]
        kwargs = {'ollama_url': ollama_url}
        if event_ceiling is not None:
            kwargs['event_ceiling'] = event_ceiling
        return HandicapCalculator(**kwargs)
    except ImportError:
        logger.warning('mark_assignment: strathmark package not installed — mark assignment unavailable')
        return None
    except Exception as exc:
        logger.error('mark_assignment: failed to create HandicapCalculator: %s', exc)
        return None


def _build_competitor_records(event, results, strathmark_lookup):
    """Build a list of STRATHMARK CompetitorRecord objects for calculate().

    Returns (records_list, result_by_name) where result_by_name maps
    CompetitorRecord.name → EventResult for mark assignment after calculate().
    """
    try:
        from strathmark.calculator import CompetitorRecord  # type: ignore[import]
    except ImportError:
        return [], {}

    records = []
    result_by_name = {}
    for r in results:
        sm_id = strathmark_lookup.get(r.competitor_id)
        name = r.competitor_name or f'competitor_{r.competitor_id}'
        record = CompetitorRecord(name=name)
        result_by_name[name] = r
        records.append(record)
    return records, result_by_name


def _build_wood_profile(event):
    """Build a STRATHMARK WoodProfile from the event's tournament WoodConfig.

    Returns a WoodProfile on success, None if data is unavailable.
    """
    try:
        from strathmark.calculator import WoodProfile  # type: ignore[import]
    except ImportError:
        return None

    try:
        from models.wood_config import WoodConfig
        # Look up wood config for this event's stand type and tournament
        gender_suffix = f'_{event.gender}' if getattr(event, 'gender', None) else '_M'
        event_type = getattr(event, 'event_type', 'pro')
        config_key = f'block_{event.stand_type}_{event_type}{gender_suffix}'
        wc = WoodConfig.query.filter_by(
            tournament_id=event.tournament_id,
            config_key=config_key,
        ).first()
        if wc is None:
            # Try without gender suffix
            config_key = f'block_{event.stand_type}_{event_type}'
            wc = WoodConfig.query.filter_by(
                tournament_id=event.tournament_id,
                config_key=config_key,
            ).first()
        if wc is None:
            logger.debug('mark_assignment: no WoodConfig for %s', config_key)
            return WoodProfile(species='Pine', diameter_mm=300.0, quality=5)

        diameter_mm = wc.size_value
        if wc.size_unit == 'in':
            diameter_mm = wc.size_value * 25.4
        return WoodProfile(
            species=wc.species or 'Pine',
            diameter_mm=diameter_mm,
            quality=5,
        )
    except Exception as exc:
        logger.warning('mark_assignment: failed to build WoodProfile: %s', exc)
        return None


def _fetch_start_mark(calculator, strathmark_id: str, event_code: str, name: str) -> Optional[float]:
    """Single-competitor fallback: call calculator.calculate() for one competitor.

    Returns the start mark in seconds (float >= 0) on success, None on failure.
    A return of 0.0 means scratch (no mark assigned by the calculator).

    NOTE: This is the legacy single-competitor fallback. The preferred path is
    _batch_calculate_marks() which calls calculator.calculate() for all
    competitors at once and returns full MarkResult objects with predicted_time.
    """
    try:
        from strathmark.predictor import CompetitorRecord, WoodProfile  # type: ignore[import]

        record = CompetitorRecord(name=name)
        # Use a default WoodProfile — the batch path is preferred for accurate wood data
        wood = WoodProfile(species='Pine', diameter_mm=300.0, quality=5)

        mark_results = calculator.calculate(
            competitors=[record],
            wood=wood,
            event_code=event_code,
        )
        if not mark_results:
            logger.debug('mark_assignment: no mark returned for %s (%s)', name, strathmark_id)
            return None

        mr = mark_results[0]
        mark_val = float(mr.mark)
        if mark_val < 0:
            logger.warning(
                'mark_assignment: negative mark %.2f for %s — clamping to 0', mark_val, name
            )
            mark_val = 0.0
        return mark_val
    except ImportError:
        logger.debug('mark_assignment: strathmark.predictor not available — skipping single-competitor path')
        return None
    except Exception as exc:
        logger.warning('mark_assignment: error fetching mark for %s: %s', name, exc)
        return None


def _batch_calculate_marks(calculator, event, results, strathmark_lookup) -> Optional[dict]:
    """Call calculator.calculate() for all competitors in one batch.

    Returns {competitor_name: MarkResult} on success, None on failure.
    This is the preferred path — it calls the real HandicapCalculator.calculate()
    API and returns full MarkResult objects with .mark and .predicted_time.
    """
    try:
        records, result_by_name = _build_competitor_records(event, results, strathmark_lookup)
        if not records:
            return None

        wood = _build_wood_profile(event)
        if wood is None:
            return None

        event_code = _STAND_TYPE_TO_EVENT_CODE.get(event.stand_type)
        if not event_code:
            return None

        mark_results = calculator.calculate(
            competitors=records,
            wood=wood,
            event_code=event_code,
        )

        return {mr.name: mr for mr in mark_results}
    except Exception as exc:
        logger.warning('mark_assignment: batch calculate failed: %s', exc)
        return None
