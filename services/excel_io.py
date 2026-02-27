"""
Excel import/export service for college and pro registration.
Maintains compatibility with existing college entry form format.
"""
import pandas as pd
import config
from database import db
from models import Tournament, Team, CollegeCompetitor, ProCompetitor


def process_college_entry_form(filepath: str, tournament: Tournament) -> dict:
    """
    Process a college entry form Excel file and import teams/competitors.

    Expected format (based on existing college entry form):
    - Sheet contains team and competitor information
    - Columns should include: Name, Gender, Events, Partners

    Args:
        filepath: Path to the Excel file
        tournament: Tournament to add teams/competitors to

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
    default_school_name = _extract_school_name(raw_df, header_row)

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
        if team_col:
            team_code = str(team_identifier)
            school_name = valid_team_df[school_col].iloc[0] if school_col else (default_school_name or team_code)
        else:
            team_code = str(team_identifier)
            school_name = default_school_name or team_code
            if school_col and not _looks_like_team_code(team_code):
                school_name = team_code
                team_code = _generate_team_code(school_name, tournament)

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
    _validate_college_entry_constraints(touched_team_ids)
    db.session.commit()

    return {
        'teams': teams_created,
        'competitors': competitors_created
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
        if any(k in normalized for k in ['horiz', 'vert', 'pole', 'climb', 'choker', 'saw', 'birling', 'kaber', 'chop', 'buck', 'toss', 'hit', 'speed']):
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
    if not categories:
        return

    for category in categories:
        group_token = f'group:team:{team.id}:{category}'
        event_key = f'category:{category}'
        for competitor in team.members.filter_by(status='active').all():
            competitor.set_gear_sharing(event_key, group_token)


def _infer_gear_categories(note_text: str) -> list:
    """Infer gear categories from free-text notes."""
    normalized = _normalize_label(note_text)
    categories = []
    if any(token in normalized for token in ['crosscut', 'single buck', 'double buck', 'buck', 'saw']):
        categories.append('crosscut')
    if any(token in normalized for token in ['stock saw', 'powersaw', 'power saw', 'hot saw']):
        categories.append('chainsaw')
    return categories


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
        'oregon state university': 'OSU',
        'university of washington': 'UW',
        'humboldt state': 'HSU',
        'cal poly': 'CP',
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


def _validate_college_entry_constraints(team_ids: set):
    """Validate college entry constraints for touched teams before commit."""
    if not team_ids:
        return

    MAX_EVENTS_PER_COMPETITOR = 6
    MAX_PER_EVENT_PER_GENDER_PER_TEAM = 3
    MAX_PAIRS_PER_PARTNERED_EVENT = 3
    MAX_CHOPPING_EVENTS_PER_COMPETITOR = 2
    CHOPPING_EVENTS = {
        'Underhand Hard Hit',
        'Underhand Speed',
        'Standing Block Hard Hit',
        'Standing Block Speed'
    }
    errors = []
    partner_gender_requirements = _partnered_event_gender_requirements()

    for team_id in team_ids:
        team = Team.query.get(team_id)
        if not team:
            continue

        per_event_gender_counts = {}
        active_members = team.members.filter_by(status='active').all()
        member_events_map = {}
        member_partners_map = {}
        member_by_norm_name = {}

        for member in active_members:
            member_name_norm = _normalize_person_name(member.name)
            events = [_canonicalize_event_name(e) for e in member.get_events_entered() if str(e).strip()]
            events = sorted(set(events))
            member_events_map[member_name_norm] = set(events)
            member_partners_map[member_name_norm] = member.get_partners() if isinstance(member.get_partners(), dict) else {}
            member_by_norm_name[member_name_norm] = member

        for member in active_members:
            events = [_canonicalize_event_name(e) for e in member.get_events_entered() if str(e).strip()]
            events = sorted(set(events))
            member_name_norm = _normalize_person_name(member.name)

            if len(events) > MAX_EVENTS_PER_COMPETITOR:
                errors.append(
                    f'{team.team_code}: {member.name} entered {len(events)} events (max {MAX_EVENTS_PER_COMPETITOR})'
                )

            chopping_count = sum(1 for e in events if e in CHOPPING_EVENTS)
            if chopping_count > MAX_CHOPPING_EVENTS_PER_COMPETITOR:
                errors.append(
                    f'{team.team_code}: {member.name} entered {chopping_count} chopping events (max {MAX_CHOPPING_EVENTS_PER_COMPETITOR})'
                )

            for event_name in events:
                if event_name in partner_gender_requirements:
                    continue
                key = (event_name, member.gender)
                per_event_gender_counts[key] = per_event_gender_counts.get(key, 0) + 1

        for (event_name, gender), count in per_event_gender_counts.items():
            if count > MAX_PER_EVENT_PER_GENDER_PER_TEAM:
                errors.append(
                    f'{team.team_code}: {count} {gender} competitors entered "{event_name}" (max {MAX_PER_EVENT_PER_GENDER_PER_TEAM})'
                )

        # Partnered events are limited by number of pairs, not number of people.
        pair_counts = {}
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
                    errors.append(
                        f'{team.team_code}: {member_name} entered "{event_name}" without a partner name'
                    )
                    continue

                partner_name_norm = _normalize_person_name(partner_name)
                partner_member = member_by_norm_name.get(partner_name_norm)
                if not partner_member:
                    errors.append(
                        f'{team.team_code}: {member_name} lists "{partner_name}" for "{event_name}" but that partner is not on this team'
                    )
                    continue

                partner_events = member_events_map.get(partner_name_norm, set())
                if event_name not in partner_events:
                    errors.append(
                        f'{team.team_code}: {member_name} lists {partner_member.name} for "{event_name}" but {partner_member.name} is not entered in that event'
                    )
                    continue

                partner_partners = member_partners_map.get(partner_name_norm, {})
                reciprocal_name = str(partner_partners.get(event_name, '')).strip()
                if _normalize_person_name(reciprocal_name) != member_name_norm:
                    errors.append(
                        f'{team.team_code}: partner mismatch in "{event_name}" between {member_name} and {partner_member.name}'
                    )
                    continue

                requirement = partner_gender_requirements.get(event_name, 'any')
                bucket = 'ALL' if requirement in ['mixed', 'any'] else member_gender
                pair_id = tuple(sorted([member_name_norm, partner_name_norm]))
                key = (event_name, bucket)
                pair_counts.setdefault(key, set()).add(pair_id)

        for (event_name, bucket), pairs in pair_counts.items():
            pair_count = len(pairs)
            if pair_count > MAX_PAIRS_PER_PARTNERED_EVENT:
                if bucket == 'ALL':
                    errors.append(
                        f'{team.team_code}: {pair_count} pairs entered "{event_name}" (max {MAX_PAIRS_PER_PARTNERED_EVENT})'
                    )
                else:
                    errors.append(
                        f'{team.team_code}: {pair_count} {bucket} pairs entered "{event_name}" (max {MAX_PAIRS_PER_PARTNERED_EVENT})'
                    )

    if errors:
        preview = '; '.join(errors[:8])
        remaining = len(errors) - 8
        if remaining > 0:
            preview += f'; ...and {remaining} more'
        raise ValueError(f'Entry form limit violations: {preview}')


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
