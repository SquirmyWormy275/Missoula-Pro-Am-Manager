"""
Virtual Woodboss — material planning calculations.

Given enrolled competitor data, calculates:
  - Chopping blocks needed per event group (species, size, count)
  - Saw log material needed per saw event (linear inches or log count)

Formulas (per head judge specification):
  - Crosscut (Single Buck, Double Buck, Jack & Jill): 2" per cut
      Double Buck and Jack & Jill are partnered → 1 cut per pair
  - Stock Saw: 5" per cut (1 cut per competitor)
  - Hot Saw: 6.5" per cut (1 cut per competitor)
  - Obstacle Pole: 2" per competitor + 5" per every 7 competitors (timber lags)
      Uses log_op config (independent — does not share species/size with general log).
  - Cookie Stack: 1 log per 3 competitors (cookie logs, not linear footage)
      Uses log_cookie config (independent — does not share species/size with general log).
  - Chopping blocks: 1 block per enrolled competitor
  - Relay blocks: count_override (set manually — lottery-determined team count)
"""
import math
from collections import defaultdict

# ---------------------------------------------------------------------------
# Block event group definitions
# Maps (fragment_in_event_name, competitor_type, gender) -> config_key
# Fragment matching: event name (lowercased) must CONTAIN the fragment.
# Multiple fragments can map to the same config_key (counts accumulate).
# Relay entries (None type) are handled separately via count_override.
# ---------------------------------------------------------------------------
BLOCK_EVENT_GROUPS = [
    # (name_fragment, competitor_type, gender, config_key, display_label)
    ('underhand', 'college', 'M', 'block_underhand_college_M', 'Underhand — College Men'),
    ('underhand', 'college', 'F', 'block_underhand_college_F', 'Underhand — College Women'),
    ('underhand', 'pro',     'M', 'block_underhand_pro_M',     'Underhand — Pro Men'),
    ('underhand', 'pro',     'F', 'block_underhand_pro_F',     'Underhand — Pro Women'),
    ('standing block', 'college', 'M', 'block_standing_college_M', 'Standing Block — College Men'),
    ('standing block', 'college', 'F', 'block_standing_college_F', 'Standing Block — College Women'),
    ('standing block', 'pro',     'M', 'block_standing_pro_M',     'Standing Block — Pro Men'),
    ('standing block', 'pro',     'F', 'block_standing_pro_F',     'Standing Block — Pro Women'),
    ('springboard', 'college', 'M', 'block_springboard_college_M', 'Springboard — College Men'),
    ('springboard', 'college', 'F', 'block_springboard_college_F', 'Springboard — College Women'),
    ('1-board', 'college', 'M', 'block_springboard_college_M', 'Springboard — College Men'),
    ('1-board', 'college', 'F', 'block_springboard_college_F', 'Springboard — College Women'),
    ('one board', 'college', 'M', 'block_springboard_college_M', 'Springboard — College Men'),
    ('one board', 'college', 'F', 'block_springboard_college_F', 'Springboard — College Women'),
    ('2-board', 'college', 'M', 'block_springboard_college_M', 'Springboard — College Men'),
    ('2-board', 'college', 'F', 'block_springboard_college_F', 'Springboard — College Women'),
    ('2 board', 'college', 'M', 'block_springboard_college_M', 'Springboard — College Men'),
    ('2 board', 'college', 'F', 'block_springboard_college_F', 'Springboard — College Women'),
    ('two board', 'college', 'M', 'block_springboard_college_M', 'Springboard — College Men'),
    ('two board', 'college', 'F', 'block_springboard_college_F', 'Springboard — College Women'),
    # Pro springboard events — three distinct wood categories:
    #   Pro Springboard (2-board) → block_springboard_pro
    #   Pro 1-Board                → block_1board_pro
    #   3-Board Jigger             → block_3board_pro
    # All are open gender.
    ('2-board',      'pro', None, 'block_springboard_pro', 'Springboard (2-Board) — Pro'),
    ('2 board',      'pro', None, 'block_springboard_pro', 'Springboard (2-Board) — Pro'),
    ('two board',    'pro', None, 'block_springboard_pro', 'Springboard (2-Board) — Pro'),
    ('springboard',  'pro', None, 'block_springboard_pro', 'Springboard (2-Board) — Pro'),
    ('1-board',      'pro', None, 'block_1board_pro',      'Pro 1-Board'),
    ('one board',    'pro', None, 'block_1board_pro',      'Pro 1-Board'),
    ('3-board',      'pro', None, 'block_3board_pro',      '3-Board Jigger — Pro'),
    ('3 board',      'pro', None, 'block_3board_pro',      '3-Board Jigger — Pro'),
    ('three-board',  'pro', None, 'block_3board_pro',      '3-Board Jigger — Pro'),
    ('three board',  'pro', None, 'block_3board_pro',      '3-Board Jigger — Pro'),
]

# Relay block config_keys — no enrollment fragment matching; count comes from count_override
RELAY_BLOCK_KEYS = {'block_relay_underhand', 'block_relay_standing'}

# Ordered dict of all config_keys → human labels (includes relay entries)
# Ordered: College blocks first, then Pro blocks, then Relay.
# Templates use this order to render section headers.
BLOCK_CONFIG_LABELS = {
    # College
    'block_underhand_college_M':   'Underhand — College Men',
    'block_underhand_college_F':   'Underhand — College Women',
    'block_standing_college_M':    'Standing Block — College Men',
    'block_standing_college_F':    'Standing Block — College Women',
    'block_springboard_college_M': 'Springboard — College Men',
    'block_springboard_college_F': 'Springboard — College Women',
    # Pro
    'block_underhand_pro_M':       'Underhand — Pro Men',
    'block_underhand_pro_F':       'Underhand — Pro Women',
    'block_standing_pro_M':        'Standing Block — Pro Men',
    'block_standing_pro_F':        'Standing Block — Pro Women',
    'block_springboard_pro':       'Springboard (2-Board) — Pro',
    'block_1board_pro':            'Pro 1-Board',
    'block_3board_pro':            '3-Board Jigger — Pro',
    # Relay
    'block_relay_underhand':       'Pro-Am Relay — Underhand Butcher Block',
    'block_relay_standing':        'Pro-Am Relay — Standing Butcher Block',
}

# ---------------------------------------------------------------------------
# Saw event definitions
# Maps event name fragment -> (category, is_partnered)
# category: 'crosscut' | 'stocksaw' | 'hotsaw' | 'op' | 'cookie'
# ---------------------------------------------------------------------------
SAW_EVENTS = [
    # (name_fragment, category, is_partnered, display_label)
    ('single buck',   'crosscut', False, 'Single Buck'),
    ('double buck',   'crosscut', True,  'Double Buck'),
    ('jack & jill',   'crosscut', True,  'Jack & Jill Sawing'),
    ('hot saw',       'hotsaw',   False, 'Hot Saw'),
    ('stock saw',     'stocksaw', False, 'Stock Saw'),
    ('obstacle pole', 'op',       False, 'Obstacle Pole'),
    ('cookie stack',  'cookie',   False, 'Cookie Stack'),
]

# Log species config keys
LOG_GENERAL_KEY = 'log_general'
LOG_STOCK_KEY = 'log_stock'
LOG_OP_KEY = 'log_op'
LOG_COOKIE_KEY = 'log_cookie'
LOG_RELAY_DOUBLEBUCK_KEY = 'log_relay_doublebuck'


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _get_configs(tournament_id):
    """Return {config_key: WoodConfig} for the given tournament."""
    from models.wood_config import WoodConfig
    rows = WoodConfig.query.filter_by(tournament_id=tournament_id).all()
    return {row.config_key: row for row in rows}


def _active_block_keys(tournament_id):
    """
    Return the set of block config_keys that are actually in use for this
    tournament, derived from its Event records (not from enrollment counts,
    so this works before registration is open).

    An event "uses" a block key when its name contains a fragment from
    BLOCK_EVENT_GROUPS that maps to that key AND the (comp_type, gender)
    tuple matches. Pro 1-board / 3-board exclusivity rules mirror
    calculate_blocks() — if an event name explicitly names 1-board or
    3-board, the generic 'springboard' fragment is NOT also allowed to
    match (otherwise every pro 1-board would also ghost a 2-board row).

    Relay block keys are always included because they are manual and do
    not correspond to any event name.
    """
    from models.event import Event
    events = Event.query.filter_by(tournament_id=tournament_id).all()
    active = set(RELAY_BLOCK_KEYS)
    for event in events:
        event_lower = (event.name or '').lower().strip()
        if not event_lower:
            continue
        comp_type = event.event_type  # 'college' | 'pro'
        # event.gender may be None for open/mixed events
        event_gender = event.gender

        is_pro_one_board = comp_type == 'pro' and (
            '1-board' in event_lower or '1 board' in event_lower
            or 'one board' in event_lower or 'one-board' in event_lower
        )
        is_pro_three_board = comp_type == 'pro' and (
            '3-board' in event_lower or '3 board' in event_lower
            or 'three-board' in event_lower or 'three board' in event_lower
            or 'jigger' in event_lower
        )
        skip_pro_sb_fallback = is_pro_one_board or is_pro_three_board

        for (fragment, grp_type, grp_gender, cfg_key, _label) in BLOCK_EVENT_GROUPS:
            if fragment not in event_lower:
                continue
            if comp_type != grp_type:
                continue
            # College groups are gendered — need the event's gender to match.
            # Pro groups with grp_gender=None are open.
            if grp_gender is not None:
                if event_gender != grp_gender:
                    continue
            if skip_pro_sb_fallback and cfg_key == 'block_springboard_pro':
                continue
            active.add(cfg_key)
    return active


def prune_stale_block_configs(tournament_id):
    """
    Delete WoodConfig rows for block keys that this tournament's events
    do not use. Used as a one-shot cleanup for ghost rows planted by
    apply_preset / copy_from before the active-key gating was added.

    Log keys (log_general, log_stock, log_op, log_cookie,
    log_relay_doublebuck) are never touched — they're tournament-wide
    and not tied to a specific event.

    Returns the number of rows deleted.
    """
    from models.wood_config import WoodConfig
    from database import db

    active = _active_block_keys(tournament_id)
    # Only block_* keys are candidates. Log keys are never pruned here.
    all_block_keys = set(BLOCK_CONFIG_LABELS.keys())
    stale = all_block_keys - active
    if not stale:
        return 0
    deleted = (
        WoodConfig.query
        .filter(
            WoodConfig.tournament_id == tournament_id,
            WoodConfig.config_key.in_(stale),
        )
        .delete(synchronize_session=False)
    )
    db.session.commit()
    return deleted


def _get_pro_event_map(tournament_id):
    """
    Return (id_map, name_map) for all pro events in this tournament.

    id_map:   {str(event_id): Event}  — primary lookup for ID-based entries
    name_map: {display_name_lower: Event} — fallback for name-based entries
              (created by the Excel importer when gendered event names like
              "Women's Standing Block" don't resolve to an Event ID)
    """
    from models.event import Event
    rows = Event.query.filter_by(tournament_id=tournament_id, event_type='pro').all()
    id_map = {str(r.id): r for r in rows}
    name_map = {}
    for r in rows:
        name_map[r.name.strip().lower()] = r
        name_map[r.display_name.strip().lower()] = r
    return id_map, name_map


def _count_competitors(tournament_id):
    """
    Count enrolled competitors per (event_name_lower, competitor_type, gender).

    Returns a defaultdict(int) keyed by (event_name_lower, 'college'|'pro', 'M'|'F').

    College competitors store event names in events_entered (e.g. "Single Buck").
    Pro competitors store event IDs; these are resolved to event.name via the Event table.
    For gendered pro events (event.gender set), the event gender is used.
    For open/mixed pro events, the competitor's own gender is used.

    Fallback: if a pro entry is a name string rather than an ID (legacy imports),
    it is matched against Event.name and Event.display_name.
    """
    from models.competitor import CollegeCompetitor, ProCompetitor
    from models.event import Event

    counts = defaultdict(int)

    # Build college event lookup. College registration + the Excel importer
    # store event NAMES in events_entered (e.g. "Underhand Hard Hit"), not
    # IDs. Map by ID first (future-proof) then by name/display_name — mirrors
    # the pro lookup below. Prior version only mapped IDs and silently
    # dropped every college enrollment, zeroing all college block counts.
    college_events = Event.query.filter_by(
        tournament_id=tournament_id, event_type='college'
    ).all()
    college_id_map = {str(e.id): e for e in college_events}
    college_name_map = {}
    for e in college_events:
        college_name_map[e.name.strip().lower()] = e
        dn = getattr(e, 'display_name', None)
        if dn:
            college_name_map[dn.strip().lower()] = e

    college_comps = (
        CollegeCompetitor.query
        .filter_by(tournament_id=tournament_id, status='active')
        .all()
    )
    for comp in college_comps:
        gender = comp.gender or 'M'
        for event_entry in comp.get_events_entered():
            event_key = str(event_entry).strip()
            event = college_id_map.get(event_key)
            if not event:
                event = college_name_map.get(event_key.lower())
            if not event:
                continue
            key = (event.name.lower().strip(), 'college', gender)
            counts[key] += 1

    pro_id_map, pro_name_map = _get_pro_event_map(tournament_id)

    pro_comps = (
        ProCompetitor.query
        .filter_by(tournament_id=tournament_id, status='active')
        .all()
    )
    for comp in pro_comps:
        comp_gender = comp.gender or 'M'
        for event_id in comp.get_events_entered():
            event_key = str(event_id).strip()
            event = pro_id_map.get(event_key)
            if not event:
                # Fallback: entry may be a name string from Excel import
                event = pro_name_map.get(event_key.lower())
            if not event:
                continue
            # Use event gender if the event is gendered; else use competitor gender
            gender = event.gender or comp_gender
            key = (event.name.lower().strip(), 'pro', gender)
            counts[key] += 1

    return counts


def _list_competitors(tournament_id):
    """
    Return all active competitors with their enrolled events for the lottery view.

    Returns a list of dicts:
        {'name': str, 'affiliation': str, 'gender': str, 'comp_type': 'college'|'pro', 'events': [str]}

    Pro competitor events are resolved from IDs to event names.
    """
    from models.competitor import CollegeCompetitor, ProCompetitor
    from models.event import Event

    result = []

    # events_entered stores NAMES on both college and pro competitors.
    # Build id + name maps, try id first then name — same pattern as
    # _count_competitors.
    college_events = Event.query.filter_by(
        tournament_id=tournament_id, event_type='college'
    ).all()
    college_id_map = {str(e.id): e for e in college_events}
    college_name_map = {}
    for e in college_events:
        college_name_map[e.name.strip().lower()] = e
        dn = getattr(e, 'display_name', None)
        if dn:
            college_name_map[dn.strip().lower()] = e

    college_comps = (
        CollegeCompetitor.query
        .filter_by(tournament_id=tournament_id, status='active')
        .all()
    )
    for comp in college_comps:
        team_code = comp.team.team_code if comp.team else ''
        event_names = []
        for event_entry in comp.get_events_entered():
            event_key = str(event_entry).strip()
            event = college_id_map.get(event_key)
            if not event:
                event = college_name_map.get(event_key.lower())
            if event:
                event_names.append(event.name)
        result.append({
            'name': comp.name,
            'affiliation': team_code,
            'gender': comp.gender or 'M',
            'comp_type': 'college',
            'events': event_names,
        })

    pro_id_map, pro_name_map = _get_pro_event_map(tournament_id)

    pro_comps = (
        ProCompetitor.query
        .filter_by(tournament_id=tournament_id, status='active')
        .all()
    )
    for comp in pro_comps:
        # Resolve event IDs (or name strings from legacy imports) to event names
        event_names = []
        for event_id in comp.get_events_entered():
            event_key = str(event_id).strip()
            event = pro_id_map.get(event_key)
            if not event:
                event = pro_name_map.get(event_key.lower())
            if event:
                event_names.append(event.name)
        result.append({
            'name': comp.name,
            'affiliation': '',
            'gender': comp.gender or 'M',
            'comp_type': 'pro',
            'events': event_names,
        })

    return result


def _fmt_size(cfg):
    """Return a display size string from a WoodConfig (or None)."""
    if cfg is None or cfg.size_value is None:
        return None
    val = cfg.size_value
    display_val = int(val) if val == int(val) else val
    unit = '"' if cfg.size_unit == 'in' else ' mm'
    return f'{display_val}{unit}'


def generate_share_token(tournament_id, secret_key):
    """
    Generate a 7-day-valid share token for the printable wood report.

    Uses itsdangerous.URLSafeTimedSerializer so the token carries an embedded
    timestamp the server can verify on read. Tokens older than 7 days are
    rejected by verify_share_token().
    """
    if not secret_key:
        raise ValueError('SECRET_KEY is required for share token generation')
    from itsdangerous import URLSafeTimedSerializer
    serializer = URLSafeTimedSerializer(secret_key, salt='woodboss-share')
    return serializer.dumps({'tid': int(tournament_id)})


def verify_share_token(token, tournament_id, secret_key, max_age_seconds=7 * 24 * 60 * 60):
    """
    Verify a share token issued by generate_share_token().

    Returns True only if (1) the signature is valid, (2) the embedded
    tournament_id matches the requested one, and (3) the token is younger than
    max_age_seconds (default 7 days).
    """
    if not token or not secret_key:
        return False
    from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
    serializer = URLSafeTimedSerializer(secret_key, salt='woodboss-share')
    try:
        payload = serializer.loads(token, max_age=max_age_seconds)
    except SignatureExpired:
        return False
    except BadSignature:
        return False
    if not isinstance(payload, dict):
        return False
    return payload.get('tid') == int(tournament_id)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def calculate_blocks(tournament_id, counts=None, configs=None):
    """
    Calculate chopping block requirements.

    Returns a list of dicts, one per config_key in BLOCK_CONFIG_LABELS order:
        {
            'config_key': str,
            'label': str,
            'species': str | None,
            'size_value': float | None,
            'size_unit': str,
            'size_display': str | None,
            'competitor_count': int,   # = blocks needed (1 per competitor, or count_override for relay)
            'is_manual': bool,         # True if count came from count_override
            'count_override': int | None,
        }
    """
    if counts is None:
        counts = _count_competitors(tournament_id)
    if configs is None:
        configs = _get_configs(tournament_id)

    # Accumulate enrollment-based counts per config_key
    key_counts = defaultdict(int)
    for (event_lower, comp_type, gender), n in counts.items():
        matched_cfg_keys = set()

        # Pro exclusivity: on the pro side, the 1-Board and 3-Board Jigger
        # categories are DISTINCT from the generic 2-board Springboard
        # category. If the event name explicitly names 1-board or 3-board,
        # the generic 'springboard' fragment must NOT also match — otherwise
        # one competitor would be counted twice (one 2-board plus one 1-board
        # / 3-board block), shorting real 2-board inventory on block-turning
        # day or ghosting extra blocks for a category that isn't running.
        is_pro_one_board = comp_type == 'pro' and (
            '1-board' in event_lower or '1 board' in event_lower
            or 'one board' in event_lower or 'one-board' in event_lower
        )
        is_pro_three_board = comp_type == 'pro' and (
            '3-board' in event_lower or '3 board' in event_lower
            or 'three-board' in event_lower or 'three board' in event_lower
            or 'jigger' in event_lower
        )
        skip_pro_springboard_fallback = is_pro_one_board or is_pro_three_board

        for (fragment, grp_type, grp_gender, cfg_key, _label) in BLOCK_EVENT_GROUPS:
            if fragment not in event_lower:
                continue
            if comp_type != grp_type:
                continue
            # grp_gender=None means open (any gender matches)
            if grp_gender is not None and gender != grp_gender:
                continue
            # Exclusivity: don't fold a 1-board / 3-board event into the
            # generic 2-board Springboard bucket.
            if skip_pro_springboard_fallback and cfg_key == 'block_springboard_pro':
                continue
            matched_cfg_keys.add(cfg_key)
        for cfg_key in matched_cfg_keys:
            key_counts[cfg_key] += n

    results = []
    seen = set()
    for cfg_key, label in BLOCK_CONFIG_LABELS.items():
        if cfg_key in seen:
            continue
        seen.add(cfg_key)
        cfg = configs.get(cfg_key)
        is_relay = cfg_key in RELAY_BLOCK_KEYS

        if is_relay:
            # Relay blocks: use count_override; enrollment count is always 0
            # M4: use `is not None` so an explicit 0 override means "zero
            # relay teams" rather than "not set" (consistent with the
            # non-relay branch below).
            manual_count = cfg.count_override if cfg and cfg.count_override is not None else 0
            competitor_count = manual_count
            is_manual = True
        else:
            # Enrollment-based count; count_override overrides if set
            enrollment_count = key_counts.get(cfg_key, 0)
            manual = cfg.count_override if cfg and cfg.count_override is not None else None
            competitor_count = manual if manual is not None else enrollment_count
            is_manual = manual is not None

        results.append({
            'config_key': cfg_key,
            'label': label,
            'species': cfg.species if cfg else None,
            'size_value': cfg.size_value if cfg else None,
            'size_unit': cfg.size_unit if cfg else 'in',
            'size_display': _fmt_size(cfg),
            'competitor_count': competitor_count,
            'is_manual': is_manual,
            'count_override': cfg.count_override if cfg else None,
        })

    return results


def calculate_saw_wood(tournament_id, counts=None, configs=None):
    """
    Calculate saw log requirements.

    Returns a list of dicts (one per saw event × gender with enrolled competitors):
        {
            'event_label': str,
            'gender': 'M'|'F'|'open',
            'competitor_count': int,
            'cut_count': int,          # pairs for partnered events
            'formula_desc': str,
            'category': str,           # 'crosscut'|'stocksaw'|'hotsaw'|'op'|'cookie'
            'total_inches': float|None, # None for cookie
            'log_count': int|None,     # only for cookie
            'species': str|None,
            'size_value': float|None,
            'size_unit': str,
            'size_display': str|None,
            'config_key': str,
        }
    """
    if counts is None:
        counts = _count_competitors(tournament_id)
    if configs is None:
        configs = _get_configs(tournament_id)

    general_cfg = configs.get(LOG_GENERAL_KEY)
    stock_cfg = configs.get(LOG_STOCK_KEY) or general_cfg  # fall back to general if stock not set
    op_cfg = configs.get(LOG_OP_KEY)        # independent — no fallback to general
    cookie_cfg = configs.get(LOG_COOKIE_KEY)  # independent — no fallback to general

    results = []

    for (fragment, category, is_partnered, base_label) in SAW_EVENTS:
        # Find all (event, comp_type, gender) combos that match this saw event.
        # Track by (comp_type, gender) so college and pro appear as separate rows.
        matched = {}  # (comp_type, gender) -> total_competitor_count
        for (event_lower, comp_type, gender), n in counts.items():
            if fragment not in event_lower:
                continue
            matched[(comp_type, gender)] = matched.get((comp_type, gender), 0) + n

        if category == 'stocksaw':
            cfg = stock_cfg
            cfg_key = LOG_STOCK_KEY
        elif category == 'op':
            cfg = op_cfg
            cfg_key = LOG_OP_KEY
        elif category == 'cookie':
            cfg = cookie_cfg
            cfg_key = LOG_COOKIE_KEY
        else:
            cfg = general_cfg
            cfg_key = LOG_GENERAL_KEY

        # Gendered events emit separate M/F rows; open events collapse all genders.
        # Emit rows for both college and pro divisions separately.
        is_gendered = fragment in ('single buck', 'double buck', 'stock saw', 'obstacle pole')

        # Determine which (comp_type, gender) combos to emit.
        # Always include college and pro rows (even if zero) for visibility.
        rows_to_emit = []
        for comp_type in ('college', 'pro'):
            if is_gendered:
                for g in ('M', 'F'):
                    rows_to_emit.append((comp_type, g))
            else:
                rows_to_emit.append((comp_type, 'open'))

        for (comp_type, gender) in rows_to_emit:
            # For open (non-gendered) events, sum all genders for this comp_type.
            if gender == 'open':
                competitor_count = sum(
                    n for (ct, _g), n in matched.items() if ct == comp_type
                )
            else:
                competitor_count = matched.get((comp_type, gender), 0)

            division_label = 'College' if comp_type == 'college' else 'Pro'
            gender_label = '' if gender == 'open' else (' Men' if gender == 'M' else ' Women')
            event_label = f'{base_label}{gender_label} — {division_label}'

            cut_count = competitor_count // 2 if is_partnered else competitor_count

            if category == 'crosscut':
                total_inches = cut_count * 2.0
                formula_desc = f'{cut_count} cut{"s" if cut_count != 1 else ""} × 2" = {total_inches:.0f}"'
                log_count = None
            elif category == 'stocksaw':
                total_inches = cut_count * 5.0
                formula_desc = f'{cut_count} cut{"s" if cut_count != 1 else ""} × 5" = {total_inches:.0f}"'
                log_count = None
            elif category == 'hotsaw':
                total_inches = cut_count * 6.5
                formula_desc = f'{cut_count} cut{"s" if cut_count != 1 else ""} × 6.5" = {total_inches:.1f}"'
                log_count = None
            elif category == 'op':
                lag_blocks = math.ceil(cut_count / 7) * 5 if cut_count > 0 else 0
                total_inches = cut_count * 2.0 + lag_blocks
                formula_desc = f'{cut_count} × 2" + ⌈{cut_count}/7⌉×5" lag = {total_inches:.0f}"'
                log_count = None
            elif category == 'cookie':
                log_count = math.ceil(cut_count / 3) if cut_count > 0 else 0
                total_inches = None
                formula_desc = f'⌈{cut_count}/3⌉ = {log_count} log{"s" if log_count != 1 else ""}'
            else:
                total_inches = None
                log_count = None
                formula_desc = ''

            results.append({
                'event_label': event_label,
                'comp_type': comp_type,
                'gender': gender,
                'competitor_count': competitor_count,
                'cut_count': cut_count,
                'formula_desc': formula_desc,
                'category': category,
                'total_inches': total_inches,
                'log_count': log_count,
                'species': cfg.species if cfg else None,
                'size_value': cfg.size_value if cfg else None,
                'size_unit': cfg.size_unit if cfg else 'in',
                'size_display': _fmt_size(cfg),
                'config_key': cfg_key,
            })

    # ── Pro-Am Relay Double Buck ────────────────────────────────────────────
    # Relay participants are NOT enrolled in a standard Double Buck event —
    # they're in the ProAmRelay system.  The judge enters team count manually
    # (count_override on log_relay_doublebuck config key).
    # Formula: each relay team does 1 double buck cut = 2" per team.
    relay_db_cfg = configs.get(LOG_RELAY_DOUBLEBUCK_KEY)
    relay_team_count = (
        relay_db_cfg.count_override
        if relay_db_cfg and relay_db_cfg.count_override is not None and relay_db_cfg.count_override > 0
        else 0
    )
    if relay_team_count:
        relay_inches = relay_team_count * 2.0
        # Species/size: use relay-specific config if set, otherwise fall back to general log
        rel_species = (relay_db_cfg.species if relay_db_cfg and relay_db_cfg.species else
                       (general_cfg.species if general_cfg else None))
        rel_size_value = (relay_db_cfg.size_value if relay_db_cfg and relay_db_cfg.size_value is not None else
                          (general_cfg.size_value if general_cfg else None))
        # M3: gate size_unit on the relay row having a real unit, not on
        # size_value. Otherwise a relay row with a selected unit but blank
        # diameter silently falls back to general.
        rel_size_unit = (relay_db_cfg.size_unit if relay_db_cfg and relay_db_cfg.size_unit in ('in', 'mm') else
                         (general_cfg.size_unit if general_cfg else 'in'))

        # Build a temporary config-like object for _fmt_size
        class _FakeCfg:
            pass
        _fake = _FakeCfg()
        _fake.size_value = rel_size_value
        _fake.size_unit = rel_size_unit

        results.append({
            'event_label': 'Pro-Am Relay — Double Buck',
            'comp_type': 'relay',
            'gender': 'open',
            'competitor_count': relay_team_count,
            'cut_count': relay_team_count,
            'formula_desc': (
                f'{relay_team_count} team{"s" if relay_team_count != 1 else ""} '
                f'× 2" = {relay_inches:.0f}"'
            ),
            'category': 'crosscut',
            'total_inches': relay_inches,
            'log_count': None,
            'species': rel_species,
            'size_value': rel_size_value,
            'size_unit': rel_size_unit,
            'size_display': _fmt_size(_fake),
            'config_key': LOG_RELAY_DOUBLEBUCK_KEY,
        })

    # Sort: college first, then pro, then relay — within each, preserve SAW_EVENTS order.
    type_order = {'college': 0, 'pro': 1, 'relay': 2}
    results.sort(key=lambda r: type_order.get(r.get('comp_type', 'pro'), 1))

    return results


def _group_by_species(blocks, saw_wood):
    """
    Aggregate blocks and saw wood by (species, size_value, size_unit).

    Returns:
        {
          'blocks': list of {species, size_display, events, total_count},
          'logs': list of {species, size_display, events, total_inches, total_log_count},
        }
    Each list is sorted by total (descending).
    """
    block_groups = defaultdict(lambda: {'events': [], 'total_count': 0})
    for b in blocks:
        if b['competitor_count'] == 0:
            continue
        key = (
            (b['species'] or '').lower().strip(),
            b['size_value'],
            b['size_unit'],
        )
        block_groups[key]['events'].append(b['label'])
        block_groups[key]['total_count'] += b['competitor_count']
        block_groups[key]['species'] = b['species'] or '(not set)'
        block_groups[key]['size_display'] = b['size_display'] or '—'

    # OP and Cookie Stack use independent log specs — never merge them with general saw logs
    # even if species/size happens to match. Include category_bucket in the grouping key.
    log_groups = defaultdict(lambda: {'events': [], 'total_inches': 0.0, 'total_log_count': 0,
                                      'category_bucket': 'general'})
    for s in saw_wood:
        cat = s['category']
        bucket = 'op' if cat == 'op' else ('cookie' if cat == 'cookie' else 'general')
        key = (
            bucket,
            (s['species'] or '').lower().strip(),
            s['size_value'],
            s['size_unit'],
        )
        log_groups[key]['events'].append(s['event_label'])
        if s['total_inches'] is not None:
            log_groups[key]['total_inches'] += s['total_inches']
        if s['log_count'] is not None:
            log_groups[key]['total_log_count'] += s['log_count']
        log_groups[key]['species'] = s['species'] or '(not set)'
        log_groups[key]['size_display'] = s['size_display'] or '—'
        log_groups[key]['category_bucket'] = bucket

    block_list = sorted(block_groups.values(), key=lambda x: x['total_count'], reverse=True)
    # Sort: general first, then OP, then cookie; within each bucket descending by linear footage
    bucket_order = {'general': 0, 'op': 1, 'cookie': 2}
    log_list = sorted(log_groups.values(),
                      key=lambda x: (bucket_order.get(x['category_bucket'], 9), -x['total_inches']))
    return {'blocks': block_list, 'logs': log_list}


def get_ordering_summary(blocks, saw_wood):
    """
    Flatten blocks and saw wood into a single purchase/prep order list.

    Returns a list of line-item dicts:
        {'category': 'block'|'log', 'species', 'size_display', 'quantity', 'unit',
         'total_inches', 'total_log_count', 'events': [str]}
    """
    items = []

    # --- Blocks ---
    block_grps = defaultdict(lambda: {'events': [], 'total': 0, 'species': '', 'size_display': ''})
    for b in blocks:
        if b['competitor_count'] == 0:
            continue
        k = ((b['species'] or '').lower().strip(), b['size_value'], b['size_unit'])
        block_grps[k]['events'].append(b['label'])
        block_grps[k]['total'] += b['competitor_count']
        block_grps[k]['species'] = b['species'] or '(species not set)'
        block_grps[k]['size_display'] = b['size_display'] or '?'

    for _k, g in sorted(block_grps.items(), key=lambda x: -x[1]['total']):
        items.append({
            'category': 'block',
            'species': g['species'],
            'size_display': g['size_display'],
            'quantity': g['total'],
            'unit': 'blocks',
            'total_inches': None,
            'total_log_count': None,
            'events': g['events'],
        })

    # --- Saw logs: OP and Cookie Stack are independent categories, never merged with general ---
    log_grps = defaultdict(lambda: {'events': [], 'total_inches': 0.0, 'total_logs': 0,
                                    'species': '', 'size_display': '', 'log_category': 'general'})
    for s in saw_wood:
        if s['competitor_count'] == 0:
            continue
        cat = s['category']
        bucket = 'op' if cat == 'op' else ('cookie' if cat == 'cookie' else 'general')
        k = (bucket, (s['species'] or '').lower().strip(), s['size_value'], s['size_unit'])
        log_grps[k]['events'].append(s['event_label'])
        if s['total_inches'] is not None:
            log_grps[k]['total_inches'] += s['total_inches']
        if s['log_count'] is not None:
            log_grps[k]['total_logs'] += s['log_count']
        log_grps[k]['species'] = s['species'] or '(species not set)'
        log_grps[k]['size_display'] = s['size_display'] or '?'
        log_grps[k]['log_category'] = bucket

    bucket_order = {'general': 0, 'op': 1, 'cookie': 2}
    for _k, g in sorted(log_grps.items(),
                         key=lambda x: (bucket_order.get(x[1]['log_category'], 9), -x[1]['total_inches'])):
        items.append({
            'category': 'log',
            'log_category': g['log_category'],
            'species': g['species'],
            'size_display': g['size_display'],
            'quantity': None,
            'unit': 'linear"',
            'total_inches': g['total_inches'],
            'total_log_count': g['total_logs'],
            'events': g['events'],
        })

    return items


def get_lottery_view(tournament_id):
    """
    Build the block lottery assignment data.

    Groups competitor names by (species, size) → event → competitors, so the
    show crew can prepare note cards for each block.

    Returns a list of column dicts, one per (species, size) combination:
        {
          'species': str,
          'size_display': str,
          'total_blocks': int,
          'sections': [
              {
                'config_label': str,       # e.g. "Underhand — College Men"
                'event_name': str,         # e.g. "Underhand Hard Hit"
                'competitors': [
                    {'name': str, 'affiliation': str}
                ],
              },
              ...
          ]
        }
    """
    configs = _get_configs(tournament_id)
    competitors = _list_competitors(tournament_id)

    # For each block config_key, build event_name → [competitor] mapping
    # key_event_comps: cfg_key → {event_name: [{'name', 'affiliation'}]}
    key_event_comps = defaultdict(lambda: defaultdict(list))

    for comp in competitors:
        gender = comp['gender']
        comp_type = comp['comp_type']
        for event_name in comp['events']:
            event_lower = event_name.lower().strip()
            matched_cfg_keys = set()
            for (fragment, grp_type, grp_gender, cfg_key, _label) in BLOCK_EVENT_GROUPS:
                if fragment not in event_lower:
                    continue
                if comp_type != grp_type:
                    continue
                if grp_gender is not None and gender != grp_gender:
                    continue
                matched_cfg_keys.add(cfg_key)
            for cfg_key in matched_cfg_keys:
                key_event_comps[cfg_key][event_name].append({
                    'name': comp['name'],
                    'affiliation': comp['affiliation'],
                })

    # For relay blocks: generate placeholder entries based on count_override
    for cfg_key in RELAY_BLOCK_KEYS:
        cfg = configs.get(cfg_key)
        if cfg and cfg.count_override and cfg.count_override > 0:
            label = BLOCK_CONFIG_LABELS[cfg_key]
            key_event_comps[cfg_key]['Pro-Am Relay (Lottery)'] = [
                {'name': f'Relay Team {i + 1}', 'affiliation': ''}
                for i in range(cfg.count_override)
            ]

    # Group config_keys by (species, size_value, size_unit) to form display columns
    spec_groups = {}  # spec_key → {'species', 'size_display', 'sections': []}

    for cfg_key, label in BLOCK_CONFIG_LABELS.items():
        cfg = configs.get(cfg_key)
        event_map = key_event_comps.get(cfg_key, {})
        if not event_map:
            continue

        spec_key = (
            (cfg.species or '').lower().strip() if cfg else '',
            cfg.size_value if cfg else None,
            cfg.size_unit if cfg else 'in',
        )

        if spec_key not in spec_groups:
            spec_groups[spec_key] = {
                'species': (cfg.species or '(not set)') if cfg else '(not set)',
                'size_display': _fmt_size(cfg) or '—',
                'sections': [],
            }

        for event_name, comp_list in sorted(event_map.items()):
            spec_groups[spec_key]['sections'].append({
                'config_label': label,
                'event_name': event_name,
                'competitors': sorted(comp_list, key=lambda c: c['name']),
            })

    # Build result list sorted by species name then size
    result = []
    for spec_key, grp in sorted(spec_groups.items(), key=lambda x: (x[1]['species'], x[1]['size_display'])):
        total = sum(len(s['competitors']) for s in grp['sections'])
        result.append({
            'species': grp['species'],
            'size_display': grp['size_display'],
            'total_blocks': total,
            'sections': grp['sections'],
        })

    return result


def _detect_friday_feature_springboard(tournament_id):
    """Return (pro_one_board_is_friday, three_board_is_friday) by reading
    the Friday Feature config file for this tournament. Isolates the file
    IO + DB lookup that used to live inside `calculate_springboard_dummies`
    so the math function is pure(r) and unit-testable without disk state.
    """
    if tournament_id is None:
        return (False, False)
    import os
    import json
    pro_one_board_is_friday = False
    three_board_is_friday = False
    try:
        instance_dir = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'instance'
        )
        fnf_path = os.path.join(instance_dir, f'friday_feature_{tournament_id}.json')
        if not os.path.exists(fnf_path):
            return (False, False)
        with open(fnf_path, 'r') as f:
            fnf_data = json.load(f)
        fnf_event_ids = set(fnf_data.get('event_ids', []))
    except Exception:
        return (False, False)

    if not fnf_event_ids:
        return (False, False)

    from models.event import Event
    fnf_events = Event.query.filter(Event.id.in_(fnf_event_ids)).all()
    for ev in fnf_events:
        name_lower = ev.name.lower()
        if '1-board' in name_lower or '1 board' in name_lower:
            pro_one_board_is_friday = True
        elif '3-board' in name_lower or '3 board' in name_lower or 'jigger' in name_lower:
            three_board_is_friday = True
    return (pro_one_board_is_friday, three_board_is_friday)


def calculate_springboard_dummies(blocks, tournament_id=None):
    """
    Calculate springboard dummy/tree requirements from block counts.

    Separates 1-board, 2-board, and 3-board into distinct groups because
    each requires a different height dummy tree.

    Rules:
      - 1-board / 2-board runs: 3 runs per dummy
      - 3-board jigger runs: 2 runs per dummy

    Friday Feature logic:
      - College 1-Board always runs Friday → needs its own Friday dummies.
      - Pro 1-Board or 3-Board Jigger in the Friday Feature → separate
        Friday dummies (cannot reuse Saturday trees).
      - If 1-board runs AFTER 2-board on Saturday, 2-board dummies can be
        cut down to 6 feet and reused for 1-board → only need max, not sum.
    """
    # --- Separate competitor counts by board height ---
    # College 1-Board is always "1-board" height.
    # Pro "Springboard" is 2-board. Pro "Pro 1-Board" is 1-board.
    # The existing config keys lump them — we need to count via event data.
    college_keys = {'block_springboard_college_M', 'block_springboard_college_F'}
    pro_sb_key = 'block_springboard_pro'
    three_board_keys = {'block_3board_pro'}

    # College 1-Board runs (always Friday)
    college_one_board_runs = sum(
        b['competitor_count'] for b in blocks if b['config_key'] in college_keys
    )

    # Pro springboard (2-board) runs
    pro_two_board_runs = 0
    # Pro 1-Board runs
    pro_one_board_runs = 0

    # Split pro 1-board vs 2-board runs via tournament event data.
    # (Block config keys are now already split into block_springboard_pro and
    # block_1board_pro, but this direct event-name walk is the authoritative
    # source for dummy math and avoids any double-counting.)
    if tournament_id is not None:
        counts = _count_competitors(tournament_id)
        for (event_lower, comp_type, _gender), n in counts.items():
            if comp_type != 'pro':
                continue
            if '1-board' in event_lower or '1 board' in event_lower or 'one board' in event_lower:
                pro_one_board_runs += n
            elif 'springboard' in event_lower and '3-board' not in event_lower and '3 board' not in event_lower:
                pro_two_board_runs += n
    else:
        # Fallback: lump all pro springboard as 2-board
        pro_two_board_runs = sum(
            b['competitor_count'] for b in blocks if b['config_key'] == pro_sb_key
        )

    three_board_runs = sum(
        b['competitor_count'] for b in blocks if b['config_key'] in three_board_keys
    )

    one_board_per_dummy = 3
    two_board_per_dummy = 3
    three_board_per_dummy = 2

    # --- Friday Feature detection (IO isolated in helper) ---
    pro_one_board_is_friday, three_board_is_friday = _detect_friday_feature_springboard(tournament_id)

    # --- Calculate dummies by day ---
    # Friday: college 1-board + any pro FNF springboard events
    friday_one_board_runs = college_one_board_runs
    if pro_one_board_is_friday:
        friday_one_board_runs += pro_one_board_runs
    friday_three_board_runs = three_board_runs if three_board_is_friday else 0

    friday_one_board_dummies = math.ceil(friday_one_board_runs / one_board_per_dummy) if friday_one_board_runs > 0 else 0
    friday_three_board_dummies = math.ceil(friday_three_board_runs / three_board_per_dummy) if friday_three_board_runs > 0 else 0

    # Saturday: 2-board always, pro 1-board if NOT FNF, 3-board if NOT FNF
    sat_two_board_runs = pro_two_board_runs
    sat_one_board_runs = 0 if pro_one_board_is_friday else pro_one_board_runs
    sat_three_board_runs = 0 if three_board_is_friday else three_board_runs

    sat_two_board_dummies = math.ceil(sat_two_board_runs / two_board_per_dummy) if sat_two_board_runs > 0 else 0
    sat_one_board_dummies = math.ceil(sat_one_board_runs / one_board_per_dummy) if sat_one_board_runs > 0 else 0
    sat_three_board_dummies = math.ceil(sat_three_board_runs / three_board_per_dummy) if sat_three_board_runs > 0 else 0

    # Reuse logic: if 1-board follows 2-board on Saturday, 2-board trees
    # can be cut down to 6ft → need max(2-board, 1-board) instead of sum.
    # We assume 1-board always follows 2-board on Saturday (standard order).
    sat_one_two_reusable = sat_one_board_runs > 0 and sat_two_board_runs > 0
    if sat_one_two_reusable:
        sat_combined_dummies = max(sat_two_board_dummies, sat_one_board_dummies)
    else:
        sat_combined_dummies = sat_two_board_dummies + sat_one_board_dummies

    total_dummies = (
        friday_one_board_dummies + friday_three_board_dummies
        + sat_combined_dummies + sat_three_board_dummies
    )

    return {
        # Per-height breakdown
        'college_one_board_runs': college_one_board_runs,
        'pro_one_board_runs': pro_one_board_runs,
        'pro_two_board_runs': pro_two_board_runs,
        'three_board_runs': three_board_runs,

        'one_board_per_dummy': one_board_per_dummy,
        'two_board_per_dummy': two_board_per_dummy,
        'three_board_per_dummy': three_board_per_dummy,

        # Friday Feature status
        'pro_one_board_is_friday': pro_one_board_is_friday,
        'three_board_is_friday': three_board_is_friday,

        # Friday counts
        'friday_one_board_runs': friday_one_board_runs,
        'friday_one_board_dummies': friday_one_board_dummies,
        'friday_three_board_runs': friday_three_board_runs,
        'friday_three_board_dummies': friday_three_board_dummies,

        # Saturday counts
        'sat_two_board_runs': sat_two_board_runs,
        'sat_two_board_dummies': sat_two_board_dummies,
        'sat_one_board_runs': sat_one_board_runs,
        'sat_one_board_dummies': sat_one_board_dummies,
        'sat_three_board_runs': sat_three_board_runs,
        'sat_three_board_dummies': sat_three_board_dummies,
        'sat_one_two_reusable': sat_one_two_reusable,
        'sat_combined_dummies': sat_combined_dummies,

        # Totals
        'total_dummies': total_dummies,
    }


def get_wood_report(tournament_id):
    """
    Full wood material report for a tournament.

    Returns:
        {
          'blocks': [...],
          'saw_wood': [...],
          'by_species': {...},
          'ordering': [...],
          'configs': {...},
          'is_configured': bool,
          'total_blocks': int,
          'total_saw_inches': float,
          'total_cookie_logs': int,
          'springboard': dict,
        }
    """
    configs = _get_configs(tournament_id)
    counts = _count_competitors(tournament_id)
    blocks = calculate_blocks(tournament_id, counts=counts, configs=configs)
    saw_wood = calculate_saw_wood(tournament_id, counts=counts, configs=configs)
    by_species = _group_by_species(blocks, saw_wood)
    ordering = get_ordering_summary(blocks, saw_wood)
    springboard = calculate_springboard_dummies(blocks, tournament_id=tournament_id)

    total_blocks = sum(b['competitor_count'] for b in blocks)
    # OP and Cookie Stack are independent categories — never fold into general saw total
    total_saw_inches = sum(
        s['total_inches'] for s in saw_wood
        if s['total_inches'] is not None and s['category'] not in ('op', 'cookie')
    )
    total_op_inches = sum(
        s['total_inches'] for s in saw_wood
        if s['total_inches'] is not None and s['category'] == 'op'
    )
    total_cookie_logs = sum(s['log_count'] for s in saw_wood if s['log_count'] is not None)

    return {
        'blocks': blocks,
        'saw_wood': saw_wood,
        'by_species': by_species,
        'ordering': ordering,
        'configs': configs,
        'is_configured': bool(configs),
        'total_blocks': total_blocks,
        'total_saw_inches': total_saw_inches,
        'total_op_inches': total_op_inches,
        'total_cookie_logs': total_cookie_logs,
        'springboard': springboard,
    }


# ---------------------------------------------------------------------------
# Wood preset helpers
#
# Preset file format (instance/wood_presets.json):
#
#   {
#     "preset_name": {
#       "blocks": {"species": "...", "size_value": X, "size_unit": "in"},   # legacy fallback (V1)
#       "blocks_by_key": {                                                    # V2 — per-category
#         "block_underhand_college_M": {"species": "...", "size_value": X, "size_unit": "in"},
#         ...
#       },
#       "log_general": {...},
#       "log_stock": {...},
#       "log_op": {...},
#       "log_cookie": {...},
#       "log_relay_doublebuck": {...}
#     }
#   }
#
# Readers prefer "blocks_by_key" when present; fall back to broadcasting
# "blocks" across every active block key for older saved files.
# ---------------------------------------------------------------------------

_LOG_PRESET_KEYS = (
    LOG_GENERAL_KEY,
    LOG_STOCK_KEY,
    LOG_OP_KEY,
    LOG_COOKIE_KEY,
    LOG_RELAY_DOUBLEBUCK_KEY,
)


def _preset_path():
    """Return the path to instance/wood_presets.json, creating instance/ if needed.

    No module-level cache — pytest workers would otherwise share the path
    across tests and leak state.
    """
    import os
    base = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'instance'
    )
    os.makedirs(base, exist_ok=True)
    return os.path.join(base, 'wood_presets.json')


def _load_preset_file():
    """Load the custom preset file, returning {} on missing/corrupt.

    A corrupt file is logged as a warning (not silently swallowed) so
    operators notice when custom presets are lost.
    """
    import json
    import logging
    try:
        with open(_preset_path(), 'r') as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError as e:
        logging.getLogger(__name__).warning(
            'wood_presets.json is corrupt (%s); custom presets ignored', e
        )
        return {}


def _write_preset_file(presets):
    """Write custom presets atomically (tmpfile + os.replace).

    Prevents half-written files from trashing all custom presets if the
    process dies mid-write.
    """
    import json
    import os
    path = _preset_path()
    tmp = path + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(presets, f, indent=2)
    os.replace(tmp, path)


def get_all_presets():
    """Return merged dict of built-in + custom presets (custom overrides built-in)."""
    import config as cfg
    presets = dict(cfg.WOOD_PRESETS)
    presets.update(_load_preset_file())
    return presets


def is_builtin_preset(name):
    """Return True if `name` is a built-in preset defined in config.WOOD_PRESETS."""
    import config as cfg
    return name in cfg.WOOD_PRESETS


def save_custom_preset(name, preset_data):
    """Save a named preset to instance/wood_presets.json.

    Raises ValueError if `name` collides with a built-in preset name —
    silently shadowing a built-in preset used to be possible and made
    dashboard dropdowns confusing. Callers should catch and flash.
    """
    if is_builtin_preset(name):
        raise ValueError(
            f'"{name}" is a built-in preset name — pick a different name'
        )
    presets = _load_preset_file()
    presets[name] = preset_data
    _write_preset_file(presets)


def delete_custom_preset(name):
    """Delete a named preset from instance/wood_presets.json."""
    presets = _load_preset_file()
    presets.pop(name, None)
    _write_preset_file(presets)


def _resolve_block_spec_for_key(preset, cfg_key):
    """Return the {species, size_value, size_unit} spec to apply to a given
    block cfg_key, or None if the preset has nothing for that key.

    Prefers `blocks_by_key[cfg_key]` (V2 per-category form); falls back to
    the V1 broadcast `blocks` spec. Returns None if neither is set.
    """
    by_key = preset.get('blocks_by_key') or {}
    if cfg_key in by_key:
        return by_key[cfg_key]
    legacy = preset.get('blocks') or {}
    if legacy:
        return legacy
    return None


def _apply_spec_to_row(row, spec):
    """Overwrite species/size_value/size_unit on a WoodConfig row from a spec.

    Skips any field whose spec value is None — that's how we preserve
    existing diameters when a preset was saved with species-only (no
    diameter). Previous behaviour wrote None through and wiped the row.
    """
    species = spec.get('species')
    if species is not None:
        row.species = species
    size_value = spec.get('size_value')
    if size_value is not None:
        row.size_value = size_value
    size_unit = spec.get('size_unit')
    if size_unit is not None:
        row.size_unit = size_unit


def apply_preset(tournament_id, preset_name):
    """Apply a named preset to a tournament's wood config.

    Only overwrites species/size_value/size_unit on existing rows; never
    writes None through (so a species-only preset does not wipe existing
    diameters). Leaves count_override and notes untouched. For block keys,
    only touches keys the tournament's events actually use (no ghost rows).

    Returns the number of config keys updated.
    """
    from models.wood_config import WoodConfig
    from database import db

    presets = get_all_presets()
    preset = presets.get(preset_name)
    if not preset:
        return 0

    updated = 0
    active_keys = _active_block_keys(tournament_id)

    # ── Block keys ────────────────────────────────────────────────────────
    # Try per-cfg_key spec first, fall back to broadcast `blocks` spec.
    for cfg_key in BLOCK_CONFIG_LABELS:
        if cfg_key in RELAY_BLOCK_KEYS:
            continue
        if cfg_key not in active_keys:
            continue
        spec = _resolve_block_spec_for_key(preset, cfg_key)
        if not spec:
            continue
        if spec.get('species') is None:
            # No species to apply for this key — skip (avoids inserting
            # a half-empty row when the preset only covers other keys).
            continue
        existing = WoodConfig.query.filter_by(
            tournament_id=tournament_id, config_key=cfg_key
        ).first()
        if existing:
            _apply_spec_to_row(existing, spec)
        else:
            db.session.add(WoodConfig(
                tournament_id=tournament_id,
                config_key=cfg_key,
                species=spec.get('species'),
                size_value=spec.get('size_value'),
                size_unit=spec.get('size_unit') or 'in',
            ))
        updated += 1

    # ── Log keys ──────────────────────────────────────────────────────────
    for log_key in _LOG_PRESET_KEYS:
        spec = preset.get(log_key)
        if not spec:
            continue
        if spec.get('species') is None:
            continue
        existing = WoodConfig.query.filter_by(
            tournament_id=tournament_id, config_key=log_key
        ).first()
        if existing:
            _apply_spec_to_row(existing, spec)
        else:
            db.session.add(WoodConfig(
                tournament_id=tournament_id,
                config_key=log_key,
                species=spec.get('species'),
                size_value=spec.get('size_value'),
                size_unit=spec.get('size_unit') or 'in',
            ))
        updated += 1

    db.session.commit()
    return updated


def _parse_size(raw):
    """Coerce a form field to float or None."""
    raw = (raw or '').strip()
    if not raw:
        return None
    try:
        return float(raw)
    except (ValueError, TypeError):
        return None


def _parse_unit(raw):
    """Coerce a form unit field to 'in' or 'mm'."""
    u = (raw or 'in').strip()
    return u if u in ('in', 'mm') else 'in'


def build_preset_from_form(form_data):
    """Build a preset dict from a posted wood config form (same field names
    as `save_config`). Captures per-category block specs so different
    species can live under different block keys.

    Emits BOTH `blocks_by_key` (V2) and `blocks` (V1 broadcast) — `blocks`
    holds the first non-empty spec so older readers still get something
    useful.
    """
    blocks_by_key = {}
    broadcast = {}
    for cfg_key in BLOCK_CONFIG_LABELS:
        if cfg_key in RELAY_BLOCK_KEYS:
            continue
        species = (form_data.get(f'species_{cfg_key}', '') or '').strip()
        if not species:
            continue
        spec = {
            'species': species,
            'size_value': _parse_size(form_data.get(f'size_value_{cfg_key}')),
            'size_unit': _parse_unit(form_data.get(f'size_unit_{cfg_key}')),
        }
        blocks_by_key[cfg_key] = spec
        if not broadcast:
            broadcast = dict(spec)

    preset = {'blocks': broadcast, 'blocks_by_key': blocks_by_key}
    for log_key in _LOG_PRESET_KEYS:
        species = (form_data.get(f'species_{log_key}', '') or '').strip()
        if not species:
            continue
        preset[log_key] = {
            'species': species,
            'size_value': _parse_size(form_data.get(f'size_value_{log_key}')),
            'size_unit': _parse_unit(form_data.get(f'size_unit_{log_key}')),
        }
    return preset


def build_preset_from_config(tournament_id):
    """Build a preset dict from the current tournament's wood config.

    Captures every non-relay block row that has a species set into
    `blocks_by_key`, plus the first as a legacy `blocks` broadcast.
    """
    configs = _get_configs(tournament_id)
    blocks_by_key = {}
    broadcast = {}
    for cfg_key in BLOCK_CONFIG_LABELS:
        if cfg_key in RELAY_BLOCK_KEYS:
            continue
        cfg = configs.get(cfg_key)
        if not cfg or not cfg.species:
            continue
        spec = {
            'species': cfg.species,
            'size_value': cfg.size_value,
            'size_unit': cfg.size_unit or 'in',
        }
        blocks_by_key[cfg_key] = spec
        if not broadcast:
            broadcast = dict(spec)

    preset = {'blocks': broadcast, 'blocks_by_key': blocks_by_key}
    for log_key in _LOG_PRESET_KEYS:
        cfg = configs.get(log_key)
        if cfg and cfg.species:
            preset[log_key] = {
                'species': cfg.species,
                'size_value': cfg.size_value,
                'size_unit': cfg.size_unit or 'in',
            }
    return preset


def get_history_report():
    """
    Cross-tournament summary for forecasting.

    Returns a list of dicts, one per tournament (newest first):
        {
          'tournament': Tournament,
          'total_blocks': int,
          'total_saw_inches': float,
          'total_cookie_logs': int,
          'is_configured': bool,
        }
    """
    from models.tournament import Tournament

    tournaments = Tournament.query.order_by(Tournament.year.desc(), Tournament.name).all()
    results = []
    for t in tournaments:
        configs = _get_configs(t.id)
        if not configs:
            results.append({
                'tournament': t,
                'total_blocks': 0,
                'total_saw_inches': 0.0,
                'total_cookie_logs': 0,
                'is_configured': False,
            })
            continue
        counts = _count_competitors(t.id)
        blocks = calculate_blocks(t.id, counts=counts, configs=configs)
        saw_wood = calculate_saw_wood(t.id, counts=counts, configs=configs)
        results.append({
            'tournament': t,
            'total_blocks': sum(b['competitor_count'] for b in blocks),
            'total_saw_inches': sum(
                s['total_inches'] for s in saw_wood
                if s['total_inches'] is not None and s['category'] not in ('op', 'cookie')
            ),
            'total_op_inches': sum(
                s['total_inches'] for s in saw_wood
                if s['total_inches'] is not None and s['category'] == 'op'
            ),
            'total_cookie_logs': sum(
                s['log_count'] for s in saw_wood if s['log_count'] is not None
            ),
            'is_configured': True,
        })
    return results
