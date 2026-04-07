"""
Mark Assignment Service — STRATHMARK handicap start-mark pipeline.

When an event is configured as a handicap-format event (Event.is_handicap=True),
each competitor needs a start mark (in seconds) assigned before the event runs.
The start mark is stored on EventResult.handicap_factor and is subtracted from
the competitor's raw time by scoring_engine._metric() to produce their net time.

This module provides:
  - assign_handicap_marks(event)  — top-level call; populates
    EventResult.handicap_factor and EventResult.predicted_time for every
    active result row in the event by querying the STRATHMARK
    HandicapCalculator.  Returns a result dict with counts and errors.
  - is_mark_assignment_eligible(event) — quick guard: True only when the event
    qualifies for handicap mark assignment.
  - parse_marks_csv(file_storage, results) — offline CSV upload path; see the
    "CSV upload" section near the bottom of this file.

Data flow (V2.7.x — fully wired):
  1. Pull historical results from the global STRATHMARK Supabase DB
     once per call (`pull_results()`).
  2. Build a per-competitor `CompetitorRecord` populated with:
       - history     — `HistoricalResult` rows filtered from step 1
       - division    — 'Open' (men) / 'Womens' (women), used by panel fallback
       - gender      — 'M' or 'F', used as ML feature
  3. Build a `WoodProfile` from the event's `WoodConfig` row.  When no
     WoodConfig is configured for this event, return status `no_wood_config`
     and let the route flash a warning to the judge — we never silently
     guess the wood (Bug 3 fix).
  4. Construct `HandicapCalculator(wood_df=..., results_df=..., ollama_url=...)`
     so the prediction cascade has access to species hardness AND the global
     historical results for ML training and cross-competitor stats.
  5. Call `calculator.calculate(competitors=, wood=, event_code=)` once per
     event (batched).  STRATHMARK returns `List[MarkResult]` with `.mark`
     (int seconds) and `.predicted_time` (float seconds).  We persist both
     onto each `EventResult` row.

Constraints from upstream STRATHMARK:
  - `event_code` MUST be one of {'SB', 'UH'} — `calculator.calculate()`
    raises ValueError on anything else.  Springboard ('SPB') is NOT yet
    supported by STRATHMARK; we gate it out at `is_mark_assignment_eligible()`
    so the route never reaches the calculator with an unsupported event.
  - `WoodProfile.diameter_mm` valid range is 225..500 mm per the dataclass
    docstring.  We don't enforce here; if WoodConfig holds an out-of-range
    value the calculator will surface its own error.

Integration points:
  - Called from `routes/scheduling/assign_marks.py` when a judge clicks
    "Assign Marks" on the scheduling page.
  - Requires STRATHMARK_SUPABASE_URL and STRATHMARK_SUPABASE_KEY env vars.
  - Non-blocking by design: failures are logged as warnings; the caller
    always receives a result dict — never an unhandled exception.
"""
from __future__ import annotations

import csv
import io
import logging
from typing import Any, Optional

import config
from models.competitor import CollegeCompetitor, ProCompetitor
from models.event import Event, EventResult

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# STRATHMARK event codes for handicap-eligible stand types
# ---------------------------------------------------------------------------
#
# STRATHMARK currently supports only Standing Block ('SB') and Underhand
# ('UH').  Springboard support is on the STRATHMARK roadmap but not yet
# shipped — `calculator.calculate()` raises ValueError for any code that
# isn't 'SB' or 'UH' (see strathmark/predictor.py::is_valid_event).
#
# Until STRATHMARK adds 'SPB', the eligibility check below filters
# springboard events out at the door so the route never reaches the
# calculator with an unsupported event.

_STAND_TYPE_TO_EVENT_CODE = {
    'underhand': 'UH',
    'standing_block': 'SB',
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
      - event.stand_type maps to a STRATHMARK-supported event code
        (currently {SB, UH} only — springboard is NOT yet supported upstream)
    """
    if not getattr(event, 'is_handicap', False):
        return False
    if event.scoring_type != 'time':
        return False
    eligible = getattr(config, 'HANDICAP_ELIGIBLE_STAND_TYPES', set())
    if event.stand_type not in eligible:
        return False
    if event.stand_type not in _STAND_TYPE_TO_EVENT_CODE:
        return False
    return True


def assign_handicap_marks(event: Event) -> dict:
    """Populate EventResult.handicap_factor + .predicted_time for every active
    result in *event*.

    Queries the STRATHMARK HandicapCalculator for each competitor's start mark
    and stores it on their EventResult row.  The DB session is NOT committed
    here — the caller is responsible for committing.

    Returns a dict:
      {
        'status':   'ok' | 'unconfigured' | 'not_eligible' | 'no_wood_config'
                    | 'partial' | 'error',
        'assigned': int,   # number of results where a mark was written
        'skipped':  int,   # no strathmark_id or no mark returned
        'errors':   list[str],
      }
    """
    if not is_mark_assignment_eligible(event):
        return _empty_result('not_eligible')

    from services.strathmark_sync import is_configured
    if not is_configured():
        logger.info(
            'mark_assignment: STRATHMARK not configured — skipping for event %s',
            event.id,
        )
        return _empty_result('unconfigured')

    event_code = _STAND_TYPE_TO_EVENT_CODE.get(event.stand_type)
    if not event_code:
        # Unreachable thanks to is_mark_assignment_eligible(); belt-and-braces.
        return {
            'status': 'not_eligible',
            'assigned': 0,
            'skipped': 0,
            'errors': [f'No STRATHMARK event code for stand_type={event.stand_type}'],
        }

    # Bug 3 fix: refuse to silently guess wood properties.  If WoodConfig
    # is missing, surface a `no_wood_config` status so the route can flash
    # a clear warning to the judge.
    wood_profile = _build_wood_profile(event)
    if wood_profile is None:
        logger.warning(
            'mark_assignment: no WoodConfig for event %s (%s) — refusing to '
            'guess wood properties',
            event.id, event.name,
        )
        return _empty_result('no_wood_config')

    results = (
        EventResult.query
        .filter_by(event_id=event.id)
        .filter(EventResult.status.in_(['pending', 'completed']))
        .all()
    )
    if not results:
        return _empty_result('ok')

    # ------------------------------------------------------------------
    # Pull global historical results once.  Used for:
    #   - per-competitor history (filtered by strathmark_id)
    #   - cross-competitor stats inside the predictor (species affinity,
    #     event-wide baseline shrinkage, ML training)
    # ------------------------------------------------------------------
    global_results_df = _pull_global_results_df()

    # Map EventResult.competitor_id → CollegeCompetitor|ProCompetitor row.
    comp_ids = [r.competitor_id for r in results]
    competitor_models = _build_competitor_model_lookup(event, comp_ids)

    # Construct calculator with full data context.
    calculator = _get_handicap_calculator(global_results_df=global_results_df)
    if calculator is None:
        return {
            'status': 'error',
            'assigned': 0,
            'skipped': len(results),
            'errors': ['Could not initialise STRATHMARK HandicapCalculator'],
        }

    # Build CompetitorRecord list with history + division + gender populated.
    records, _result_by_name = _build_competitor_records(
        results=results,
        competitor_models=competitor_models,
        global_results_df=global_results_df,
        event_code=event_code,
    )
    if not records:
        return _empty_result('ok')

    # ------------------------------------------------------------------
    # Run the calculator (single batched call).
    # ------------------------------------------------------------------
    assigned = 0
    skipped = 0
    errors: list[str] = []

    try:
        mark_results = calculator.calculate(
            competitors=records,
            wood=wood_profile,
            event_code=event_code,
        )
    except ValueError as exc:
        logger.error(
            'mark_assignment: calculator.calculate() rejected input for event %s: %s',
            event.id, exc,
        )
        return {
            'status': 'error',
            'assigned': 0,
            'skipped': len(results),
            'errors': [f'STRATHMARK rejected input: {exc}'],
        }
    except Exception as exc:
        logger.exception('mark_assignment: calculator.calculate() raised for event %s', event.id)
        return {
            'status': 'error',
            'assigned': 0,
            'skipped': len(results),
            'errors': [f'STRATHMARK calculator failed: {exc}'],
        }

    # Map MarkResult back to EventResult by competitor_name.
    by_name = {mr.name: mr for mr in mark_results}
    for result in results:
        name = result.competitor_name or f'competitor_{result.competitor_id}'
        mr = by_name.get(name)
        if mr is None:
            skipped += 1
            continue

        # Bug 2 fix: predicted_time is non-optional float on MarkResult; the
        # `if mr.predicted_time` falsy check would lose an exact 0.0 (which
        # IS a valid prediction, however unlikely).  Store unconditionally.
        result.handicap_factor = float(mr.mark)
        result.predicted_time = float(mr.predicted_time)
        assigned += 1
        logger.debug(
            'mark_assignment: event=%s competitor=%s mark=%ds predicted=%.2fs method=%s',
            event.id, result.competitor_id, mr.mark, mr.predicted_time, mr.method_used,
        )

    logger.info(
        'mark_assignment: event=%s assigned=%d skipped=%d (HandicapCalculator)',
        event.id, assigned, skipped,
    )
    status = 'ok' if not errors else 'partial'
    return {'status': status, 'assigned': assigned, 'skipped': skipped, 'errors': errors}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _empty_result(status: str) -> dict:
    """Tiny helper to keep return statements compact."""
    return {'status': status, 'assigned': 0, 'skipped': 0, 'errors': []}


def _build_competitor_model_lookup(
    event: Event,
    competitor_ids: list[int],
) -> dict[int, Any]:
    """Return {competitor_id: CollegeCompetitor|ProCompetitor model row}.

    Returning the full ORM row (not just the strathmark_id) lets the caller
    read both `strathmark_id` AND `gender` for the CompetitorRecord, which
    STRATHMARK uses as ML feature #11 in its prediction cascade.
    """
    lookup: dict[int, Any] = {}
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
        lookup[row.id] = row
    return lookup


def _pull_global_results_df():
    """Pull all results from the global STRATHMARK Supabase DB.

    Returns the DataFrame on success, or None if STRATHMARK is unavailable
    or the call fails.  We never raise — a missing global df just means
    the predictor falls back to per-record history (still useful) and
    panel-mark fallback for competitors with no history.
    """
    try:
        from strathmark.db import pull_results  # type: ignore[import]
        df = pull_results()
        if df is None or df.empty:
            logger.info('mark_assignment: STRATHMARK pull_results returned empty')
            return None
        logger.info('mark_assignment: pulled %d historical results from STRATHMARK', len(df))
        return df
    except ImportError:
        logger.warning('mark_assignment: strathmark.db not installed — predictions will use per-record history only')
        return None
    except Exception as exc:
        logger.warning('mark_assignment: pull_results() failed: %s', exc)
        return None


def _build_competitor_records(
    results,
    competitor_models: dict[int, Any],
    global_results_df,
    event_code: str,
) -> tuple[list, dict]:
    """Build STRATHMARK CompetitorRecord objects with history + division + gender.

    Returns (records, result_by_name) where result_by_name maps
    record.name → EventResult so the caller can write marks back.
    """
    try:
        from strathmark.predictor import CompetitorRecord  # type: ignore[import]
    except ImportError:
        logger.warning('mark_assignment: strathmark.predictor not available')
        return [], {}

    records = []
    result_by_name: dict = {}

    for r in results:
        comp = competitor_models.get(r.competitor_id)
        sm_id = getattr(comp, 'strathmark_id', None) if comp else None
        gender = getattr(comp, 'gender', None) if comp else None
        name = r.competitor_name or f'competitor_{r.competitor_id}'

        history = _build_history_for_competitor(
            strathmark_id=sm_id,
            global_results_df=global_results_df,
        )

        record = CompetitorRecord(
            name=name,
            history=history,
            division=_division_from_gender(gender),
            gender=gender if gender in ('M', 'F') else None,
        )
        records.append(record)
        result_by_name[name] = r

    return records, result_by_name


def _build_history_for_competitor(
    strathmark_id: Optional[str],
    global_results_df,
) -> list:
    """Filter global results_df to one competitor and convert rows to
    HistoricalResult objects.

    Returns an empty list on any failure (no strathmark_id, no global df,
    no matching rows, import error).
    """
    if not strathmark_id or global_results_df is None:
        return []

    try:
        from strathmark.predictor import HistoricalResult  # type: ignore[import]
    except ImportError:
        return []

    # pull_results() returns supabase column names — competitor_id (lower),
    # 'Event', 'Time (seconds)', 'Size (mm)', 'Species Code',
    # 'Date (optional)', etc.  See strathmark/db.py::pull_results().
    df = global_results_df
    if 'competitor_id' not in df.columns:
        return []

    rows = df[df['competitor_id'] == strathmark_id]
    if rows.empty:
        return []

    history = []
    for _, row in rows.iterrows():
        try:
            event = str(row.get('Event', '')).strip().upper()
            if event not in ('SB', 'UH'):
                continue
            time_seconds = row.get('Time (seconds)')
            if time_seconds is None:
                continue
            time_seconds = float(time_seconds)
            if time_seconds <= 0:
                continue
            size_mm = row.get('Size (mm)')
            diameter_mm = float(size_mm) if size_mm is not None else 300.0
            species_code = row.get('Species Code') or ''
            species = _species_from_code(str(species_code)) or 'eastern white pine'

            result_date = row.get('Date (optional)')
            # pandas may surface NaT — convert to None
            if result_date is not None:
                try:
                    import pandas as _pd
                    if _pd.isna(result_date):
                        result_date = None
                    else:
                        # Coerce datetime → date
                        if hasattr(result_date, 'date') and callable(result_date.date):
                            result_date = result_date.date()
                except Exception:
                    pass

            history.append(HistoricalResult(
                event_code=event,
                time_seconds=time_seconds,
                species=species,
                diameter_mm=diameter_mm,
                quality=5,  # global results don't carry per-row quality
                result_date=result_date,
            ))
        except (TypeError, ValueError) as exc:
            logger.debug('mark_assignment: dropping malformed history row: %s', exc)
            continue

    return history


def _species_from_code(species_code: str) -> Optional[str]:
    """Map a STRATHMARK species code (S01..S13) to a display species name.

    The wood properties table is keyed by display name, so the predictor's
    species lookup needs the display string rather than the code.
    """
    if not species_code:
        return None
    from services.strathmark_wood_data import WOOD_TABLE_ROWS
    for row in WOOD_TABLE_ROWS:
        if row.get('speciesID') == species_code:
            return row.get('species')
    return None


def _division_from_gender(gender: Optional[str]) -> str:
    """Map a competitor's gender to a STRATHMARK panel-fallback division.

    STRATHMARK's panel-mark fallback recognizes 'Open', 'Novice', 'Junior',
    'Veterans', 'Womens'.  This app doesn't track novice/junior/veteran
    status, so we use the simplest gender-aware mapping:
        M → 'Open'
        F → 'Womens'
        unknown → 'Open' (most common case)
    """
    if gender == 'F':
        return 'Womens'
    return 'Open'


def _get_handicap_calculator(
    event_ceiling: int = None,
    global_results_df=None,
):
    """Attempt to instantiate the STRATHMARK HandicapCalculator with full
    wood + results context.

    Returns the calculator object on success, None on any failure.
    The STRATHMARK package is optional — if not installed or misconfigured,
    this returns None gracefully.

    HandicapCalculator.__init__ accepts:
      event_ceiling (Optional[int]), ollama_url (str),
      wood_df (Optional[DataFrame]), results_df (Optional[DataFrame])

    We always pass wood_df (inlined from `services/strathmark_wood_data`)
    so species hardness lookups work.  We pass results_df when available
    so the predictor cascade has cross-competitor data for baseline
    shrinkage, species affinity, and (if the [ml] extra is installed)
    ML training.
    """
    try:
        import os
        from strathmark.calculator import HandicapCalculator  # type: ignore[import]

        from services.strathmark_wood_data import get_wood_dataframe
        wood_df = get_wood_dataframe()

        ollama_url = os.environ.get('STRATHMARK_OLLAMA_URL', 'http://localhost:11434')
        kwargs: dict = {
            'ollama_url': ollama_url,
            'wood_df': wood_df,
        }
        if event_ceiling is not None:
            kwargs['event_ceiling'] = event_ceiling
        if global_results_df is not None:
            kwargs['results_df'] = global_results_df

        return HandicapCalculator(**kwargs)
    except ImportError:
        logger.warning('mark_assignment: strathmark package not installed — mark assignment unavailable')
        return None
    except Exception as exc:
        logger.error('mark_assignment: failed to create HandicapCalculator: %s', exc)
        return None


def _build_wood_profile(event: Event):
    """Build a STRATHMARK WoodProfile from the event's tournament WoodConfig.

    Returns a WoodProfile on success, or None when no WoodConfig exists for
    this event (Bug 3 fix — was previously a silent Pine 300mm fallback that
    masked setup errors).  The route is responsible for surfacing the missing
    config to the judge.
    """
    try:
        from strathmark.predictor import WoodProfile  # type: ignore[import]
    except ImportError:
        logger.warning('mark_assignment: strathmark.predictor not available')
        return None

    try:
        from models.wood_config import WoodConfig
    except ImportError:
        logger.warning('mark_assignment: WoodConfig model not importable')
        return None

    try:
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
            return None

        diameter_mm = float(wc.size_value)
        if wc.size_unit == 'in':
            diameter_mm = diameter_mm * 25.4

        species = wc.species or 'eastern white pine'
        return WoodProfile(
            species=species,
            diameter_mm=diameter_mm,
            quality=5,  # WoodConfig doesn't carry quality; assume reference 5
        )
    except Exception as exc:
        logger.warning('mark_assignment: failed to build WoodProfile: %s', exc)
        return None


# ---------------------------------------------------------------------------
# CSV upload — offline pre-computed marks
# ---------------------------------------------------------------------------
#
# Workflow this enables:
#   1. Judge runs STRATHMARK locally on a laptop with full Ollama + Gemini
#      access — picks up the most accurate marks the cascade can produce.
#   2. Judge exports a CSV (competitor_name,proposed_mark) and uploads it
#      to the deployed Pro-Am Manager on Railway.
#   3. Route renders a preview table; judge can override individual marks;
#      confirm writes them to EventResult.handicap_factor.
#
# This is the race-day safety net for when Railway can't reach Ollama AND
# Gemini is unset / quota'd / blocked.

# Required column variants we accept (case-insensitive header match).
_CSV_NAME_COLUMNS = ('competitor_name', 'name', 'competitor')
_CSV_ID_COLUMNS = ('competitor_id', 'id')
_CSV_MARK_COLUMNS = ('proposed_mark', 'mark', 'start_mark', 'start_mark_seconds')


def parse_marks_csv(file_storage, results: list) -> tuple[list[dict], list[str]]:
    """Parse a marks CSV upload and match each row against the event's results.

    Args:
        file_storage: Werkzeug FileStorage from request.files (or any object
                      with a .read() method returning bytes/str).
        results: list of EventResult rows for the current event — used to
                 match by competitor_id or competitor_name.

    Returns:
        (preview_rows, errors) where:
          preview_rows = [
              {
                  'matched_result_id': int | None,
                  'competitor_name': str,         # matched name (or CSV name if no match)
                  'proposed_mark': float | None,  # parsed seconds; None if invalid
                  'warning': str | None,          # row-level warning, e.g. 'unknown', 'ambiguous'
                  'csv_name': str,                # original CSV cell, for display
              },
              ...
          ]
          errors = top-level parse failures (file unreadable, missing columns)

    Matching policy (Warn-in-preview, leave unfilled):
        - If competitor_id is supplied and matches a result row in this event → use it.
        - Otherwise case-insensitive whitespace-normalised name match.
        - Zero matches → row warning 'unknown', no result_id assigned.
        - Multiple matches → row warning 'ambiguous', no result_id assigned.
        - Bad numeric mark → row warning, mark left None.
        - The judge must resolve all warnings manually before confirming.
    """
    errors: list[str] = []
    preview_rows: list[dict] = []

    if file_storage is None:
        errors.append('No file uploaded.')
        return preview_rows, errors

    # Read raw bytes — file_storage may be a Werkzeug FileStorage or a buffer.
    try:
        raw = file_storage.read()
    except Exception as exc:
        errors.append(f'Could not read uploaded file: {exc}')
        return preview_rows, errors

    if not raw:
        errors.append('Uploaded file is empty.')
        return preview_rows, errors

    if isinstance(raw, bytes):
        # Try utf-8 first, then latin-1 as a permissive fallback.
        try:
            text = raw.decode('utf-8-sig')
        except UnicodeDecodeError:
            try:
                text = raw.decode('latin-1')
            except Exception as exc:
                errors.append(f'Could not decode CSV: {exc}')
                return preview_rows, errors
    else:
        text = raw

    try:
        reader = csv.DictReader(io.StringIO(text))
        fieldnames = [(f or '').strip().lower() for f in (reader.fieldnames or [])]
    except Exception as exc:
        errors.append(f'CSV parse error: {exc}')
        return preview_rows, errors

    if not fieldnames:
        errors.append('CSV is missing a header row.')
        return preview_rows, errors

    # Locate columns by tolerant header match
    name_col = next((c for c in _CSV_NAME_COLUMNS if c in fieldnames), None)
    id_col = next((c for c in _CSV_ID_COLUMNS if c in fieldnames), None)
    mark_col = next((c for c in _CSV_MARK_COLUMNS if c in fieldnames), None)

    if not mark_col:
        errors.append(
            'CSV must have a "proposed_mark" column (also accepts: mark, start_mark, '
            'start_mark_seconds).'
        )
        return preview_rows, errors

    if not name_col and not id_col:
        errors.append(
            'CSV must have either a "competitor_name" or "competitor_id" column '
            '(also accepts: name, competitor, id).'
        )
        return preview_rows, errors

    # Build lookups against the event's competitors.
    by_id: dict[int, object] = {r.competitor_id: r for r in results}
    by_name: dict[str, list] = {}
    for r in results:
        key = _normalise_name(r.competitor_name)
        by_name.setdefault(key, []).append(r)

    for raw_row in reader:
        # csv.DictReader keys preserve original header case — re-normalise to lookup.
        row = {(k or '').strip().lower(): (v or '').strip() for k, v in raw_row.items()}

        csv_name_value = row.get(name_col, '') if name_col else ''
        csv_id_value = row.get(id_col, '') if id_col else ''
        mark_raw = row.get(mark_col, '')

        # Skip totally blank lines silently
        if not csv_name_value and not csv_id_value and not mark_raw:
            continue

        matched_result = None
        warning: Optional[str] = None
        display_name = csv_name_value

        # Prefer ID match when an ID column is provided and parses
        if id_col and csv_id_value:
            try:
                cid = int(csv_id_value)
                matched_result = by_id.get(cid)
                if matched_result is None:
                    warning = f'competitor_id {cid} is not in this event'
                else:
                    display_name = matched_result.competitor_name
            except (TypeError, ValueError):
                warning = f'competitor_id {csv_id_value!r} is not an integer'

        # Fall back to name match when ID didn't match (or wasn't supplied)
        if matched_result is None and not warning and csv_name_value:
            candidates = by_name.get(_normalise_name(csv_name_value), [])
            if len(candidates) == 1:
                matched_result = candidates[0]
                display_name = matched_result.competitor_name
            elif len(candidates) == 0:
                warning = 'no competitor with this name in this event'
            else:
                warning = f'name matches {len(candidates)} competitors — please disambiguate'

        if matched_result is None and not warning:
            warning = 'no competitor_name or competitor_id supplied for this row'

        # Parse the mark value (allowed to fail per row)
        proposed_mark: Optional[float] = None
        if mark_raw:
            try:
                proposed_mark = float(mark_raw)
                if proposed_mark < 0:
                    warning = (warning + '; ' if warning else '') + 'negative mark — clamping to 0'
                    proposed_mark = 0.0
            except (TypeError, ValueError):
                warning = (warning + '; ' if warning else '') + f'invalid mark {mark_raw!r}'
                proposed_mark = None
        else:
            warning = (warning + '; ' if warning else '') + 'mark cell is blank'

        preview_rows.append({
            'matched_result_id': matched_result.id if matched_result is not None else None,
            'competitor_name': display_name or csv_name_value or csv_id_value,
            'proposed_mark': proposed_mark,
            'warning': warning,
            'csv_name': csv_name_value or csv_id_value,
        })

    if not preview_rows:
        errors.append('CSV contained no data rows.')

    return preview_rows, errors


def _normalise_name(name: Optional[str]) -> str:
    """Whitespace + case normalisation for tolerant name matching."""
    if not name:
        return ''
    return ' '.join(name.split()).lower()
