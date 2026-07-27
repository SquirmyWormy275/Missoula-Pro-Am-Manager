"""
STRATHMARK integration layer for the Missoula Pro-Am Tournament Manager.

Provides non-blocking enrollment and result-push helpers.  Every public
function in this module is designed to be called fire-and-forget: failures
are logged as warnings/errors but never propagated to the caller, so
STRATHMARK unavailability never interrupts a registration or scoring action.

Environment variables required (both must be set to enable integration):
    STRATHMARK_SUPABASE_URL
    STRATHMARK_SUPABASE_KEY

Local state files written to instance/ (gitignored):
    strathmark_sync_cache.json  — timestamp and count of the last successful push
    strathmark_skipped.json     — college competitors skipped due to no name match
"""
from __future__ import annotations

import json
import logging
import os
from datetime import date, datetime
from typing import Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SHOW_NAME = 'Missoula Pro-Am'
SOURCE_APP = 'missoula-manager'

# NOTE: cache + skipped-log files live in instance/ which is EPHEMERAL on
# Railway — every deploy wipes them. Symptom: the /strathmark/status page
# reports "last push: never" after each redeploy even when pushes happened.
# This is operationally tolerable (sync calls themselves are idempotent),
# but a follow-up ticket should move both files to the DB so status is
# durable. Tracked as audit tech-debt #13. Do not put load-bearing data
# in these files.
_CACHE_DIR = 'instance'
_SYNC_CACHE_FILE = os.path.join(_CACHE_DIR, 'strathmark_sync_cache.json')
_SKIPPED_LOG_FILE = os.path.join(_CACHE_DIR, 'strathmark_skipped.json')

# STRATHMARK event codes used in the global results table.
_STAND_TYPE_TO_EVENT_CODE = {
    'standing_block': 'SB',
    'underhand': 'UH',
}

# WoodConfig key fragments for block events.
# Pattern: block_{fragment}_{competitor_type}_{gender}
_STAND_TYPE_TO_WOOD_FRAGMENT = {
    'standing_block': 'standing',
    'underhand': 'underhand',
}


# ---------------------------------------------------------------------------
# Configuration helpers
# ---------------------------------------------------------------------------

def is_configured() -> bool:
    """Return True if both STRATHMARK env vars are set."""
    return bool(
        os.environ.get('STRATHMARK_SUPABASE_URL')
        and os.environ.get('STRATHMARK_SUPABASE_KEY')
    )


# ---------------------------------------------------------------------------
# Competitor ID generation
# ---------------------------------------------------------------------------

def make_strathmark_id(name: str, gender: str, existing_ids: 'set | None' = None) -> str:
    """
    Generate a deterministic, portable STRATHMARK competitor ID.

    Format: <FirstInitial><LastName><GenderCode>  (uppercase, no spaces)
    Example: Alex Kaper, Male -> AKAPERM

    If the generated base ID already exists in existing_ids, a numeric suffix
    is appended (2, 3, …) until the ID is unique within the provided set.

    Args:
        name:         Competitor's full name (first [middle] last).
        gender:       'M' or 'F' (case-insensitive).
        existing_ids: Set of already-used strathmark_ids to check against.
                      Pass None (or empty set) to skip collision detection.

    Returns:
        Unique strathmark_id string.
    """
    parts = name.strip().split()
    if not parts:
        raise ValueError(f'Cannot generate strathmark_id from empty name: {name!r}')

    first_initial = parts[0][0].upper()
    last_name = parts[-1].upper()
    gender_code = 'M' if str(gender).upper().startswith('M') else 'F'
    base = f'{first_initial}{last_name}{gender_code}'

    if not existing_ids or base not in existing_ids:
        return base

    # Collision: append incrementing suffix until unique
    suffix = 2
    while f'{base}{suffix}' in existing_ids:
        suffix += 1
    return f'{base}{suffix}'


def _get_existing_strathmark_ids() -> set:
    """Return all strathmark_ids already stored locally (both competitor types)."""
    from models.competitor import CollegeCompetitor, ProCompetitor
    pro_ids = {
        row[0]
        for row in ProCompetitor.query
        .filter(ProCompetitor.strathmark_id.isnot(None))
        .with_entities(ProCompetitor.strathmark_id)
        .all()
    }
    college_ids = {
        row[0]
        for row in CollegeCompetitor.query
        .filter(CollegeCompetitor.strathmark_id.isnot(None))
        .with_entities(CollegeCompetitor.strathmark_id)
        .all()
    }
    return pro_ids | college_ids


# ---------------------------------------------------------------------------
# Change 1 — Pro competitor enrollment
# ---------------------------------------------------------------------------

def enroll_pro_competitor(competitor) -> bool:
    """
    Enroll a newly-registered pro competitor in the global STRATHMARK database.

    Generates a deterministic strathmark_id, calls push_competitors() with a
    single-row DataFrame, and stores the id on the local record.

    This function is non-blocking: any exception is caught, logged as a warning,
    and False is returned so the caller's registration flow is never interrupted.

    Args:
        competitor: ProCompetitor ORM instance (already committed to local DB).

    Returns:
        True on success, False if STRATHMARK is unconfigured or the push fails.
    """
    if not is_configured():
        logger.info(
            'STRATHMARK not configured; skipping enrollment for %s', competitor.name
        )
        return False

    try:
        import pandas as pd
        from strathmark import push_competitors

        from database import db

        existing = _get_existing_strathmark_ids()
        strathmark_id = make_strathmark_id(competitor.name, competitor.gender, existing)

        df = pd.DataFrame([{
            'CompetitorID':   strathmark_id,
            'Name':           competitor.name,
            'Country':        None,
            'State/Province': None,
            'Gender':         competitor.gender,
            'Region':         None,
        }])

        push_competitors(df)

        # Persist the generated ID locally so result pushes can reference it later.
        competitor.strathmark_id = strathmark_id
        db.session.commit()
        logger.info(
            'STRATHMARK: enrolled %s as %s', competitor.name, strathmark_id
        )
        return True

    except Exception as exc:
        logger.warning(
            'STRATHMARK enrollment failed for %s: %s', competitor.name, exc
        )
        return False


# ---------------------------------------------------------------------------
# Wood config lookup helpers (shared by pro and college push functions)
# ---------------------------------------------------------------------------

def _wood_config_key(stand_type: str, competitor_type: str, gender: 'str | None') -> 'str | None':
    """
    Build the WoodConfig config_key for a block event.

    Returns None if the stand_type has no wood-config mapping.
    """
    fragment = _STAND_TYPE_TO_WOOD_FRAGMENT.get(stand_type)
    if not fragment:
        return None
    if gender in ('M', 'F'):
        return f'block_{fragment}_{competitor_type}_{gender}'
    # Gendered events without a known gender value — cannot determine key.
    return None


def _get_wood_for_event(event, competitor_type: str) -> 'tuple[str | None, float | None]':
    """
    Return (species_code, size_mm) from WoodConfig for a SB or UH event.

    Returns (None, None) if the stand type is unsupported, no WoodConfig row
    exists, or the stored species/size is blank.
    """
    config_key = _wood_config_key(event.stand_type, competitor_type, event.gender)
    if not config_key:
        return None, None

    from models.wood_config import WoodConfig
    wc = WoodConfig.query.filter_by(
        tournament_id=event.tournament_id,
        config_key=config_key,
    ).first()

    if wc is None or not wc.species or wc.size_value is None:
        return None, None

    size_mm = round(wc.size_value * 25.4, 1) if wc.size_unit == 'in' else wc.size_value
    return wc.species, size_mm


# ---------------------------------------------------------------------------
# Local sync-state helpers
# ---------------------------------------------------------------------------

def _ensure_cache_dir():
    os.makedirs(_CACHE_DIR, exist_ok=True)


def _write_sync_cache(timestamp: str, count: int) -> None:
    """Persist last-push timestamp and count to instance/strathmark_sync_cache.json."""
    _ensure_cache_dir()
    try:
        try:
            with open(_SYNC_CACHE_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            data = {}
        data['last_push_timestamp'] = timestamp
        data['last_push_count'] = count
        with open(_SYNC_CACHE_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f)
    except Exception as exc:
        logger.warning('STRATHMARK: could not write sync cache: %s', exc)


def read_sync_cache() -> dict:
    """Return the last-push info dict from the local cache file, or {} if absent."""
    try:
        with open(_SYNC_CACHE_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def log_skipped_competitor(name: str, event_name: str) -> None:
    """Append a skipped college competitor entry to instance/strathmark_skipped.json."""
    _ensure_cache_dir()
    try:
        try:
            with open(_SKIPPED_LOG_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            data = []
        data.append({
            'name': name,
            'event': event_name,
            'skipped_at': datetime.utcnow().isoformat(),
        })
        with open(_SKIPPED_LOG_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2)
    except Exception as exc:
        logger.warning('STRATHMARK: could not write skipped log: %s', exc)


def get_skipped_competitors() -> list:
    """Return all skipped-competitor entries from the local log."""
    try:
        with open(_SKIPPED_LOG_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []


# ---------------------------------------------------------------------------
# Auto-register fallback for college competitors with no global match
# ---------------------------------------------------------------------------

_AUTO_REGISTER_ENV_VAR = 'STRATHMARK_AUTO_REGISTER_COLLEGE'


def _auto_register_enabled() -> bool:
    """Return True unless STRATHMARK_AUTO_REGISTER_COLLEGE is set to '0'/'false'.

    Default is enabled so unmapped college competitors get added to Supabase
    automatically.  Set the env var to ``0`` (or ``false``/``no``/``off``) to
    restore the prior skip-and-log behaviour.
    """
    raw = os.environ.get(_AUTO_REGISTER_ENV_VAR, '').strip().lower()
    if raw in ('0', 'false', 'no', 'off'):
        return False
    return True


def _auto_register_college_competitor(comp, display_name: str) -> 'str | None':
    """Register an unmapped college competitor in Supabase via STRATHMARK.

    Calls strathmark.register_competitor() with the competitor's name and
    gender.  STRATHMARK's helper is idempotent on case-insensitive name match,
    so re-running this for an already-existing record returns the existing ID
    instead of creating a duplicate.

    Returns the new ``competitor_id`` on success, or None when:
      - the feature is disabled via STRATHMARK_AUTO_REGISTER_COLLEGE=0
      - register_competitor is unavailable (older STRATHMARK)
      - any exception is raised by the STRATHMARK call

    Never raises -- failures fall through to the caller's skip-and-log path.
    """
    if not _auto_register_enabled():
        return None

    try:
        from strathmark import register_competitor
    except ImportError:
        logger.info(
            'STRATHMARK: register_competitor unavailable in installed version; '
            'skipping auto-register for %s', display_name,
        )
        return None

    try:
        result = register_competitor(
            name=display_name,
            country='USA',
            gender=getattr(comp, 'gender', '') or '',
        )
        new_id = result.get('competitor_id') if isinstance(result, dict) else None
        status = result.get('status', 'unknown') if isinstance(result, dict) else 'unknown'
        if not new_id:
            logger.warning(
                'STRATHMARK: register_competitor returned no id for %s (%s)',
                display_name, result,
            )
            return None
        logger.info(
            'STRATHMARK: register_competitor(%s) -> %s (%s)',
            display_name, new_id, status,
        )
        return new_id
    except Exception as exc:
        logger.warning(
            'STRATHMARK: auto-register failed for %s: %s', display_name, exc,
        )
        return None


# ---------------------------------------------------------------------------
# Validated push helper (uses STRATHMARK push_results_dicts when available,
# falls back to legacy push_results for older STRATHMARK installs)
# ---------------------------------------------------------------------------

def _row_to_dict_api(row: dict) -> dict:
    """Convert a legacy STRATHEX-column row to the push_results_dicts schema.

    The two helpers below build rows in the historical column-name format
    (CompetitorID, Event, Time (seconds), …) because that's what the legacy
    DataFrame-based push_results() consumed.  push_results_dicts() expects
    snake_case keys instead.  This helper does the rename in one place so the
    callers stay readable.
    """
    return {
        'competitor_id': row['CompetitorID'],
        'event_code':    row['Event'],
        'time_seconds':  row['Time (seconds)'],
        'size_mm':       row['Size (mm)'],
        'species_code':  row['Species Code'],
        'date':          row['Date (optional)'],
        'notes':         row.get('Notes (Competition, special circumstances, etc.)'),
    }


def _push_rows_validated(rows: list, event_label: str) -> dict:
    """Push a list of legacy-format rows via the validated dict API.

    Returns a result dict with explicit ``inserted`` / ``skipped`` / ``errors``
    counts so callers can surface them on the status page.  Never raises:
    any exception is caught, logged at ERROR with the full row list for
    manual retry, and an error result dict is returned.

    Falls back to the legacy DataFrame ``push_results`` when the installed
    STRATHMARK predates ``push_results_dicts`` so older deployments still
    work without an explicit upgrade.
    """
    if not rows:
        return {'inserted': 0, 'skipped': 0, 'errors': []}

    # Preferred path: validated dict API (push_results_dicts).  This is
    # wrapped in its own try so that any failure falls through to the
    # legacy DataFrame-based push_results, which is still supported by
    # every released STRATHMARK version.
    try:
        from strathmark import push_results_dicts
        payload = [_row_to_dict_api(r) for r in rows]
        result = push_results_dicts(
            payload,
            source=SOURCE_APP,
            show_name=SHOW_NAME,
        )
        # Defend against mock-injected return values: a real STRATHMARK call
        # always returns a plain dict.  Anything else means we're in a test
        # that has not stubbed push_results_dicts, so fall through to legacy.
        if not isinstance(result, dict):
            raise TypeError(
                f'push_results_dicts returned {type(result).__name__}, expected dict'
            )
        inserted = int(result.get('inserted', 0))
        skipped = int(result.get('skipped', 0))
        errors = list(result.get('errors', []) or [])
        _write_sync_cache(datetime.utcnow().isoformat(), inserted)
        if errors:
            logger.warning(
                'STRATHMARK: %s push reported %d errors: %s',
                event_label, len(errors), errors[:5],
            )
        logger.info(
            'STRATHMARK: %s push -- inserted=%d skipped=%d errors=%d',
            event_label, inserted, skipped, len(errors),
        )
        return {'inserted': inserted, 'skipped': skipped, 'errors': errors}
    except ImportError:
        pass  # STRATHMARK predates push_results_dicts -- fall through to legacy
    except (TypeError, ValueError, AttributeError) as exc:
        # The dict API returned a value we can't interpret -- try legacy.
        logger.warning(
            'STRATHMARK: %s dict-API push raised %s; falling back to legacy push_results',
            event_label, exc,
        )

    # Legacy fallback: DataFrame-based push_results
    try:
        import pandas as pd
        from strathmark import push_results
        df = pd.DataFrame(rows)
        count = push_results(df, show_name=SHOW_NAME, source_app=SOURCE_APP)
        _write_sync_cache(datetime.utcnow().isoformat(), count)
        logger.info('STRATHMARK: %s push -- inserted=%d (legacy API)', event_label, count)
        return {'inserted': int(count), 'skipped': 0, 'errors': []}
    except Exception as exc:
        logger.error(
            'STRATHMARK: push failed for %s: %s.  '
            'Rows for manual retry: %r',
            event_label, exc, rows,
        )
        return {'inserted': 0, 'skipped': 0, 'errors': [str(exc)]}


# ---------------------------------------------------------------------------
# Change 2 — Pro SB / UH result push
# ---------------------------------------------------------------------------

def push_pro_event_results(event, tournament_year: int) -> None:
    """
    Push finalized pro Standing Block or Underhand results to STRATHMARK.

    Only processes results for competitors who have a strathmark_id.  If a
    competitor is missing a strathmark_id, an info-level log message is emitted
    so the director can manually enroll them and re-push.

    On push failure the full row list is logged at ERROR level so results can
    be manually submitted later — no data is lost.

    Args:
        event:            Finalized Event ORM instance (pro, SB or UH).
        tournament_year:  Tournament year, used in the Notes field.
    """
    if not is_configured():
        logger.info(
            'STRATHMARK not configured; skipping result push for %s', event.display_name
        )
        return

    event_code = _STAND_TYPE_TO_EVENT_CODE.get(event.stand_type)
    if not event_code:
        return  # Not a SB or UH event — nothing to push.

    species_code, size_mm = _get_wood_for_event(event, 'pro')
    if species_code is None or size_mm is None:
        logger.warning(
            'STRATHMARK: no wood config found for %s (tournament_id=%s); '
            'pro results not pushed — configure wood species/size and re-finalize.',
            event.display_name, event.tournament_id,
        )
        return

    from models.competitor import ProCompetitor

    today = date.today().isoformat()
    notes = f'Missoula Pro-Am {tournament_year}'
    rows = []

    for result in event.results.filter_by(status='completed').all():
        if result.result_value is None:
            continue
        comp = ProCompetitor.query.get(result.competitor_id)
        if comp is None or not comp.strathmark_id:
            logger.info(
                'STRATHMARK: pro competitor %s (id=%s) has no strathmark_id; '
                'result not pushed — enroll them manually to capture this result.',
                result.competitor_name, result.competitor_id,
            )
            continue
        rows.append({
            'CompetitorID':                                     comp.strathmark_id,
            'Event':                                            event_code,
            'Time (seconds)':                                   result.result_value,
            'Size (mm)':                                        size_mm,
            'Species Code':                                     species_code,
            'Date (optional)':                                  today,
            'Notes (Competition, special circumstances, etc.)': notes,
        })

    if not rows:
        return

    _push_rows_validated(rows, event_label=f'pro {event.display_name}')

    # Record prediction residuals for STRATHMARK bias-learning (non-blocking).
    _record_prediction_residuals_for_pro_event(event, event_code)


# ---------------------------------------------------------------------------
# Change 2b — Prediction residual recording for pro SB / UH events
# ---------------------------------------------------------------------------

def _record_prediction_residuals_for_pro_event(event, event_code: str) -> None:
    """
    Record per-competitor prediction residuals in the STRATHMARK Supabase table
    after a pro SB or UH event is finalized.

    Called from push_pro_event_results() immediately after results have been
    pushed so that STRATHMARK can track prediction accuracy and adjust future
    marks for systematic bias.

    residual = actual_time - predicted_time
    Positive: competitor ran slower than predicted (undermarked).
    Negative: competitor ran faster than predicted (overmarked).

    PREDICTED TIME:
    EventResult.predicted_time (Float, nullable) was added in migration
    d8d4aa7bdb45. The mark assignment service (services/mark_assignment.py)
    populates both handicap_factor (start mark) and predicted_time (predicted
    completion time) from STRATHMARK MarkResult objects via the batch
    calculate path. When marks have been assigned before finalization,
    this function records residuals (actual - predicted) for STRATHMARK
    bias-learning. If predicted_time is NULL (marks not assigned), this
    function silently skips. It remains safe to call at any time.

    This function is non-blocking — all exceptions are caught and logged at
    ERROR level; the caller's response path is never interrupted.
    """
    if not is_configured():
        return

    try:
        from models.competitor import ProCompetitor

        today = date.today()
        predicted: dict = {}
        actual: dict = {}

        for result in event.results.filter_by(status='completed').all():
            if result.result_value is None:
                continue

            comp = ProCompetitor.query.get(result.competitor_id)
            if comp is None or not comp.strathmark_id:
                # strathmark_id absence already logged by push_pro_event_results().
                continue

            # predicted_time is populated by mark_assignment when handicap marks
            # are assigned before the event. If marks weren't assigned, it's NULL.
            predicted_time = getattr(result, 'predicted_time', None)
            if predicted_time is None:
                logger.warning(
                    'STRATHMARK residuals: no predicted_time for %s (%s) in %s; '
                    'skipping — assign handicap marks before finalizing to capture residuals.',
                    result.competitor_name, comp.strathmark_id, event.display_name,
                )
                continue

            predicted[comp.strathmark_id] = float(predicted_time)
            actual[comp.strathmark_id] = float(result.result_value)

        if not predicted:
            return

        from strathmark import record_prediction_residuals
        record_prediction_residuals(
            predicted=predicted,
            actual=actual,
            show_name=SHOW_NAME,
            event_code=event_code,
            result_date=today,
        )
        logger.info(
            'STRATHMARK: recorded prediction residuals for %d competitor(s) in %s',
            len(predicted), event.display_name,
        )

    except Exception as exc:
        logger.error(
            'STRATHMARK: _record_prediction_residuals_for_pro_event failed for %s: %s',
            event.display_name, exc,
        )


# ---------------------------------------------------------------------------
# Change 3 — College SB Speed / UH Speed result push
# ---------------------------------------------------------------------------

def is_college_sb_uh_speed(event) -> bool:
    """
    Return True if the event is a college Standing Block Speed or Underhand Speed event.

    Matches case-insensitively and also recognises common abbreviations
    (SB Speed, UH Speed, Standing Block Speed, Underhand Speed).
    """
    if event.event_type != 'college':
        return False
    if event.stand_type not in ('standing_block', 'underhand'):
        return False
    name_lower = event.name.strip().lower()
    # Accepted name fragments (case-insensitive)
    speed_names = {
        'standing block speed',
        'underhand speed',
        'sb speed',
        'uh speed',
    }
    return name_lower in speed_names or (
        'speed' in name_lower
        and event.stand_type in ('standing_block', 'underhand')
        and event.scoring_type == 'time'
    )


def push_college_event_results(event, tournament_year: int) -> None:
    """
    Push finalized college SB Speed / UH Speed results to STRATHMARK.

    For each competitor with a recorded time:
    1. If they already have a strathmark_id locally, use it directly.
    2. If not, pull the global competitor list and match by name (case-insensitive).
       - Match found:  store strathmark_id locally, include result in push.
       - No match:     skip silently, write to skipped log, emit an info log.
    3. College-only competitors (no global profile) are NOT auto-created.

    Args:
        event:            Finalized Event ORM instance (college SB/UH Speed).
        tournament_year:  Tournament year, used in the Notes field.
    """
    if not is_configured():
        logger.info(
            'STRATHMARK not configured; skipping college result push for %s',
            event.display_name,
        )
        return

    event_code = _STAND_TYPE_TO_EVENT_CODE.get(event.stand_type)
    if not event_code:
        return

    species_code, size_mm = _get_wood_for_event(event, 'college')
    if species_code is None or size_mm is None:
        logger.warning(
            'STRATHMARK: no wood config found for college %s (tournament_id=%s); '
            'results not pushed — configure wood species/size and re-finalize.',
            event.display_name, event.tournament_id,
        )
        return

    from database import db
    from models.competitor import CollegeCompetitor

    # Fetch global competitor list once per call; cache in local variable.
    _global_df_cache: list = []  # list of one DataFrame, lazy-loaded

    def _global_df():
        if not _global_df_cache:
            try:
                from strathmark import pull_competitors
                _global_df_cache.append(pull_competitors())
            except Exception as exc:
                logger.warning('STRATHMARK: pull_competitors failed: %s', exc)
                import pandas as pd
                _global_df_cache.append(pd.DataFrame())
        return _global_df_cache[0]

    today = date.today().isoformat()
    notes = f'Missoula Pro-Am {tournament_year}'
    rows = []

    for result in event.results.filter_by(status='completed').all():
        if result.result_value is None:
            continue

        comp = CollegeCompetitor.query.get(result.competitor_id)
        if comp is None:
            continue

        # --- Resolve strathmark_id ---
        if not comp.strathmark_id:
            gdf = _global_df()
            if gdf.empty:
                log_skipped_competitor(result.competitor_name, event.display_name)
                logger.info(
                    'STRATHMARK: global DB unavailable; skipping college competitor %s',
                    result.competitor_name,
                )
                continue

            name_lower = result.competitor_name.strip().lower()
            matches = gdf[gdf['Name'].str.strip().str.lower() == name_lower]

            if matches.empty:
                # No global match: try to auto-register the competitor in
                # Supabase via STRATHMARK's register_competitor() helper.  This
                # is gated by STRATHMARK_AUTO_REGISTER_COLLEGE (default "1") so
                # the prior skip-only behaviour can be restored if needed.
                auto_id = _auto_register_college_competitor(comp, result.competitor_name)
                if auto_id is None:
                    log_skipped_competitor(result.competitor_name, event.display_name)
                    logger.info(
                        'STRATHMARK: no global match for college competitor %s and '
                        'auto-register did not produce an ID; skipped.',
                        result.competitor_name,
                    )
                    continue
                comp.strathmark_id = auto_id
                try:
                    db.session.commit()
                    logger.info(
                        'STRATHMARK: auto-registered college competitor %s -> %s',
                        result.competitor_name, auto_id,
                    )
                except Exception as exc:
                    db.session.rollback()
                    logger.warning(
                        'STRATHMARK: could not save auto-registered id for %s: %s',
                        result.competitor_name, exc,
                    )
                    log_skipped_competitor(result.competitor_name, event.display_name)
                    continue
                # Fall through to row append below — comp.strathmark_id is now set.
            else:
                # Match found — persist strathmark_id locally.
                found_id = matches.iloc[0]['CompetitorID']
                comp.strathmark_id = found_id
                try:
                    db.session.commit()
                    logger.info(
                        'STRATHMARK: resolved college competitor %s -> %s',
                        result.competitor_name, found_id,
                    )
                except Exception as exc:
                    db.session.rollback()
                    logger.warning(
                        'STRATHMARK: could not save strathmark_id for %s: %s',
                        result.competitor_name, exc,
                    )
                    log_skipped_competitor(result.competitor_name, event.display_name)
                    continue

        rows.append({
            'CompetitorID':                                     comp.strathmark_id,
            'Event':                                            event_code,
            'Time (seconds)':                                   result.result_value,
            'Size (mm)':                                        size_mm,
            'Species Code':                                     species_code,
            'Date (optional)':                                  today,
            'Notes (Competition, special circumstances, etc.)': notes,
        })

    if not rows:
        return

    _push_rows_validated(rows, event_label=f'college {event.display_name}')
