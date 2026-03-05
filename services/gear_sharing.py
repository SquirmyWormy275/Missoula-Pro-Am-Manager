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
    """Resolve raw partner text to the closest known competitor name when possible."""
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
