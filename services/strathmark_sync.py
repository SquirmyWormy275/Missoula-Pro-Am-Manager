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

    try:
        import pandas as pd
        from strathmark import push_results
        df = pd.DataFrame(rows)
        count = push_results(df, show_name=SHOW_NAME, source_app=SOURCE_APP)
        _write_sync_cache(datetime.utcnow().isoformat(), count)
        logger.info(
            'STRATHMARK: pushed %d pro result(s) for %s', count, event.display_name
        )
    except Exception as exc:
        logger.error(
            'STRATHMARK: push_results failed for %s: %s.  '
            'Rows for manual retry: %r',
            event.display_name, exc, rows,
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
                log_skipped_competitor(result.competitor_name, event.display_name)
                logger.info(
                    'STRATHMARK: no global match for college competitor %s; skipped. '
                    'Manually enroll them to capture this result.',
                    result.competitor_name,
                )
                continue

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

    try:
        import pandas as pd
        from strathmark import push_results
        df = pd.DataFrame(rows)
        count = push_results(df, show_name=SHOW_NAME, source_app=SOURCE_APP)
        _write_sync_cache(datetime.utcnow().isoformat(), count)
        logger.info(
            'STRATHMARK: pushed %d college result(s) for %s', count, event.display_name
        )
    except Exception as exc:
        logger.error(
            'STRATHMARK: push_results failed for college %s: %s.  '
            'Rows for manual retry: %r',
            event.display_name, exc, rows,
        )
