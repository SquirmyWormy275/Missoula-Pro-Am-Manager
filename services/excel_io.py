"""
Excel import/export service for college and pro registration.
Maintains compatibility with existing college entry form format.
"""
import pandas as pd

import config
from database import db
from models import CollegeCompetitor, ProCompetitor, Team, Tournament
from services.gear_sharing import infer_equipment_categories, normalize_person_name


def process_college_entry_form(filepath: str, tournament: Tournament, original_filename: str = None) -> dict:
    """
    Process a college entry form Excel file and import teams/competitors.

    Expected format (based on existing college entry form):
    - Sheet contains team and competitor information
    - Columns should include: Name, Gender, Events, Partners

    Args:
        filepath: Path to the Excel file
        tournament: Tournament to add teams/competitors to
        original_filename: Original uploaded filename (e.g., "University of Montana.xlsx")

    Returns:
        dict with counts: {'teams': int, 'competitors': int}
    """
    # Read raw sheet first so we can detect where headers actually start.
    try:
        raw_df = pd.read_excel(filepath, sheet_name=0, header=None)
    except Exception as e:
        raise ValueError(f"Could not read Excel file: {str(e)}")

    header_row = _detect_header_row(raw_df)
    if header_row is None:
        raise ValueError("Could not find header row in the entry form")

    # Read again using detected header row.
    df = pd.read_excel(filepath, sheet_name=0, header=header_row)
    df = df.dropna(axis=1, how='all')
    df.columns = [str(c).strip() for c in df.columns]
    # Extract school name: prefer filename (e.g., "University of Montana.xlsx"), fall back to preamble/data
    default_school_name = _school_name_from_filename(original_filename) or _extract_school_name(raw_df, header_row)

    # Detect the format and process accordingly
    if _find_column(df, ['school', 'university', 'college', 'institution']) or _find_column(df, ['team', 'team code']):
        return _process_standard_entry_form(df, tournament, default_school_name=default_school_name)
    else:
        # Try to infer format
        return _process_inferred_format(df, tournament, default_school_name=default_school_name)


def _process_standard_entry_form(df: pd.DataFrame, tournament: Tournament, default_school_name: str = None) -> dict:
    """Process a standard format entry form with school/team columns."""
    teams_created = 0
    competitors_created = 0
    touched_team_ids = set()

    # Get column mappings (flexible to handle variations)
    school_col = _find_column(df, ['school', 'university', 'college', 'institution'])
    team_col = _find_column(df, ['team', 'team_code', 'team_id', 'team name'])
    # Fallback: detect unnamed columns whose values look like team identifiers (e.g., "A Team", "B Team")
    if not team_col:
        team_col = _detect_team_column_by_values(df)
    name_col = _find_column(df, ['name', 'first and last name', 'competitor', 'athlete', 'full name', 'competitor name'])
    gender_col = _find_column(df, ['gender', 'sex', 'm/f', 'male/female', 'male female', 'mf'])
    events_col = _find_column(df, ['events', 'event', 'entered'])
    relay_lottery_col = _find_column(df, ['pro-am relay lottery', 'pro am relay lottery', 'pro-am lottery', 'relay lottery'])
    event_marker_cols = _find_event_marker_columns(
        df,
        excluded_cols=[school_col, team_col, name_col, gender_col, events_col, relay_lottery_col]
    )
    default_gender = _infer_default_gender(df, gender_col)

    if not name_col:
        raise ValueError("Could not find name column in the entry form")

    # Group by team if team column exists
    if team_col:
        grouped = df.groupby(team_col, sort=False)
    elif school_col:
        grouped = df.groupby(school_col, sort=False)
    else:
        # Treat entire file as one team
        grouped = [(df.iloc[0].get(school_col, 'Unknown'), df)]

    last_real_team = None

    for team_identifier, team_df in grouped:
        # Skip empty groups
        if len(team_df) == 0:
            continue
        # Ignore note/placeholder groups with no valid competitor names.
        valid_team_df = team_df[team_df[name_col].apply(_is_valid_competitor_name)]
        if len(valid_team_df) == 0:
            note = _extract_gear_sharing_note(team_identifier, team_df, school_col)
            if note and last_real_team is not None:
                _apply_gear_sharing_note_to_team(last_real_team, note)
            continue

        # Determine school and team code
        raw_team_id = str(team_identifier).strip()
        # Resolve school name: prefer School column value, then filename-derived default
        if school_col:
            raw_school = str(valid_team_df[school_col].iloc[0]).strip() if not pd.isna(valid_team_df[school_col].iloc[0]) else ''
        else:
            raw_school = ''
        # The School column may already contain a team code (e.g., "UM-A") — use default_school_name if available
        school_name = default_school_name or raw_school or raw_team_id
        school_abbr = _abbreviate_school(school_name)
        # Extract team letter from identifiers like "A Team", "B Team" or use raw_team_id
        team_letter = _extract_team_letter(raw_team_id)
        if team_letter:
            team_code = f"{school_abbr}-{team_letter}"
        elif _looks_like_team_code(raw_team_id):
            team_code = raw_team_id
        else:
            team_code = f"{school_abbr}-A"

        # Create or find team
        team = Team.query.filter_by(
            tournament_id=tournament.id,
            team_code=team_code
        ).first()

        if not team:
            team = Team(
                tournament_id=tournament.id,
                team_code=team_code,
                school_name=school_name,
                school_abbreviation=_abbreviate_school(school_name)
            )
            db.session.add(team)
            db.session.flush()  # Get the ID
            teams_created += 1
        last_real_team = team
        touched_team_ids.add(team.id)

        # Add competitors to team
        for _, row in valid_team_df.iterrows():
            name = row.get(name_col)
            if pd.isna(name) or not str(name).strip():
                continue

            gender = _resolve_row_gender(row, gender_col, default_gender, event_marker_cols)

            # Check if competitor already exists
            existing = CollegeCompetitor.query.filter_by(
                tournament_id=tournament.id,
                team_id=team.id,
                name=str(name).strip()
            ).first()

            relay_opt_in = _parse_relay_opt_in(row.get(relay_lottery_col)) if relay_lottery_col else False

            if not existing:
                competitor = CollegeCompetitor(
                    tournament_id=tournament.id,
                    team_id=team.id,
                    name=str(name).strip(),
                    gender=gender
                )

                # Process events if column exists
                events = []
                if events_col and not pd.isna(row.get(events_col)):
                    events = _parse_events(row.get(events_col))
                elif event_marker_cols:
                    events = _parse_event_markers(row, event_marker_cols)

                # Process partnered-event partner columns and ensure paired events are included.
                pairings = _extract_partner_entries(row, list(df.columns))
                for event_name in pairings.keys():
                    if event_name not in events:
                        events.append(event_name)
                competitor.set_events_entered(sorted(set(events)))
                for event_name, partner_name in pairings.items():
                    competitor.set_partner(event_name, partner_name)

                # Process partners if column exists
                partners_col = _find_column(df, ['partner', 'partners', 'partner name'])
                if partners_col and not pd.isna(row.get(partners_col)):
                    _process_partners(competitor, row.get(partners_col))

                competitor.pro_am_lottery_opt_in = relay_opt_in
                db.session.add(competitor)
                competitors_created += 1
            else:
                existing.gender = gender
                existing.pro_am_lottery_opt_in = relay_opt_in

    db.session.flush()
    errors_by_team = _validate_college_entry_constraints(touched_team_ids)

    invalid_count = 0
    valid_count = 0
    for team_id in touched_team_ids:
        team = Team.query.get(team_id)
        if not team:
            continue
        team_errors = errors_by_team.get(team_id, [])
        if team_errors:
            team.set_validation_errors(team_errors)
            invalid_count += 1
        else:
            team.validation_errors = '[]'
            team.status = 'active'
            valid_count += 1

    db.session.commit()

    return {
        'teams': valid_count,
        'invalid_teams': invalid_count,
        'competitors': competitors_created,
    }


def _process_inferred_format(df: pd.DataFrame, tournament: Tournament, default_school_name: str = None) -> dict:
    """Try to infer format from column structure."""
    # Fallback processing - try common patterns
    name_col = _find_column(df, ['name', 'first and last name', 'competitor', 'athlete', 'full name', 'participant'])

    if not name_col:
        # Try first column
        name_col = df.columns[0]

    return _process_standard_entry_form(df, tournament, default_school_name=default_school_name)


def _find_column(df: pd.DataFrame, candidates: list) -> str:
    """Find a column matching one of the candidate names."""
    normalized_to_original = {_normalize_label(c): c for c in df.columns}

    # Exact normalized match first.
    for candidate in candidates:
        normalized_candidate = _normalize_label(candidate)
        if normalized_candidate in normalized_to_original:
            return normalized_to_original[normalized_candidate]

    # Then tolerant "contains" checks for messy headers.
    for column in df.columns:
        normalized_column = _normalize_label(column)
        for candidate in candidates:
            normalized_candidate = _normalize_label(candidate)
            if normalized_candidate and normalized_candidate in normalized_column:
                return column

    return None


def _detect_team_column_by_values(df: pd.DataFrame) -> str:
    """Detect an unnamed column whose values look like team identifiers (e.g., 'A Team', 'B Team')."""
    import re
    team_pattern = re.compile(r'^[a-d]\s*team$', re.IGNORECASE)
    for col in df.columns:
        normalized_header = _normalize_label(col)
        if not normalized_header.startswith('unnamed'):
            continue
        values = df[col].dropna().astype(str).str.strip()
        non_empty = values[values != '']
        if len(non_empty) == 0:
            continue
        # Check if a meaningful fraction of values match the team pattern
        match_count = sum(1 for v in non_empty if team_pattern.match(v))
        if match_count >= 2 or (match_count >= 1 and match_count / len(non_empty) >= 0.3):
            return col
    return None


def _normalize_label(value) -> str:
    """Normalize a header label for tolerant matching."""
    import re
    text = '' if value is None else str(value).strip().lower()
    text = re.sub(r'[^a-z0-9]+', ' ', text)
    text = re.sub(r'\s+', ' ', text).strip()
    return text


def _detect_header_row(raw_df: pd.DataFrame):
    """Find the row index that contains the real table headers."""
    max_scan = min(len(raw_df), 50)
    for idx in range(max_scan):
        row_values = [v for v in raw_df.iloc[idx].tolist() if pd.notna(v) and str(v).strip()]
        if not row_values:
            continue

        normalized = [_normalize_label(v) for v in row_values]
        has_name = any(
            ('name' in value) or ('competitor' in value) or ('athlete' in value) or ('participant' in value)
            for value in normalized
        )
        has_team_or_school = any(
            ('school' in value) or ('team' in value) or ('college' in value) or ('university' in value)
            for value in normalized
        )
        if has_name and has_team_or_school:
            return idx
    return None


def _extract_school_name(raw_df: pd.DataFrame, header_row: int) -> str:
    """Try to read school name from preamble rows above headers."""
    for idx in range(max(0, header_row - 1), -1, -1):
        row = raw_df.iloc[idx].tolist()
        for value in row:
            if pd.isna(value):
                continue
            text = str(value).strip()
            if not text:
                continue
            normalized = _normalize_label(text)
            if any(k in normalized for k in ['name', 'school', 'team', 'event', 'gear']):
                continue
            return text
    return None


def _school_name_from_filename(filename: str) -> str:
    """Extract school name from an uploaded filename (e.g., 'University of Montana.xlsx' → 'University of Montana')."""
    if not filename:
        return None
    import os
    name = os.path.splitext(filename)[0].strip()
    # Remove common prefixes/suffixes that aren't part of the school name
    for noise in ['entry form', 'entry', 'roster', 'registration', 'form', 'team', 'college', 'pro am', 'pro-am']:
        name = name.replace(noise, '').replace(noise.title(), '').replace(noise.upper(), '')
    name = name.strip(' -_')
    return name if name else None


def _extract_team_letter(raw_team_id: str) -> str:
    """Extract team letter from identifiers like 'A Team', 'B Team', 'Team A', etc."""
    import re
    text = raw_team_id.strip()
    # "A Team", "B Team", etc.
    m = re.match(r'^([A-Da-d])\s*[Tt]eam$', text)
    if m:
        return m.group(1).upper()
    # "Team A", "Team B", etc.
    m = re.match(r'^[Tt]eam\s*([A-Da-d])$', text)
    if m:
        return m.group(1).upper()
    return None


def _looks_like_team_code(value: str) -> bool:
    """Return True when value looks like a team code (e.g., UM-A, JT-B)."""
    import re
    return bool(re.match(r'^[A-Za-z]{2,6}[- ][A-Za-z0-9]{1,3}$', str(value).strip()))


def _find_event_marker_columns(df: pd.DataFrame, excluded_cols: list) -> list:
    """Detect event columns where entries are marked with x/yes/1."""
    excluded = {c for c in excluded_cols if c}
    marker_cols = []
    for col in df.columns:
        if col in excluded:
            continue
        normalized = _normalize_label(col)
        if not normalized or normalized.startswith('unnamed'):
            continue
        # Event headers usually include short labels like "W."/"M." or event keywords.
        if any(k in normalized for k in ['horiz', 'vert', 'pole', 'climb', 'choker', 'saw', 'birling', 'kaber', 'caber', 'chop', 'buck', 'toss', 'hit', 'speed', 'axe', 'throw', 'pv', 'peavey', 'log roll', 'pulp', 'power', 'obstacle', 'single']):
            marker_cols.append(col)
    return marker_cols


def _parse_event_markers(row: pd.Series, event_columns: list) -> list:
    """Convert x/yes/1 style event markers into event labels."""
    selected = []
    for col in event_columns:
        value = row.get(col)
        if pd.isna(value):
            continue
        marker = str(value).strip().lower()
        if marker in {'x', 'y', 'yes', '1', 'true', 't'}:
            selected.append(_canonicalize_event_name(str(col).strip()))
    return sorted(set(e for e in selected if e))


def _infer_default_gender(df: pd.DataFrame, gender_col: str = None) -> str:
    """Infer gender from headers when no gender column exists."""
    if gender_col:
        return 'M'

    female_markers = 0
    male_markers = 0
    for col in df.columns:
        normalized = _normalize_label(col)
        if normalized.startswith('w ') or normalized.startswith('women') or normalized.startswith('female'):
            female_markers += 1
        if normalized.startswith('m ') or normalized.startswith('men') or normalized.startswith('male'):
            male_markers += 1
    if female_markers > male_markers:
        return 'F'
    return 'M'


def _resolve_row_gender(row: pd.Series, gender_col: str, default_gender: str, event_marker_cols: list) -> str:
    """Resolve competitor gender from explicit column first, then event markers."""
    if gender_col:
        raw_gender = row.get(gender_col)
        if not pd.isna(raw_gender) and str(raw_gender).strip():
            return _parse_gender(raw_gender)

    # Fallback: infer from selected event columns for this row.
    selected_event_cols = []
    for col in event_marker_cols or []:
        value = row.get(col)
        if pd.isna(value):
            continue
        marker = str(value).strip().lower()
        if marker in {'x', 'y', 'yes', '1', 'true', 't'}:
            selected_event_cols.append(col)

    female_marks = sum(1 for col in selected_event_cols if _event_column_gender_hint(col) == 'F')
    male_marks = sum(1 for col in selected_event_cols if _event_column_gender_hint(col) == 'M')
    if female_marks > male_marks:
        return 'F'
    if male_marks > female_marks:
        return 'M'

    return _parse_gender(default_gender)


def _event_column_gender_hint(column_name: str):
    """Return 'M'/'F'/None based on an event column header."""
    normalized = _normalize_label(column_name)
    if normalized.startswith('w ') or normalized.startswith('women') or normalized.startswith('female'):
        return 'F'
    if normalized.startswith('m ') or normalized.startswith('men') or normalized.startswith('male'):
        return 'M'
    return None


def _is_valid_competitor_name(value) -> bool:
    """Return True only for real competitor name rows."""
    if pd.isna(value):
        return False
    text = str(value).strip()
    if not text:
        return False
    normalized = _normalize_label(text)
    if any(marker in normalized for marker in ['gear being shared', 'pro am lottery', 'do not count']):
        return False
    if normalized in {'a team', 'b team', 'c team', 'd team', 'team'}:
        return False
    return True


def _extract_gear_sharing_note(team_identifier, team_df: pd.DataFrame, school_col: str = None):
    """Extract possible gear-sharing note text from a non-competitor group."""
    candidates = []

    # Group key can be the note itself when grouped by school/team code.
    if team_identifier is not None and not pd.isna(team_identifier):
        candidates.append(str(team_identifier).strip())

    if school_col and school_col in team_df.columns:
        for value in team_df[school_col].dropna().tolist():
            text = str(value).strip()
            if text:
                candidates.append(text)

    for _, row in team_df.iterrows():
        for value in row.tolist():
            if pd.isna(value):
                continue
            text = str(value).strip()
            if text:
                candidates.append(text)

    for text in candidates:
        normalized = _normalize_label(text)
        if any(marker in normalized for marker in ['crosscut', 'gear', 'share']):
            return text
    return None


def _apply_gear_sharing_note_to_team(team: Team, note_text: str):
    """Map imported gear-sharing notes to team members for heat generation constraints."""
    categories = _infer_gear_categories(note_text)
    explicit_events = _infer_events_from_gear_note(note_text)
    if not categories and not explicit_events:
        return

    active_members = team.members.filter_by(status='active').all()
    target_members = _gear_note_target_members(note_text, active_members)
    if not target_members:
        target_members = active_members

    for competitor in target_members:
        if explicit_events:
            for event_name in explicit_events:
                token = f'group:team:{team.id}:{normalize_person_name(event_name)}'
                competitor.set_gear_sharing(event_name, token)
        for category in categories:
            token = f'group:team:{team.id}:{category}'
            competitor.set_gear_sharing(f'category:{category}', token)


def _infer_gear_categories(note_text: str) -> list:
    """Infer gear categories from free-text notes."""
    return sorted(infer_equipment_categories(note_text))


def _infer_events_from_gear_note(note_text: str) -> list[str]:
    """Extract explicit event names from a gear note."""
    note_norm = _normalize_label(note_text)
    if not note_norm:
        return []
    events = set()
    for cfg in (config.COLLEGE_OPEN_EVENTS + config.COLLEGE_CLOSED_EVENTS):
        canonical = _canonicalize_event_name(cfg.get('name', ''))
        if not canonical:
            continue
        if _normalize_label(canonical) in note_norm:
            events.add(canonical)
    return sorted(events)


def _gear_note_target_members(note_text: str, members: list[CollegeCompetitor]) -> list[CollegeCompetitor]:
    """Return members explicitly mentioned in a gear note."""
    if not note_text:
        return []
    text_norm = normalize_person_name(note_text)
    matches = []
    for member in members:
        name_norm = normalize_person_name(member.name)
        if name_norm and name_norm in text_norm:
            matches.append(member)
    return matches


def _parse_gender(value) -> str:
    """Parse gender value to 'M' or 'F'."""
    if pd.isna(value):
        return 'M'

    value = str(value).strip().upper()
    if value in ['F', 'FEMALE', 'W', 'WOMAN', 'WOMEN']:
        return 'F'
    return 'M'


def _parse_relay_opt_in(value) -> bool:
    """Parse lottery opt-in marker values from import sheets."""
    if pd.isna(value):
        return False
    marker = str(value).strip().lower()
    return marker in {'x', 'y', 'yes', '1', 'true', 't'}


def _generate_team_code(school_name: str, tournament: Tournament) -> str:
    """Generate a unique team code for a school."""
    abbrev = _abbreviate_school(school_name)

    # Count existing teams from this school
    existing = Team.query.filter(
        Team.tournament_id == tournament.id,
        Team.team_code.like(f'{abbrev}-%')
    ).count()

    if existing == 0:
        # Check if there's one without a suffix
        existing_no_suffix = Team.query.filter_by(
            tournament_id=tournament.id,
            team_code=abbrev
        ).first()
        if not existing_no_suffix:
            return f"{abbrev}-A"

    # Generate next letter
    suffix = chr(ord('A') + existing)
    return f"{abbrev}-{suffix}"


def _abbreviate_school(school_name: str) -> str:
    """Create an abbreviation from school name."""
    # Common abbreviations
    abbreviations = {
        'university of montana': 'UM',
        'montana state university': 'MSU',
        'colorado state university': 'CSU',
        'university of idaho': 'UI',
        'idaho': 'UI',
        'oregon state university': 'OSU',
        'university of washington': 'UW',
        'humboldt state': 'HSU',
        'humboldt state university': 'HSU',
        'cal poly': 'CP',
        'cal poly humboldt': 'CPH',
        'uc berkeley': 'UCB',
        'uc berkley': 'UCB',
        'berkeley': 'UCB',
        'berkley': 'UCB',
        'university of california berkeley': 'UCB',
        'flathead valley community college': 'FVCC',
        'flathead valley': 'FVCC',
        'montana tech': 'MTech',
        'university of oregon': 'UO',
        'washington state university': 'WSU',
        'university of british columbia': 'UBC',
        'virginia tech': 'VT',
        'virginia polytechnic': 'VT',
        'northern arizona university': 'NAU',
        'southern oregon university': 'SOU',
    }

    name_lower = school_name.lower().strip()
    if name_lower in abbreviations:
        return abbreviations[name_lower]

    # Generate from initials
    words = school_name.split()
    if len(words) >= 2:
        return ''.join(w[0].upper() for w in words if w.lower() not in ['of', 'the', 'and'])

    return school_name[:3].upper()


def _parse_events(events_str) -> list:
    """Parse events string into list of event names."""
    if pd.isna(events_str):
        return []

    # Split by common delimiters
    import re
    events = re.split(r'[,;/\n]', str(events_str))
    normalized = [_canonicalize_event_name(e.strip()) for e in events if e.strip()]
    return sorted(set(e for e in normalized if e))


def _canonicalize_event_name(raw_name: str) -> str:
    """Normalize free-form/column event labels into configured event names."""
    normalized = _normalize_label(raw_name)

    if 'jack' in normalized and 'jill' in normalized:
        return 'Jack & Jill Sawing'
    if 'double buck' in normalized:
        return 'Double Buck'
    if 'single buck' in normalized:
        return 'Single Buck'
    if 'stock saw' in normalized or 'power saw' in normalized:
        return 'Stock Saw'
    if 'obstacle' in normalized and 'pole' in normalized:
        return 'Obstacle Pole'
    if 'choker' in normalized:
        return "Chokerman's Race"
    if 'climb' in normalized:
        return 'Speed Climb'
    if 'birling' in normalized:
        return 'Birling'
    if 'kaber' in normalized or 'caber' in normalized:
        return 'Caber Toss'
    if 'axe throw' in normalized:
        return 'Axe Throw'
    if 'pulp toss' in normalized:
        return 'Pulp Toss'
    if 'peavey' in normalized or 'pv log roll' in normalized:
        return 'Peavey Log Roll'
    if ('horiz' in normalized or 'horizontal' in normalized) and ('h hit' in normalized or 'hard hit' in normalized):
        return 'Underhand Hard Hit'
    if ('horiz' in normalized or 'horizontal' in normalized) and ('sp chop' in normalized or 'speed' in normalized):
        return 'Underhand Speed'
    if ('vert' in normalized or 'vertical' in normalized) and ('h hit' in normalized or 'hard hit' in normalized):
        return 'Standing Block Hard Hit'
    if ('vert' in normalized or 'vertical' in normalized) and ('speed' in normalized):
        return 'Standing Block Speed'
    if 'springboard' in normalized or '1 board' in normalized:
        return '1-Board Springboard'

    return raw_name.strip()


def _validate_college_entry_constraints(team_ids: set) -> dict:
    """
    Validate college entry constraints for the given team IDs.

    Returns a dict mapping team_id -> list of structured error dicts.
    Teams with no errors are not included in the returned dict.
    Each error dict has at minimum: 'type' and 'message' keys, plus
    type-specific fields (competitor_id, competitor_name, event_name, etc.).
    """
    if not team_ids:
        return {}

    MIN_MEN = 2
    MIN_WOMEN = 2
    MAX_TEAM_MEMBERS = 8
    MAX_EVENTS_PER_COMPETITOR = 6  # Applies to CLOSED events only; OPEN events are uncapped
    MAX_PER_EVENT_PER_GENDER_PER_TEAM = 3
    MAX_PAIRS_PER_PARTNERED_EVENT = 3
    MAX_CHOPPING_EVENTS_PER_COMPETITOR = 2
    CLOSED_EVENT_NAMES = {e['name'] for e in config.COLLEGE_CLOSED_EVENTS}
    CHOPPING_EVENTS = {
        'Underhand Hard Hit',
        'Underhand Speed',
        'Standing Block Hard Hit',
        'Standing Block Speed'
    }
    errors_by_team = {}
    partner_gender_requirements = _partnered_event_gender_requirements()

    for team_id in team_ids:
        team = Team.query.get(team_id)
        if not team:
            continue

        team_errors = []
        per_event_gender_counts = {}
        active_members = team.members.filter_by(status='active').all()

        # --- Roster-level checks ---
        men_count = sum(1 for m in active_members if (m.gender or '').strip().upper() == 'M')
        women_count = sum(1 for m in active_members if (m.gender or '').strip().upper() == 'F')
        total = len(active_members)
        if men_count < MIN_MEN:
            team_errors.append({
                'type': 'roster_min_men',
                'message': f'Team has {men_count} men but requires at least {MIN_MEN}',
            })
        if women_count < MIN_WOMEN:
            team_errors.append({
                'type': 'roster_min_women',
                'message': f'Team has {women_count} women but requires at least {MIN_WOMEN}',
            })
        if total > MAX_TEAM_MEMBERS:
            team_errors.append({
                'type': 'roster_max_members',
                'message': f'Team has {total} members but maximum is {MAX_TEAM_MEMBERS}',
            })
        member_events_map = {}
        member_partners_map = {}
        member_by_norm_name = {}
        member_by_first_name = {}

        for member in active_members:
            member_name_norm = _normalize_person_name(member.name)
            events = [_canonicalize_event_name(e) for e in member.get_events_entered() if str(e).strip()]
            events = sorted(set(events))
            member_events_map[member_name_norm] = set(events)
            member_partners_map[member_name_norm] = member.get_partners() if isinstance(member.get_partners(), dict) else {}
            member_by_norm_name[member_name_norm] = member
            first_name_norm = _normalize_person_name(member.name.split()[0]) if member.name.strip() else ''
            if first_name_norm:
                if first_name_norm in member_by_first_name:
                    member_by_first_name[first_name_norm] = None  # ambiguous — multiple members share first name
                else:
                    member_by_first_name[first_name_norm] = member

        for member in active_members:
            events = [_canonicalize_event_name(e) for e in member.get_events_entered() if str(e).strip()]
            events = sorted(set(events))
            member_name_norm = _normalize_person_name(member.name)

            closed_events = [e for e in events if e in CLOSED_EVENT_NAMES]
            if len(closed_events) > MAX_EVENTS_PER_COMPETITOR:
                team_errors.append({
                    'type': 'too_many_events',
                    'message': f'{member.name} entered {len(closed_events)} closed events (max {MAX_EVENTS_PER_COMPETITOR})',
                    'competitor_id': member.id,
                    'competitor_name': member.name,
                    'count': len(closed_events),
                    'max': MAX_EVENTS_PER_COMPETITOR,
                    'events': closed_events,
                })

            chopping_count = sum(1 for e in events if e in CHOPPING_EVENTS)
            if chopping_count > MAX_CHOPPING_EVENTS_PER_COMPETITOR:
                chopping_events = [e for e in events if e in CHOPPING_EVENTS]
                team_errors.append({
                    'type': 'too_many_chopping',
                    'message': f'{member.name} entered {chopping_count} chopping events (max {MAX_CHOPPING_EVENTS_PER_COMPETITOR})',
                    'competitor_id': member.id,
                    'competitor_name': member.name,
                    'count': chopping_count,
                    'max': MAX_CHOPPING_EVENTS_PER_COMPETITOR,
                    'events': chopping_events,
                })

            for event_name in events:
                if event_name in partner_gender_requirements:
                    continue
                key = (event_name, member.gender)
                per_event_gender_counts[key] = per_event_gender_counts.get(key, 0) + 1

        for (event_name, gender), count in per_event_gender_counts.items():
            if count > MAX_PER_EVENT_PER_GENDER_PER_TEAM:
                # Collect competitor IDs in this over-limit group for fix forms
                over_competitors = [
                    {'id': m.id, 'name': m.name}
                    for m in active_members
                    if m.gender == gender and event_name in member_events_map.get(_normalize_person_name(m.name), set())
                    and event_name not in partner_gender_requirements
                ]
                team_errors.append({
                    'type': 'too_many_per_event',
                    'message': f'{count} {gender} competitors entered "{event_name}" (max {MAX_PER_EVENT_PER_GENDER_PER_TEAM})',
                    'event_name': event_name,
                    'gender': gender,
                    'count': count,
                    'max': MAX_PER_EVENT_PER_GENDER_PER_TEAM,
                    'competitors': over_competitors,
                })

        # Partnered events are limited by number of pairs, not number of people.
        pair_counts = {}
        # Track partner errors to avoid duplicate entries per pair
        partner_error_pairs = set()

        for member in active_members:
            member_name = member.name.strip()
            member_name_norm = _normalize_person_name(member_name)
            member_gender = (member.gender or '').strip().upper()
            partners = member_partners_map.get(member_name_norm, {})
            events = member_events_map.get(member_name_norm, set())

            for event_name in events:
                if event_name not in partner_gender_requirements:
                    continue

                partner_name = str(partners.get(event_name, '')).strip()
                if not partner_name:
                    team_errors.append({
                        'type': 'missing_partner',
                        'message': f'{member_name} entered "{event_name}" without a partner name',
                        'competitor_id': member.id,
                        'competitor_name': member_name,
                        'event_name': event_name,
                    })
                    continue

                partner_name_norm = _normalize_person_name(partner_name)
                partner_member = member_by_norm_name.get(partner_name_norm)
                if not partner_member:
                    # Fallback: try matching by first name only (common in spreadsheets)
                    first_match = member_by_first_name.get(partner_name_norm)
                    if first_match is not None:
                        partner_member = first_match
                        partner_name_norm = _normalize_person_name(partner_member.name)
                if not partner_member:
                    # Fallback: fuzzy match — find closest name on team (handles typos like McKinley/Mickinley)
                    partner_member = _fuzzy_match_member(partner_name_norm, member_by_norm_name, member_by_first_name)
                    if partner_member:
                        partner_name_norm = _normalize_person_name(partner_member.name)
                if not partner_member:
                    team_errors.append({
                        'type': 'partner_not_on_team',
                        'message': f'{member_name} lists "{partner_name}" for "{event_name}" but that partner is not on this team',
                        'competitor_id': member.id,
                        'competitor_name': member_name,
                        'event_name': event_name,
                        'partner_name': partner_name,
                    })
                    continue

                partner_events = member_events_map.get(partner_name_norm, set())
                if event_name not in partner_events:
                    team_errors.append({
                        'type': 'partner_not_in_event',
                        'message': f'{member_name} lists {partner_member.name} for "{event_name}" but {partner_member.name} is not entered in that event',
                        'competitor_id': member.id,
                        'competitor_name': member_name,
                        'event_name': event_name,
                        'partner_name': partner_member.name,
                        'partner_id': partner_member.id,
                    })
                    continue

                partner_partners = member_partners_map.get(partner_name_norm, {})
                reciprocal_name = str(partner_partners.get(event_name, '')).strip()
                reciprocal_norm = _normalize_person_name(reciprocal_name)
                # Resolve reciprocal name through same fallback chain: exact → first-name → fuzzy
                reciprocal_resolved = member_by_norm_name.get(reciprocal_norm)
                if not reciprocal_resolved:
                    fm = member_by_first_name.get(reciprocal_norm)
                    if fm is not None:
                        reciprocal_resolved = fm
                if not reciprocal_resolved:
                    reciprocal_resolved = _fuzzy_match_member(reciprocal_norm, member_by_norm_name, member_by_first_name)
                reciprocal_matches = (
                    reciprocal_resolved is not None
                    and _normalize_person_name(reciprocal_resolved.name) == member_name_norm
                )
                if not reciprocal_matches:
                    # Deduplicate: only report once per pair per event
                    pair_key = (event_name, tuple(sorted([member_name_norm, partner_name_norm])))
                    if pair_key not in partner_error_pairs:
                        partner_error_pairs.add(pair_key)
                        team_errors.append({
                            'type': 'partner_mismatch',
                            'message': f'Partner mismatch in "{event_name}" between {member_name} and {partner_member.name}',
                            'competitor_id': member.id,
                            'competitor_name': member_name,
                            'event_name': event_name,
                            'partner_name': partner_member.name,
                            'partner_id': partner_member.id,
                        })
                    continue

                requirement = partner_gender_requirements.get(event_name, 'any')
                bucket = 'ALL' if requirement in ['mixed', 'any'] else member_gender
                pair_id = tuple(sorted([member_name_norm, partner_name_norm]))
                key = (event_name, bucket)
                pair_counts.setdefault(key, set()).add(pair_id)

        for (event_name, bucket), pairs in pair_counts.items():
            pair_count = len(pairs)
            if pair_count > MAX_PAIRS_PER_PARTNERED_EVENT:
                # Collect the pair competitor IDs for fix forms
                pair_competitors = []
                for pair_id in pairs:
                    for norm_name in pair_id:
                        m = member_by_norm_name.get(norm_name)
                        if m:
                            pair_competitors.append({'id': m.id, 'name': m.name})
                pair_msg = (
                    f'{pair_count} pairs entered "{event_name}" (max {MAX_PAIRS_PER_PARTNERED_EVENT})'
                    if bucket == 'ALL'
                    else f'{pair_count} {bucket} pairs entered "{event_name}" (max {MAX_PAIRS_PER_PARTNERED_EVENT})'
                )
                team_errors.append({
                    'type': 'too_many_pairs',
                    'message': pair_msg,
                    'event_name': event_name,
                    'gender': None if bucket == 'ALL' else bucket,
                    'count': pair_count,
                    'max': MAX_PAIRS_PER_PARTNERED_EVENT,
                    'competitors': pair_competitors,
                })

        if team_errors:
            errors_by_team[team_id] = team_errors

    return errors_by_team


def _partnered_event_gender_requirements() -> dict:
    """Return dict of partnered college event name -> gender requirement."""
    partnered = {}
    for event in config.COLLEGE_OPEN_EVENTS + config.COLLEGE_CLOSED_EVENTS:
        if event.get('is_partnered'):
            name = _canonicalize_event_name(event['name'])
            partnered[name] = event.get('partner_gender', 'any')
    return partnered


def _extract_partner_entries(row: pd.Series, columns: list) -> dict:
    """Extract partnered event -> partner name mappings from row columns."""
    pairings = {}
    partnered_events = set(_partnered_event_gender_requirements().keys())

    for idx, column_name in enumerate(columns):
        event_name = _canonicalize_event_name(str(column_name).strip())
        if event_name not in partnered_events:
            continue

        # Event should be selected (or partner provided) to count as entered.
        selected = False
        event_value = row.get(column_name)
        if not pd.isna(event_value):
            marker = str(event_value).strip().lower()
            selected = marker in {'x', 'y', 'yes', '1', 'true', 't'}

        partner_name = ''
        if idx + 1 < len(columns):
            next_col = str(columns[idx + 1]).strip()
            if _normalize_label(next_col).startswith('partner'):
                raw_partner = row.get(columns[idx + 1])
                if not pd.isna(raw_partner):
                    partner_name = str(raw_partner).strip()

        if selected or partner_name:
            if partner_name:
                pairings[event_name] = partner_name

    return pairings


def _normalize_person_name(name: str) -> str:
    """Normalize person names for robust matching."""
    import re
    text = '' if name is None else str(name).strip().lower()
    text = re.sub(r'[^a-z0-9]+', '', text)
    return text


def _fuzzy_match_member(query_norm, member_by_norm_name, member_by_first_name):
    """Find a team member by approximate name match (edit distance ≤ 2).

    Handles common typos like McKinley/Mickinley. Returns None if no
    unique close match found.
    """
    if not query_norm:
        return None
    best, best_dist, ambiguous = None, 3, False
    # Check against full names and first names
    for norm_name, member in member_by_norm_name.items():
        d = _edit_distance(query_norm, norm_name)
        if d < best_dist:
            best, best_dist, ambiguous = member, d, False
        elif d == best_dist and member != best:
            ambiguous = True
    for first_norm, member in member_by_first_name.items():
        if member is None:
            continue
        d = _edit_distance(query_norm, first_norm)
        if d < best_dist:
            best, best_dist, ambiguous = member, d, False
        elif d == best_dist and member != best:
            ambiguous = True
    if ambiguous or best is None:
        return None
    return best


def _edit_distance(a: str, b: str) -> int:
    """Levenshtein edit distance (bounded — returns 3 early for speed)."""
    if abs(len(a) - len(b)) >= 3:
        return 3
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a):
        curr = [i + 1] + [0] * len(b)
        for j, cb in enumerate(b):
            curr[j + 1] = prev[j] if ca == cb else 1 + min(prev[j], prev[j + 1], curr[j])
        if min(curr) >= 3:
            return 3
        prev = curr
    return prev[len(b)]


def _process_partners(competitor: CollegeCompetitor, partners_str):
    """Process partner information from entry form."""
    if pd.isna(partners_str):
        return

    # Partners might be formatted as "Event: Partner Name" or just "Partner Name"
    import re
    parts = re.split(r'[,;/\n]', str(partners_str))

    for part in parts:
        part = part.strip()
        if ':' in part:
            event, partner = part.split(':', 1)
            competitor.set_partner(event.strip(), partner.strip())
        elif part:
            # Generic partner - might need manual assignment later
            pass


def export_results_to_excel(tournament: Tournament, filepath: str):
    """Export all tournament results to an Excel file."""
    with pd.ExcelWriter(filepath, engine='openpyxl') as writer:
        # College team standings
        teams = tournament.get_team_standings()
        team_data = [{
            'Rank': i + 1,
            'Team': t.team_code,
            'School': t.school_name,
            'Members': t.member_count,
            'Points': t.total_points
        } for i, t in enumerate(teams)]

        if team_data:
            pd.DataFrame(team_data).to_excel(writer, sheet_name='Team Standings', index=False)

        # Individual standings
        bull = tournament.get_bull_of_woods(20)
        belle = tournament.get_belle_of_woods(20)

        bull_data = [{
            'Rank': i + 1,
            'Name': c.name,
            'Team': c.team.team_code if c.team else 'N/A',
            'Points': c.individual_points
        } for i, c in enumerate(bull)]

        belle_data = [{
            'Rank': i + 1,
            'Name': c.name,
            'Team': c.team.team_code if c.team else 'N/A',
            'Points': c.individual_points
        } for i, c in enumerate(belle)]

        if bull_data:
            pd.DataFrame(bull_data).to_excel(writer, sheet_name='Bull of Woods', index=False)
        if belle_data:
            pd.DataFrame(belle_data).to_excel(writer, sheet_name='Belle of Woods', index=False)

        # Event results
        for event in tournament.events.all():
            results = event.get_results_sorted()
            if not results:
                continue

            result_data = [{
                'Position': r.final_position,
                'Name': r.competitor_name,
                'Result': r.result_value,
                'Points': r.points_awarded if event.event_type == 'college' else None,
                'Payout': r.payout_amount if event.event_type == 'pro' else None
            } for r in results]

            sheet_name = event.display_name[:31]  # Excel sheet name limit
            pd.DataFrame(result_data).to_excel(writer, sheet_name=sheet_name, index=False)
