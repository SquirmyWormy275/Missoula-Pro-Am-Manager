"""
Centralized text labels and lightweight localization for the Missoula Pro Am Tournament Manager.

English is the fallback language for any untranslated key.
"""
from __future__ import annotations

from copy import deepcopy
from collections.abc import Mapping
import json
from pathlib import Path
import re
from flask import has_request_context, session

DEFAULT_LANGUAGE = 'en'
SUPPORTED_LANGUAGES = {
    'en': 'English',
    'arp': 'Northern Arapaho',
}

ENGLISH = {
    'NAV': {
        'brand': 'Missoula Pro Am',
        'home': 'Home',
        'dashboard': 'Dashboard',
        'college': 'College',
        'pro': 'Pro',
        'events': 'Events',
    },
    'COMPETITION': {
        'app_title': 'Missoula Pro Am Tournament Manager',
        'app_footer': 'Missoula Pro Am Tournament Manager',
        'college_title': 'College Competition',
        'college_day': 'Friday',
        'pro_title': 'Pro Competition',
        'pro_day': 'Saturday',
        'bull_of_woods': 'Bull of the Woods',
        'belle_of_woods': 'Belle of the Woods',
        'team_standings': 'Team Standings',
    },
    'FLASH': {
        'tournament_created': 'Tournament "{name} {year}" created successfully!',
        'college_active': 'College competition is now active!',
        'pro_active': 'Professional competition is now active!',
        'invalid_comp_type': 'Invalid competition type.',
        'no_file': 'No file selected.',
        'import_success': 'Successfully imported {teams} team(s) with {competitors} competitor(s).',
        'import_error': 'Error processing file: {error}',
        'invalid_file_type': 'Invalid file type. Please upload an Excel file (.xlsx or .xls).',
        'competitor_added': 'Competitor "{name}" added successfully!',
        'competitor_scratched': 'Competitor "{name}" has been scratched.',
        'events_configured': 'Events configured successfully!',
        'heats_generated': 'Generated {num_heats} heat(s) for {event_name}.',
        'heats_error': 'Error generating heats: {error}',
        'flights_built': 'Built {num_flights} flight(s) for pro competition.',
        'flights_error': 'Error building flights: {error}',
        'heat_saved': 'Heat results saved successfully!',
        'event_finalized': '{event_name} has been finalized.',
        'pro_only_payouts': 'Payouts can only be configured for pro events.',
        'payouts_saved': 'Payouts configured successfully!',
        'language_changed': 'Language changed to {language}.',
        'invalid_language': 'Invalid language selection.',
        'arapaho_restricted': 'Arapaho mode is available only in Judge/Admin mode.',
    },
    'UI': {
        'language': 'Language',
        'dashboard': 'Dashboard',
        'active_tournament': 'Active Tournament',
        'status': 'Status',
        'setup': 'Setup',
        'college_competition_active': 'College Competition Active',
        'pro_competition_active': 'Pro Competition Active',
        'college_active': 'College Active',
        'pro_active': 'Pro Active',
        'completed': 'Completed',
        'go_to_tournament': 'Go to Tournament',
        'tournaments': 'Tournaments',
        'new_tournament': 'New Tournament',
        'tournament': 'Tournament',
        'year': 'Year',
        'teams': 'Teams',
        'college': 'College',
        'pro': 'Pro',
        'actions': 'Actions',
        'view': 'View',
        'no_tournaments': 'No tournaments created yet.',
        'create_first_tournament': 'Create Your First Tournament',
        'create_new_tournament': 'Create New Tournament',
        'tournament_name': 'Tournament Name',
        'create_tournament': 'Create Tournament',
        'cancel': 'Cancel',
        'college_teams': 'College Teams',
        'college_competitors': 'College Competitors',
        'pro_competitors': 'Pro Competitors',
        'events_completed': 'Events Completed',
        'quick_actions': 'Quick Actions',
        'configure_events': 'Configure Events',
        'view_events': 'View Events',
        'view_all_events': 'View All Events',
        'view_all_results': 'View All Results',
        'import_teams': 'Import Teams',
        'go_to_college_dashboard': 'Go to College Dashboard',
        'register_competitors': 'Register Competitors',
        'go_to_pro_dashboard': 'Go to Pro Dashboard',
        'language_english': 'English',
        'language_arapaho': 'Northern Arapaho',
    },
}

# Verified from arapaho-dictionary1.pdf (dictionary entries for HOME, LANGUAGE,
# COMPETITION, COLLEGE, FRIDAY, SATURDAY).
ARAPAHO_OVERRIDES = {
    'NAV': {
        'home': "beyeihi'",
        'college': "tesco'ouutou3eino'oowu'",
    },
    'COMPETITION': {
        'college_title': "tesco'ouutou3eino'oowu' hoonoyoo3etiit",
        'pro_title': "Pro hoonoyoo3etiit",
        'college_day': "neh'eheiniisi'",
        'pro_day': "hooxobeti'",
    },
    'FLASH': {
        'language_changed': "Heenetiit nih'ookuuni3i' {language}.",
    },
    'UI': {
        'language': "Hinono'eitiit, Bee3osohoot",
        'home': "beyeihi'",
        'college': "tesco'ouutou3eino'oowu'",
        'year': 'cec',
        'college_competitors': "tesco'ouutou3eino'oowu' hineniteeno'",
        'pro_competitors': "Pro hineniteeno'",
        'language_english': 'English',
        'language_arapaho': 'Northern Arapaho',
    },
}

# Dictionary-backed phrase substitutions used for full-page translation
# in Arapaho mode. Unknown terms intentionally remain English for safety.
ARAPAHO_VERIFIED_PHRASES = {
    'Language': "Hinono'eitiit, Bee3osohoot",
    'Home': "beyeihi'",
    'Homepage': "niiheyoo niihenehiitono",
    'Arapaho': "hinono'ei'",
    'Arapaho Tribe': "hinono'eiteen",
    'College Competition': "tesco'ouutou3eino'oowu' hoonoyoo3etiit",
    'Competition': 'hoonoyoo3etiit',
    'Friday': "neh'eheiniisi'",
    'Saturday': "hooxobeti'",
    'School': "neyei3eino'oowu'",
    'Create': "ceebii'ootiit",  # dictionary entry: CREATE
    'Save': "hee'neetouhunoo",  # dictionary entry: SAVE
    'Search': "nootiiho'",  # dictionary entry: SEARCH
    'Error': 'nontoot',
    'Day': "hiisi'",
    'Week': "niiseti'",
    'Month': 'biikousiis',
    'Year': 'cec',
    'Person': "Hinono'eino",
    'People': "hineniteeno'",
    'Country': "Hinono'eino' Biito'owu'",
    'Man': 'hinen',
    'Woman': 'hisei',
    'Pay': 'honoontoone3en',
    'Score': "3eneiikuu3ei'it",
    'Who': "henee'eeno'",
    'What': 'heeyou',
    'What will you do?': 'heetoustoo',
    'Where': 'tootiinoo',
    'Where at?': 'tootiinoo',
    'Store': "hootooneeno'oowu'",
    'Town': "woteeniihi'",
    'Movie': "ce'iskuu3oo",
    'Spring': "beenii'owuuni'",
    'Easter': "ce'koheiniisi'",
    'April': "benii'owuusiis",
    'Why?': 'nohtou',
    'Doctor': 'notoniheihii',
    'Teacher': 'neyei3eibeihii',
    'Boss': 'niinookoonit',
}

_GLOSSARY_FILE = Path(__file__).with_name('arapaho_glossary.json')


def _merge_nested(base: dict, overrides: dict) -> dict:
    merged = deepcopy(base)
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge_nested(merged[key], value)
        else:
            merged[key] = value
    return merged


TRANSLATIONS = {
    'en': ENGLISH,
    'arp': _merge_nested(ENGLISH, ARAPAHO_OVERRIDES),
}


def get_language() -> str:
    if not has_request_context():
        return DEFAULT_LANGUAGE
    lang = session.get('lang', DEFAULT_LANGUAGE)
    return lang if lang in TRANSLATIONS else DEFAULT_LANGUAGE


def set_language(lang: str) -> bool:
    if not has_request_context() or lang not in TRANSLATIONS:
        return False
    session['lang'] = lang
    return True


def get_language_name(lang: str | None = None) -> str:
    lang_code = lang or get_language()
    return SUPPORTED_LANGUAGES.get(lang_code, SUPPORTED_LANGUAGES[DEFAULT_LANGUAGE])


def section(name: str, lang: str | None = None) -> dict:
    lang_code = lang or get_language()
    source = TRANSLATIONS.get(lang_code, TRANSLATIONS[DEFAULT_LANGUAGE])
    fallback = TRANSLATIONS[DEFAULT_LANGUAGE]
    return source.get(name, fallback.get(name, {}))


def tr(section_name: str, key: str, **kwargs) -> str:
    text_value = section(section_name).get(key)
    if text_value is None:
        text_value = TRANSLATIONS[DEFAULT_LANGUAGE].get(section_name, {}).get(key, key)
    return text_value.format(**kwargs) if kwargs else text_value


def ui(key: str, **kwargs) -> str:
    return tr('UI', key, **kwargs)


def _replace_phrase_case_insensitive(text_value: str, source: str, target: str) -> str:
    pattern = re.compile(re.escape(source), re.IGNORECASE)
    return pattern.sub(target, text_value)


def _load_custom_glossary() -> dict[str, str]:
    """
    Load user-approved glossary overrides.
    File format: {"English phrase": "Northern Arapaho phrase"}
    """
    if not _GLOSSARY_FILE.exists():
        return {}
    try:
        raw = json.loads(_GLOSSARY_FILE.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    clean = {}
    for key, value in raw.items():
        if isinstance(key, str) and isinstance(value, str) and key.strip() and value.strip():
            clean[key.strip()] = value.strip()
    return clean


def _phrase_map(lang_code: str) -> dict[str, str]:
    if lang_code != 'arp':
        return {}
    # User glossary wins over built-ins for community-approved phrasing.
    merged = dict(ARAPAHO_VERIFIED_PHRASES)
    merged.update(_load_custom_glossary())
    return merged


def free_text(text_value: str, lang: str | None = None) -> str:
    """Translate free-form UI text with strict phrase-level substitutions only."""
    lang_code = lang or get_language()
    if lang_code != 'arp' or not text_value:
        return text_value

    translated = text_value
    phrase_map = _phrase_map(lang_code)

    for src, dst in sorted(phrase_map.items(), key=lambda item: len(item[0]), reverse=True):
        translated = _replace_phrase_case_insensitive(translated, src, dst)

    return translated


def translate_html(html: str, lang: str | None = None) -> str:
    """
    Translate all text nodes in rendered HTML.
    Splits on tags and only transforms plain text chunks.
    """
    lang_code = lang or get_language()
    if lang_code != 'arp' or not html:
        return html

    parts = re.split(r'(<[^>]+>)', html)
    in_style = False
    in_script = False

    for idx, chunk in enumerate(parts):
        if not chunk:
            continue

        if chunk.startswith('<'):
            tag = chunk.lower()
            if tag.startswith('<style'):
                in_style = True
            elif tag.startswith('</style'):
                in_style = False
            elif tag.startswith('<script'):
                in_script = True
            elif tag.startswith('</script'):
                in_script = False
            continue

        if in_style or in_script or chunk.isspace():
            continue

        parts[idx] = free_text(chunk, lang=lang_code)
    return ''.join(parts)


class _LocalizedSection(Mapping):
    def __init__(self, section_name: str):
        self.section_name = section_name

    def __getitem__(self, key: str) -> str:
        return tr(self.section_name, key)

    def __iter__(self):
        return iter(section(self.section_name))

    def __len__(self):
        return len(section(self.section_name))


# Backward-compatible access for existing imports in route files.
FLASH = _LocalizedSection('FLASH')
