"""
Shared gear-sharing parsing and matching helpers.

This module centralizes:
- partner-name normalization/matching
- event-key normalization/matching
- free-text parsing of gear-sharing details
- event-level conflict checks used by heat generation and validation
"""
from __future__ import annotations

import difflib
import json
import re
from typing import Iterable


_CATEGORY_KEYS = {'category:crosscut', 'category:chainsaw', 'category:springboard'}


def normalize_person_name(value: str) -> str:
    """Normalize a person name for tolerant matching."""
    return re.sub(r'[^a-z0-9]+', '', str(value or '').strip().lower())


def normalize_event_text(value: str) -> str:
    """Normalize event text for tolerant matching."""
    return re.sub(r'[^a-z0-9]+', '', str(value or '').strip().lower())


def build_name_index(names: Iterable[str]) -> dict[str, str]:
    """Build normalized-name -> canonical-name index."""
    index: dict[str, str] = {}
    for raw in names:
        name = str(raw or '').strip()
        if not name:
            continue
        key = normalize_person_name(name)
        if key and key not in index:
            index[key] = name
    return index


def resolve_partner_name(raw_name: str, name_index: dict[str, str], cutoff: float = 0.86) -> str:
    """Resolve raw partner text to the closest known competitor name when possible.

    Matching order:
    1. Exact normalized match
    2. difflib fuzzy match at `cutoff`
    3. Last-name-only match (e.g. "Smith") — only when unambiguous
    4. First-initial + last-name match (e.g. "J. Smith" or "J Smith") — only when unambiguous
    """
    candidate = str(raw_name or '').strip()
    if not candidate:
        return ''

    direct = name_index.get(normalize_person_name(candidate))
    if direct:
        return direct

    keys = list(name_index.keys())
    if not keys:
        return candidate

    wanted = normalize_person_name(candidate)
    if not wanted:
        return candidate

    close = difflib.get_close_matches(wanted, keys, n=1, cutoff=cutoff)
    if close:
        return name_index[close[0]]

    # Last-name-only fallback: "Smith" → matches "John Smith" when unambiguous.
    candidate_tokens = candidate.strip().split()
    if candidate_tokens:
        last_norm = normalize_person_name(candidate_tokens[-1])
        if len(last_norm) >= 3:
            last_matches = [k for k in keys if k.endswith(last_norm)]
            if len(last_matches) == 1:
                return name_index[last_matches[0]]

    # Initials fallback: "J. Smith" or "J Smith" → matches "John Smith" when unambiguous.
    if len(candidate_tokens) == 2:
        first_initial = normalize_person_name(candidate_tokens[0])[:1]
        last_norm = normalize_person_name(candidate_tokens[1])
        if first_initial and len(last_norm) >= 3:
            initial_matches = [
                k for k in keys
                if k.startswith(first_initial) and k.endswith(last_norm)
            ]
            if len(initial_matches) == 1:
                return name_index[initial_matches[0]]

    return candidate


def infer_equipment_categories(text: str) -> set[str]:
    """Infer broad equipment categories from free-text detail strings."""
    normalized = str(text or '').strip().lower()
    categories = set()
    if any(token in normalized for token in ['single buck', 'double buck', 'crosscut', 'jack & jill', 'jack and jill', 'handsaw', 'hand saw']):
        categories.add('crosscut')
    if any(token in normalized for token in ['hot saw', 'stock saw', 'chainsaw', 'power saw', 'powersaw']):
        categories.add('chainsaw')
    if any(token in normalized for token in ['springboard', 'board']):
        categories.add('springboard')
    return categories


def _event_name_aliases(event) -> set[str]:
    """Return normalized aliases for an event (including legacy import labels)."""
    aliases = {
        normalize_event_text(getattr(event, 'name', '')),
        normalize_event_text(getattr(event, 'display_name', '')),
    }
    event_name = normalize_event_text(getattr(event, 'name', ''))
    stand_type = str(getattr(event, 'stand_type', '') or '').strip().lower()

    if event_name == 'springboard':
        aliases.update({'springboardl', 'springboardr'})
    elif event_name in {'pro1board', '1boardspringboard'}:
        aliases.update({'intermediate1boardspringboard', 'pro1board', '1boardspringboard'})
    elif event_name == 'jackjillsawing':
        aliases.update({'jackjill', 'jackandjill'})
    elif event_name in {'poleclimb', 'speedclimb'}:
        aliases.update({'poleclimb', 'speedclimb'})
    elif event_name == 'partneredaxethrow':
        aliases.update({'partneredaxethrow', 'axethrow'})

    if stand_type == 'saw_hand':
        aliases.update({'singlebuck', 'doublebuck', 'jackjill', 'jackandjill', 'crosscut'})
    elif stand_type in {'hot_saw', 'stock_saw'}:
        aliases.update({'hotsaw', 'stocksaw', 'chainsaw', 'powersaw'})
    elif stand_type == 'springboard':
        aliases.update({'springboard', '1boardspringboard', 'pro1board'})

    return {a for a in aliases if a}


def _short_event_codes(event) -> set[str]:
    """Return short token codes frequently used in manual notes/spreadsheets."""
    name = normalize_event_text(getattr(event, 'name', ''))
    display = normalize_event_text(getattr(event, 'display_name', ''))
    combined = f'{name} {display}'
    codes = set()
    if 'underhand' in combined:
        codes.add('uh')
    if 'obstaclepole' in combined:
        codes.add('op')
    if 'hotsaw' in combined:
        codes.add('hs')
    if 'springboard' in combined:
        codes.add('sb')
    if 'singlebuck' in combined:
        codes.update({'sbu', 'singlebuck'})
    if 'doublebuck' in combined:
        codes.add('db')
    if 'poleclimb' in combined or 'speedclimb' in combined:
        codes.update({'pc', 'sc'})
    if 'stocksaw' in combined:
        codes.add('ss')
    if 'cookiestack' in combined:
        codes.add('cs')
    return codes


def event_matches_gear_key(event, raw_key: str) -> bool:
    """Return True when a stored gear-sharing key applies to the given event."""
    if event is None:
        return False

    key = str(raw_key or '').strip().lower()
    if not key:
        return False
    if key == str(getattr(event, 'id', '')).strip():
        return True

    if key in _CATEGORY_KEYS:
        stand_type = str(getattr(event, 'stand_type', '') or '').strip().lower()
        event_text = normalize_event_text(getattr(event, 'display_name', '') or getattr(event, 'name', ''))
        if key == 'category:crosscut':
            return stand_type == 'saw_hand' or any(token in event_text for token in ['buck', 'jackjill', 'saw'])
        if key == 'category:chainsaw':
            return stand_type in {'hot_saw', 'stock_saw'} or any(token in event_text for token in ['hotsaw', 'stocksaw', 'powersaw', 'chainsaw'])
        if key == 'category:springboard':
            return stand_type == 'springboard' or 'springboard' in event_text or '1board' in event_text
        return False

    norm_key = normalize_event_text(key)
    return norm_key in _event_name_aliases(event)


def parse_gear_sharing_details(
    details_text: str,
    event_pool: list,
    name_index: dict[str, str],
    self_name: str = '',
    entered_event_names: list[str] | None = None,
) -> tuple[dict[str, str], list[str]]:
    """
    Parse free-text gear sharing detail text into a structured mapping.

    Returns:
        (gear_map, warnings)
    where gear_map is dict(event_key -> canonical partner name).
    """
    text = str(details_text or '').strip()
    if not text:
        return {}, ['missing_details']

    warnings: list[str] = []
    parsed: dict[str, str] = {}
    lowered = text.lower()
    self_norm = normalize_person_name(self_name)
    entered_norm = {normalize_event_text(v) for v in (entered_event_names or []) if str(v or '').strip()}

    # Detect partner name from known competitor names mentioned in details.
    mentioned = []
    for norm_name, canonical_name in name_index.items():
        if norm_name and norm_name in normalize_person_name(lowered):
            if norm_name != self_norm:
                mentioned.append((len(norm_name), canonical_name))
    partner_name = ''
    if mentioned:
        mentioned.sort(reverse=True)
        partner_name = mentioned[0][1]

    # Fallback: read first segment before separator as partner candidate.
    if not partner_name:
        first_segment = re.split(r'[-—:;,]', text, maxsplit=1)[0].strip()
        first_segment = re.sub(r'\b(sharing|with|gear|events?)\b', '', first_segment, flags=re.IGNORECASE).strip()
        if len(first_segment.split()) >= 2:
            partner_name = resolve_partner_name(first_segment, name_index)

    if not partner_name:
        warnings.append('partner_not_resolved')
        return {}, warnings

    # Event-specific extraction from known event aliases.
    matched_any_event = False
    normalized_text = normalize_event_text(text)
    raw_tokens = set(re.findall(r'[a-z0-9]+', lowered))

    candidates = list(event_pool)
    if entered_norm:
        filtered = []
        for event in candidates:
            aliases = _event_name_aliases(event)
            if any(a in entered_norm for a in aliases):
                filtered.append(event)
        if filtered:
            candidates = filtered

    sb_matches = [event for event in candidates if 'sb' in _short_event_codes(event)]
    for event in candidates:
        aliases = _event_name_aliases(event)
        short_codes = _short_event_codes(event)
        alias_match = any(alias and alias in normalized_text for alias in aliases if len(alias) >= 4)
        short_match = any(code in raw_tokens for code in short_codes)

        # "SB" is ambiguous; only accept when narrowed by entered-events context.
        if short_match and 'sb' in short_codes and len(sb_matches) > 1 and not entered_norm:
            short_match = False

        if alias_match or short_match:
            parsed[str(event.id)] = partner_name
            matched_any_event = True

    # Equipment category extraction when explicit event names are absent/incomplete.
    categories = infer_equipment_categories(text)
    for category in categories:
        parsed[f'category:{category}'] = partner_name

    if not matched_any_event and not categories:
        warnings.append('events_not_resolved')

    return parsed, warnings


def competitors_share_gear_for_event(comp1_name: str, comp1_gear: dict, comp2_name: str, comp2_gear: dict, event) -> bool:
    """Check if two competitors have a gear-sharing conflict for the given event."""
    sharing1 = comp1_gear if isinstance(comp1_gear, dict) else {}
    sharing2 = comp2_gear if isinstance(comp2_gear, dict) else {}
    name1 = normalize_person_name(comp1_name)
    name2 = normalize_person_name(comp2_name)

    if event is None:
        for value in sharing1.values():
            partner1 = normalize_person_name(str(value or '').strip())
            if partner1 and partner1 == name2:
                return True
        for value in sharing2.values():
            partner2 = normalize_person_name(str(value or '').strip())
            if partner2 and partner2 == name1:
                return True

    for key1, value1 in sharing1.items():
        if not event_matches_gear_key(event, key1):
            continue
        partner1 = normalize_person_name(str(value1 or '').strip())
        if not partner1:
            continue
        if partner1 == name2:
            return True
        if partner1.startswith('group:'):
            for key2, value2 in sharing2.items():
                if event_matches_gear_key(event, key2) and str(value2 or '').strip() == str(value1 or '').strip():
                    return True

    for key2, value2 in sharing2.items():
        if not event_matches_gear_key(event, key2):
            continue
        partner2 = normalize_person_name(str(value2 or '').strip())
        if partner2 == name1:
            return True

    return False


# ---------------------------------------------------------------------------
# Bidirectional sync primitives
# ---------------------------------------------------------------------------

def sync_gear_bidirectional(comp_a, comp_b, event_key: str) -> None:
    """
    Write gear-sharing entries on both sides.
      comp_a.gear_sharing[event_key] = comp_b.name
      comp_b.gear_sharing[event_key] = comp_a.name
    Caller must commit.
    """
    sharing_a = comp_a.get_gear_sharing()
    sharing_a[event_key] = str(comp_b.name or '').strip()
    comp_a.gear_sharing = json.dumps(sharing_a)

    sharing_b = comp_b.get_gear_sharing()
    sharing_b[event_key] = str(comp_a.name or '').strip()
    comp_b.gear_sharing = json.dumps(sharing_b)


def normalize_gear_key_to_event_id(raw_key: str, pro_events: list) -> str:
    """
    Try to resolve raw_key to a numeric event ID string.
    Category keys (category:*) and group keys (group:*) pass through unchanged.
    Falls back to raw_key if no match.
    """
    key = str(raw_key or '').strip()
    if not key or key.isdigit() or key.startswith('category:') or key.startswith('group:'):
        return key
    norm = normalize_event_text(key)
    for event in pro_events:
        if norm in _event_name_aliases(event):
            return str(event.id)
    return key


def sync_all_gear_for_competitor(comp, pro_comps_by_norm: dict, old_gear: dict | None = None) -> None:
    """
    After updating comp's gear_sharing, write reciprocals on all referenced
    partners.  If old_gear is given, also clear removed entries from partners.
    Caller must commit.
    """
    gear = comp.get_gear_sharing()
    comp_norm = normalize_person_name(comp.name)

    # Write reciprocals for current entries.
    for key, partner_text in gear.items():
        pt = str(partner_text or '').strip()
        if not pt or pt.startswith('group:'):
            continue
        partner_comp = pro_comps_by_norm.get(normalize_person_name(pt))
        if not partner_comp or partner_comp.id == comp.id:
            continue
        partner_gear = partner_comp.get_gear_sharing()
        if normalize_person_name(str(partner_gear.get(key, ''))) != comp_norm:
            partner_gear[key] = comp.name
            partner_comp.gear_sharing = json.dumps(partner_gear)

    # Clear removed entries from partners.
    if old_gear:
        for key in set(old_gear.keys()) - set(gear.keys()):
            for partner_comp in pro_comps_by_norm.values():
                if partner_comp.id == comp.id:
                    continue
                partner_gear = partner_comp.get_gear_sharing()
                if key in partner_gear and normalize_person_name(str(partner_gear.get(key, ''))) == comp_norm:
                    del partner_gear[key]
                    partner_comp.gear_sharing = json.dumps(partner_gear)


# ---------------------------------------------------------------------------
# Group gear sharing (multiple pairs sharing one piece of equipment)
# ---------------------------------------------------------------------------

def create_gear_group(comps: list, event_key: str, group_name: str) -> int:
    """
    Assign all listed competitors to a named gear-sharing group for the event.
    Value stored as 'group:{group_name}'.

    Use this for two or more partnered pairs (e.g. Double Buck or Jack & Jill)
    that share one saw.  The heat generator treats intra-pair as a unit and only
    fires the gear conflict check between units, so pairs sharing a saw will be
    placed in separate heats automatically.

    Caller must commit.  Returns count updated.
    """
    group_value = f'group:{group_name}'
    for comp in comps:
        sharing = comp.get_gear_sharing()
        sharing[event_key] = group_value
        comp.gear_sharing = json.dumps(sharing)
    return len(comps)


def get_gear_groups(tournament) -> dict:
    """
    Return a mapping group_name → list[{competitor, event_key}] for all
    group: gear-sharing entries across active pro competitors.
    """
    from models.competitor import ProCompetitor

    groups: dict = {}
    for comp in ProCompetitor.query.filter_by(tournament_id=tournament.id, status='active').all():
        for key, value in comp.get_gear_sharing().items():
            v = str(value or '').strip()
            if v.startswith('group:'):
                gname = v[len('group:'):]
                groups.setdefault(gname, []).append({'competitor': comp, 'event_key': key})
    return groups


# ---------------------------------------------------------------------------
# Utility batch operations
# ---------------------------------------------------------------------------

def complete_one_sided_pairs(tournament) -> dict:
    """
    Write reciprocal gear-sharing entries for all one-sided pairs (A lists B
    but B does not list A back).  Always treating gear sharing as mutual.
    Caller must commit.  Returns {completed: int}.
    """
    from models.competitor import ProCompetitor

    pro_comps = ProCompetitor.query.filter_by(
        tournament_id=tournament.id, status='active'
    ).all()
    pro_by_norm = {normalize_person_name(c.name): c for c in pro_comps}
    completed = 0

    for comp in pro_comps:
        comp_norm = normalize_person_name(comp.name)
        for key, raw_partner in comp.get_gear_sharing().items():
            pt = str(raw_partner or '').strip()
            if not pt or pt.startswith('group:'):
                continue
            partner_comp = pro_by_norm.get(normalize_person_name(pt))
            if not partner_comp or partner_comp.id == comp.id:
                continue
            partner_gear = partner_comp.get_gear_sharing()
            already = any(normalize_person_name(str(v or '')) == comp_norm for v in partner_gear.values())
            if not already:
                partner_gear[key] = comp.name
                partner_comp.gear_sharing = json.dumps(partner_gear)
                completed += 1

    return {'completed': completed}


def cleanup_scratched_gear_entries(tournament, scratched_competitor=None, competitor_type: str = 'pro') -> dict:
    """
    Remove gear-sharing entries from active competitors that reference scratched
    (or a specific given) competitor.

    competitor_type: 'pro' (default) or 'college' — selects which competitor
    table to scan for active/scratched rows.

    Caller must commit.  Returns {cleaned: int, affected: list[str]}.
    """
    if competitor_type == 'college':
        from models.competitor import CollegeCompetitor as CompModel
    else:
        from models.competitor import ProCompetitor as CompModel

    active_comps = CompModel.query.filter_by(
        tournament_id=tournament.id, status='active'
    ).all()
    if scratched_competitor:
        scratched_norms = {normalize_person_name(scratched_competitor.name)}
    else:
        scratched_norms = {
            normalize_person_name(s.name)
            for s in CompModel.query.filter_by(tournament_id=tournament.id, status='scratched').all()
        }

    cleaned = 0
    affected: list = []
    for comp in active_comps:
        gear = comp.get_gear_sharing()
        updated = {k: v for k, v in gear.items()
                   if normalize_person_name(str(v or '')) not in scratched_norms}
        if len(updated) != len(gear):
            comp.gear_sharing = json.dumps(updated)
            cleaned += len(gear) - len(updated)
            if comp.name not in affected:
                affected.append(comp.name)

    return {'cleaned': cleaned, 'affected': affected}


def auto_populate_partners_from_gear(tournament) -> dict:
    """
    For each pro competitor, copy gear_sharing entries into the partners dict
    for partnered events without overwriting existing partner entries.
    Caller must commit.  Returns {updated: int}.
    """
    from models.competitor import ProCompetitor
    from models import Event

    partnered_events = Event.query.filter_by(
        tournament_id=tournament.id, event_type='pro', is_partnered=True
    ).all()
    # Build alias → event_id lookup (include numeric ID itself).
    alias_to_id: dict = {}
    for e in partnered_events:
        alias_to_id[str(e.id)] = str(e.id)
        for alias in _event_name_aliases(e):
            alias_to_id[alias] = str(e.id)

    updated = 0
    for comp in ProCompetitor.query.filter_by(tournament_id=tournament.id, status='active').all():
        gear = comp.get_gear_sharing()
        partners = comp.get_partners()
        changed = False
        for key, partner_text in gear.items():
            pt = str(partner_text or '').strip()
            if not pt or pt.startswith('group:'):
                continue
            resolved = alias_to_id.get(key) or alias_to_id.get(normalize_event_text(key))
            if resolved and not str(partners.get(resolved, '')).strip():
                partners[resolved] = pt
                changed = True
        if changed:
            comp.partners = json.dumps(partners)
            updated += 1

    return {'updated': updated}


def build_parse_review(tournament) -> list:
    """
    Return proposed gear_sharing parse results for competitors whose
    gear_sharing_details are unstructured, WITHOUT committing.

    Returns list of dicts per competitor:
        {competitor, details_text, proposed_gear_map, warnings,
         already_structured, event_labels}
    """
    from models.competitor import ProCompetitor
    from models import Event

    pro_comps = ProCompetitor.query.filter_by(
        tournament_id=tournament.id, status='active'
    ).all()
    pro_events = Event.query.filter_by(tournament_id=tournament.id, event_type='pro').all()
    name_index = build_name_index(c.name for c in pro_comps)
    event_labels = {str(e.id): e.display_name for e in pro_events}

    results = []
    for comp in pro_comps:
        details = str(getattr(comp, 'gear_sharing_details', '') or '').strip()
        if not details:
            continue
        entered = [str(v).strip() for v in comp.get_events_entered() if str(v).strip()]
        gear_map, warnings = parse_gear_sharing_details(
            details, pro_events, name_index,
            self_name=comp.name, entered_event_names=entered,
        )
        results.append({
            'competitor': comp,
            'details_text': details,
            'proposed_gear_map': gear_map,
            'warnings': [w for w in warnings if w != 'missing_details'],
            'already_structured': bool(comp.get_gear_sharing()),
            'event_labels': event_labels,
        })
    return results


def build_gear_completeness_check(event, pro_comps: list) -> dict:
    """
    For a given event, identify active entrants that lack any gear-sharing entry.
    Returns {missing: list[{competitor, reason}], ok_count: int, total: int}.
    """
    event_aliases = _event_name_aliases(event)
    entered = []
    for c in pro_comps:
        for v in c.get_events_entered():
            val = str(v or '').strip()
            if val == str(event.id) or normalize_event_text(val) in event_aliases:
                entered.append(c)
                break

    missing = []
    for comp in entered:
        gear = comp.get_gear_sharing()
        if not any(event_matches_gear_key(event, k) for k in gear):
            missing.append({'competitor': comp, 'reason': 'no gear entry for this event'})

    return {'missing': missing, 'ok_count': len(entered) - len(missing), 'total': len(entered)}


# ---------------------------------------------------------------------------
# Tournament-wide gear audit
# ---------------------------------------------------------------------------

def build_gear_report(tournament) -> dict:
    """
    Build a comprehensive gear-sharing audit for a tournament.

    Returns a dict with:
        pro_pairs         — confirmed bidirectional gear pairs (de-duped)
        pro_unresolved    — one-sided, unknown, missing, or self-reference entries
        pro_conflicts     — pairs whose two competitors appear in the same heat
        unparsed_count    — competitors who have free-text details but no structured map
        college_constraints — all college gear-sharing entries (read-only)
        stats             — summary counts
    """
    from models.competitor import CollegeCompetitor, ProCompetitor
    from models import Event, Heat

    pro_comps = ProCompetitor.query.filter_by(
        tournament_id=tournament.id, status='active'
    ).order_by(ProCompetitor.name).all()

    college_comps = CollegeCompetitor.query.filter_by(
        tournament_id=tournament.id, status='active'
    ).order_by(CollegeCompetitor.name).all()

    pro_events = Event.query.filter_by(tournament_id=tournament.id, event_type='pro').all()
    college_events = Event.query.filter_by(tournament_id=tournament.id, event_type='college').all()

    event_display_pro = {str(e.id): e.display_name for e in pro_events}
    event_display_college = {str(e.id): e.display_name for e in college_events}

    pro_by_norm = {normalize_person_name(c.name): c for c in pro_comps}
    comp_gear_by_id = {c.id: c.get_gear_sharing() for c in pro_comps}

    def _resolve_pro_event_label(key):
        label = event_display_pro.get(str(key))
        if label:
            return label
        # For category keys, join all matching event display names.
        if str(key).startswith('category:'):
            matching = [e.display_name for e in pro_events if event_matches_gear_key(e, key)]
            return ', '.join(matching) if matching else str(key)
        ev = next((e for e in pro_events if event_matches_gear_key(e, key)), None)
        return ev.display_name if ev else str(key)

    # --- Pro gear-sharing analysis ---
    pro_pairs = []
    pro_unresolved = []
    seen_pairs: set = set()

    for comp in pro_comps:
        gear = comp.get_gear_sharing()
        if not isinstance(gear, dict):
            continue

        for key, raw_partner in gear.items():
            event_label = _resolve_pro_event_label(key)
            partner_text = str(raw_partner or '').strip()
            partner_norm = normalize_person_name(partner_text)
            partner_comp = pro_by_norm.get(partner_norm)

            status = 'ok'
            issues = []

            if not partner_text:
                status = 'missing_partner'
                issues.append('No partner name specified')
            elif partner_norm == normalize_person_name(comp.name):
                status = 'self_reference'
                issues.append('Entry references the competitor themselves')
            elif not partner_comp:
                status = 'unknown_partner'
                issues.append(f'"{partner_text}" is not on the active roster')
            else:
                # Check for reciprocal entry on partner's side.
                partner_gear = partner_comp.get_gear_sharing()
                reciprocal = any(
                    normalize_person_name(str(v or '')) == normalize_person_name(comp.name)
                    for k, v in partner_gear.items()
                )
                if not reciprocal:
                    status = 'one_sided'
                    issues.append('Partner has no matching gear-sharing entry pointing back')

            # Treat ok and one_sided as verified pairs — gear sharing is always
            # considered reciprocal even when only one side has it recorded.
            if status in ('ok', 'one_sided') and partner_comp is not None:
                # De-dupe: only the lower-ID competitor owns the entry.
                if comp.id < partner_comp.id:
                    seen_pairs.add((comp.id, partner_comp.id))
                    pro_pairs.append({
                        'comp_a': comp,
                        'comp_b': partner_comp,
                        'event_key': key,
                        'event_label': event_label,
                        'heat_conflict': False,
                        # 'mutual' = both sides explicitly recorded;
                        # 'inferred' = only one side recorded but treated as mutual.
                        'paired_by': 'mutual' if status == 'ok' else 'inferred',
                    })
            elif status not in ('ok', 'one_sided'):
                pro_unresolved.append({
                    'competitor': comp,
                    'event_key': key,
                    'event_label': event_label,
                    'partner_raw': partner_text,
                    'partner_comp': partner_comp,
                    'status': status,
                    'issues': issues,
                })

    # --- Heat conflict detection ---
    pro_conflicts = []
    conflict_pair_ids: set = set()

    for event in pro_events:
        heats = event.heats.filter(Heat.status != 'completed').all()
        for heat in heats:
            comp_ids = heat.get_competitors()
            heat_comps = [c for c in pro_comps if c.id in comp_ids]
            for i, c1 in enumerate(heat_comps):
                for c2 in heat_comps[i + 1:]:
                    if competitors_share_gear_for_event(
                        c1.name, comp_gear_by_id.get(c1.id, {}),
                        c2.name, comp_gear_by_id.get(c2.id, {}),
                        event,
                    ):
                        pro_conflicts.append({
                            'event': event,
                            'heat': heat,
                            'comp_a': c1,
                            'comp_b': c2,
                        })
                        conflict_pair_ids.add((min(c1.id, c2.id), max(c1.id, c2.id)))

    for pair in pro_pairs:
        pk = (min(pair['comp_a'].id, pair['comp_b'].id), max(pair['comp_a'].id, pair['comp_b'].id))
        if pk in conflict_pair_ids:
            pair['heat_conflict'] = True

    # --- Gear group size validation ---
    # For partnered events a gear group should have exactly 2 members (one pair per saw).
    # Warn when a group for a partnered event has 1 or 3+ members.
    partnered_event_ids = {str(e.id) for e in pro_events if getattr(e, 'is_partnered', False)}
    group_warnings: list[dict] = []
    raw_groups: dict[str, dict] = {}  # group_name -> {event_key, members}
    for comp in pro_comps:
        for key, value in comp.get_gear_sharing().items():
            v = str(value or '').strip()
            if not v.startswith('group:'):
                continue
            gname = v[len('group:'):]
            if gname not in raw_groups:
                raw_groups[gname] = {'event_key': key, 'members': []}
            raw_groups[gname]['members'].append(comp.name)
    for gname, gdata in raw_groups.items():
        key = gdata['event_key']
        members = gdata['members']
        is_partnered_key = key in partnered_event_ids or key.startswith('category:crosscut')
        if is_partnered_key and len(members) != 2:
            group_warnings.append({
                'group_name': gname,
                'event_key': key,
                'event_label': _resolve_pro_event_label(key),
                'member_count': len(members),
                'members': members,
                'issue': f'Expected 2 members (one pair per saw), found {len(members)}.',
            })

    # --- Unparsed free-text details ---
    unparsed_count = sum(
        1 for c in pro_comps
        if str(getattr(c, 'gear_sharing_details', '') or '').strip()
        and not c.get_gear_sharing()
    )

    # --- College gear constraints (read-only) ---
    college_constraints = []
    for comp in college_comps:
        gear = comp.get_gear_sharing()
        if not isinstance(gear, dict) or not gear:
            continue
        team_code = comp.team.team_code if comp.team else ''
        for key, partner in gear.items():
            label = event_display_college.get(str(key))
            if not label:
                ev = next((e for e in college_events if event_matches_gear_key(e, key)), None)
                label = ev.display_name if ev else str(key)
            college_constraints.append({
                'competitor': comp,
                'team_code': team_code,
                'event_key': key,
                'event_label': label,
                'partner': str(partner or '').strip(),
            })

    return {
        'pro_pairs': pro_pairs,
        'pro_unresolved': pro_unresolved,
        'pro_conflicts': pro_conflicts,
        'group_warnings': group_warnings,
        'unparsed_count': unparsed_count,
        'college_constraints': college_constraints,
        'stats': {
            'pairs': len(pro_pairs),
            'unresolved': len(pro_unresolved),
            'conflicts': len(pro_conflicts),
            'college': len(college_constraints),
            'group_warnings': len(group_warnings),
        },
    }


# ---------------------------------------------------------------------------
# Batch free-text parser
# ---------------------------------------------------------------------------

def parse_all_gear_details(tournament) -> dict:
    """
    Parse gear_sharing_details free-text on each active ProCompetitor that has
    no structured gear_sharing data yet, populating the gear_sharing JSON column.

    Caller is responsible for db.session.commit().
    Returns a summary dict: parsed, skipped, warnings.
    """
    from models.competitor import ProCompetitor
    from models import Event

    pro_comps = ProCompetitor.query.filter_by(
        tournament_id=tournament.id, status='active'
    ).all()
    pro_events = Event.query.filter_by(tournament_id=tournament.id, event_type='pro').all()
    name_index = build_name_index(c.name for c in pro_comps)

    parsed_count = 0
    skipped_count = 0
    warning_rows = []

    for comp in pro_comps:
        details = str(getattr(comp, 'gear_sharing_details', '') or '').strip()
        if not details:
            continue

        existing = comp.get_gear_sharing()
        if existing:
            skipped_count += 1
            continue

        entered_event_names = [str(v).strip() for v in comp.get_events_entered() if str(v).strip()]

        gear_map, warnings = parse_gear_sharing_details(
            details,
            pro_events,
            name_index,
            self_name=comp.name,
            entered_event_names=entered_event_names,
        )

        if gear_map:
            comp.gear_sharing = json.dumps(gear_map)
            parsed_count += 1

        non_trivial = [w for w in warnings if w not in ('missing_details',)]
        if non_trivial:
            warning_rows.append({'name': comp.name, 'warnings': non_trivial})

    return {
        'parsed': parsed_count,
        'skipped': skipped_count,
        'warnings': warning_rows,
    }


# ---------------------------------------------------------------------------
# Heat conflict auto-fix
# ---------------------------------------------------------------------------

def fix_heat_gear_conflicts(tournament) -> dict:
    """
    Detect gear-sharing conflicts in existing pending/in-progress heats and
    attempt to resolve each by moving one competitor to a compatible heat in
    the same event run.

    Only touches heats that are not yet completed.
    Caller is responsible for db.session.commit().
    Returns: {fixed: int, failed: list[dict]}
    """
    import config
    from database import db
    from models.competitor import ProCompetitor
    from models import Event, Heat

    pro_comps = ProCompetitor.query.filter_by(
        tournament_id=tournament.id, status='active'
    ).all()
    comp_by_id = {c.id: c for c in pro_comps}
    comp_gear_by_id = {c.id: c.get_gear_sharing() for c in pro_comps}

    fixed = 0
    failed = []

    for event in Event.query.filter_by(tournament_id=tournament.id, event_type='pro').all():
        heats = event.heats.filter(
            Heat.status != 'completed'
        ).order_by(Heat.run_number, Heat.heat_number).all()
        if not heats:
            continue

        stand_config = config.STAND_CONFIGS.get(event.stand_type, {})
        max_per_heat = (
            event.max_stands
            if event.max_stands is not None
            else stand_config.get('total', 4)
        )

        # Group heats by run_number so a move stays within the same run.
        by_run: dict[int, list] = {}
        for h in heats:
            by_run.setdefault(h.run_number or 1, []).append(h)

        for run_heats in by_run.values():
            # Multi-pass: keep iterating until no more fixes are possible.
            for _pass in range(len(run_heats) * len(run_heats) + 1):
                made_fix_this_pass = False

                for heat in run_heats:
                    comp_ids = heat.get_competitors()
                    heat_comps = [comp_by_id[cid] for cid in comp_ids if cid in comp_by_id]

                    # Find the first gear conflict in this heat.
                    conflict_pair = None
                    for i, c1 in enumerate(heat_comps):
                        for c2 in heat_comps[i + 1:]:
                            if competitors_share_gear_for_event(
                                c1.name, comp_gear_by_id.get(c1.id, {}),
                                c2.name, comp_gear_by_id.get(c2.id, {}),
                                event,
                            ):
                                conflict_pair = (c1, c2)
                                break
                        if conflict_pair:
                            break

                    if not conflict_pair:
                        continue

                    mover = conflict_pair[1]

                    # Score all candidate target heats; pick the best.
                    # Score = remaining capacity - (new conflicts mover would create).
                    # Only heats with score >= 0 (capacity available, no new conflicts) qualify.
                    best_target = None
                    best_score = -1

                    for target_heat in run_heats:
                        if target_heat.id == heat.id:
                            continue
                        target_ids = target_heat.get_competitors()
                        if len(target_ids) >= max_per_heat:
                            continue
                        target_comps = [comp_by_id[cid] for cid in target_ids if cid in comp_by_id]
                        new_conflicts = sum(
                            1 for tc in target_comps
                            if competitors_share_gear_for_event(
                                mover.name, comp_gear_by_id.get(mover.id, {}),
                                tc.name, comp_gear_by_id.get(tc.id, {}),
                                event,
                            )
                        )
                        if new_conflicts > 0:
                            continue
                        score = max_per_heat - len(target_ids)
                        if score > best_score:
                            best_score = score
                            best_target = target_heat

                    if best_target is None:
                        # No valid target for this conflict right now; move on.
                        continue

                    target_ids = best_target.get_competitors()

                    # Remove mover from source heat.
                    heat.set_competitors([cid for cid in comp_ids if cid != mover.id])
                    src_assignments = heat.get_stand_assignments()
                    src_assignments.pop(str(mover.id), None)
                    heat.stand_assignments = json.dumps(src_assignments)

                    # Add mover to target heat.
                    best_target.set_competitors(target_ids + [mover.id])
                    tgt_assignments = best_target.get_stand_assignments()
                    used_stands = {int(v) for v in tgt_assignments.values() if str(v).lstrip('-').isdigit()}
                    next_stand = 1
                    while next_stand in used_stands:
                        next_stand += 1
                    tgt_assignments[str(mover.id)] = next_stand
                    best_target.stand_assignments = json.dumps(tgt_assignments)

                    db.session.flush()
                    heat.sync_assignments('pro')
                    best_target.sync_assignments('pro')

                    fixed += 1
                    made_fix_this_pass = True
                    break  # Restart full scan after each move.

                if not made_fix_this_pass:
                    break  # No more fixes possible for this run.

            # Record any remaining un-fixable conflicts.
            for heat in run_heats:
                comp_ids = heat.get_competitors()
                heat_comps = [comp_by_id[cid] for cid in comp_ids if cid in comp_by_id]
                for i, c1 in enumerate(heat_comps):
                    for c2 in heat_comps[i + 1:]:
                        if competitors_share_gear_for_event(
                            c1.name, comp_gear_by_id.get(c1.id, {}),
                            c2.name, comp_gear_by_id.get(c2.id, {}),
                            event,
                        ):
                            failed.append({
                                'event': event.display_name,
                                'heat': heat.heat_number,
                                'comp_a': c1.name,
                                'comp_b': c2.name,
                            })

    return {'fixed': fixed, 'failed': failed}
