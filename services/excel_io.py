"""
Excel import/export service for college and pro registration.
Maintains compatibility with existing college entry form format.
"""
import pandas as pd
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

    # Get column mappings (flexible to handle variations)
    school_col = _find_column(df, ['school', 'university', 'college', 'institution'])
    team_col = _find_column(df, ['team', 'team_code', 'team_id', 'team name'])
    name_col = _find_column(df, ['name', 'first and last name', 'competitor', 'athlete', 'full name', 'competitor name'])
    gender_col = _find_column(df, ['gender', 'sex', 'm/f'])
    events_col = _find_column(df, ['events', 'event', 'entered'])
    event_marker_cols = _find_event_marker_columns(df, excluded_cols=[school_col, team_col, name_col, gender_col, events_col])
    default_gender = _infer_default_gender(df, gender_col)

    if not name_col:
        raise ValueError("Could not find name column in the entry form")

    # Group by team if team column exists
    if team_col:
        grouped = df.groupby(team_col)
    elif school_col:
        grouped = df.groupby(school_col)
    else:
        # Treat entire file as one team
        grouped = [(df.iloc[0].get(school_col, 'Unknown'), df)]

    for team_identifier, team_df in grouped:
        # Skip empty groups
        if len(team_df) == 0:
            continue

        # Determine school and team code
        if team_col:
            team_code = str(team_identifier)
            school_name = team_df[school_col].iloc[0] if school_col else (default_school_name or team_code)
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

        # Add competitors to team
        for _, row in team_df.iterrows():
            name = row.get(name_col)
            if pd.isna(name) or not str(name).strip():
                continue

            gender = _parse_gender(row.get(gender_col, default_gender))

            # Check if competitor already exists
            existing = CollegeCompetitor.query.filter_by(
                tournament_id=tournament.id,
                team_id=team.id,
                name=str(name).strip()
            ).first()

            if not existing:
                competitor = CollegeCompetitor(
                    tournament_id=tournament.id,
                    team_id=team.id,
                    name=str(name).strip(),
                    gender=gender
                )

                # Process events if column exists
                if events_col and not pd.isna(row.get(events_col)):
                    events = _parse_events(row.get(events_col))
                    competitor.set_events_entered(events)
                elif event_marker_cols:
                    events = _parse_event_markers(row, event_marker_cols)
                    competitor.set_events_entered(events)

                # Process partners if column exists
                partners_col = _find_column(df, ['partner', 'partners', 'partner name'])
                if partners_col and not pd.isna(row.get(partners_col)):
                    _process_partners(competitor, row.get(partners_col))

                db.session.add(competitor)
                competitors_created += 1

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
            selected.append(str(col).strip())
    return selected


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


def _parse_gender(value) -> str:
    """Parse gender value to 'M' or 'F'."""
    if pd.isna(value):
        return 'M'

    value = str(value).strip().upper()
    if value in ['F', 'FEMALE', 'W', 'WOMAN', 'WOMEN']:
        return 'F'
    return 'M'


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
    return [e.strip() for e in events if e.strip()]


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
