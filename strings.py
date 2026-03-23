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

# Public languages are shown to all users in the language switcher.
PUBLIC_LANGUAGES = {
    'en': 'English',
    'ru': 'Русский',
}

# Restricted languages are shown only to judges and admins.
RESTRICTED_LANGUAGES = {
    'arp': 'Northern Arapaho',
}

SUPPORTED_LANGUAGES = {**PUBLIC_LANGUAGES, **RESTRICTED_LANGUAGES}

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
        'language_russian': 'Russian',
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

# ---------------------------------------------------------------------------
# Russian
# ---------------------------------------------------------------------------

RUSSIAN_OVERRIDES = {
    'NAV': {
        'home': 'Главная',
        'dashboard': 'Панель управления',
        'college': 'Колледж',
        'pro': 'Про',
        'events': 'Соревнования',
    },
    'COMPETITION': {
        'app_title': 'Менеджер турнира Missoula Pro Am',
        'app_footer': 'Менеджер турнира Missoula Pro Am',
        'college_title': 'Соревнования колледжей',
        'college_day': 'Пятница',
        'pro_title': 'Профессиональные соревнования',
        'pro_day': 'Суббота',
        'bull_of_woods': 'Король леса',
        'belle_of_woods': 'Королева леса',
        'team_standings': 'Командный зачёт',
    },
    'FLASH': {
        'tournament_created': 'Турнир "{name} {year}" успешно создан!',
        'college_active': 'Соревнования колледжей начались!',
        'pro_active': 'Профессиональные соревнования начались!',
        'invalid_comp_type': 'Неверный тип соревнований.',
        'no_file': 'Файл не выбран.',
        'import_success': 'Успешно импортировано: {teams} команд(а), {competitors} участник(ов).',
        'import_error': 'Ошибка обработки файла: {error}',
        'invalid_file_type': 'Неверный тип файла. Загрузите файл Excel (.xlsx или .xls).',
        'competitor_added': 'Участник "{name}" успешно добавлен!',
        'competitor_scratched': 'Участник "{name}" снят с соревнований.',
        'events_configured': 'Соревнования успешно настроены!',
        'heats_generated': 'Создано заездов: {num_heats} для {event_name}.',
        'heats_error': 'Ошибка создания заездов: {error}',
        'flights_built': 'Создано этапов: {num_flights} для профессиональных соревнований.',
        'flights_error': 'Ошибка создания этапов: {error}',
        'heat_saved': 'Результаты заезда успешно сохранены!',
        'event_finalized': '{event_name} завершено.',
        'pro_only_payouts': 'Выплаты настраиваются только для профессиональных соревнований.',
        'payouts_saved': 'Выплаты успешно настроены!',
        'language_changed': 'Язык изменён на {language}.',
        'invalid_language': 'Неверный выбор языка.',
        'arapaho_restricted': 'Режим арапахо доступен только для судей и администраторов.',
    },
    'UI': {
        'language': 'Язык',
        'dashboard': 'Панель управления',
        'active_tournament': 'Активный турнир',
        'status': 'Статус',
        'setup': 'Настройка',
        'college_competition_active': 'Соревнования колледжей активны',
        'pro_competition_active': 'Профессиональные соревнования активны',
        'college_active': 'Колледж активен',
        'pro_active': 'Про активен',
        'completed': 'Завершено',
        'go_to_tournament': 'Перейти к турниру',
        'tournaments': 'Турниры',
        'new_tournament': 'Новый турнир',
        'tournament': 'Турнир',
        'year': 'Год',
        'teams': 'Команды',
        'college': 'Колледж',
        'pro': 'Про',
        'actions': 'Действия',
        'view': 'Просмотр',
        'no_tournaments': 'Турниры ещё не созданы.',
        'create_first_tournament': 'Создайте первый турнир',
        'create_new_tournament': 'Создать новый турнир',
        'tournament_name': 'Название турнира',
        'create_tournament': 'Создать турнир',
        'cancel': 'Отмена',
        'college_teams': 'Команды колледжей',
        'college_competitors': 'Участники колледжей',
        'pro_competitors': 'Профессиональные участники',
        'events_completed': 'Завершённые соревнования',
        'quick_actions': 'Быстрые действия',
        'configure_events': 'Настроить соревнования',
        'view_events': 'Просмотр соревнований',
        'view_all_events': 'Все соревнования',
        'view_all_results': 'Все результаты',
        'import_teams': 'Импорт команд',
        'go_to_college_dashboard': 'Панель колледжей',
        'register_competitors': 'Зарегистрировать участников',
        'go_to_pro_dashboard': 'Профессиональная панель',
        'language_english': 'Английский',
        'language_arapaho': 'Северный Арапахо',
        'language_russian': 'Русский',
    },
}

# Common English phrases found throughout templates, substituted during full-page
# translation in Russian mode. Sorted longest-first at runtime so longer phrases
# take precedence over shorter ones (e.g. "Pro Competition" before "Competition").
RUSSIAN_PHRASES = {
    # ── Competition structure ──────────────────────────────────────────────
    'College Competition': 'Соревнования колледжей',
    'Pro Competition': 'Профессиональные соревнования',
    'Pro-Am Relay': 'Эстафета Про-Ам',
    'Partnered Axe Throw': 'Парное метание топора',
    'Bull of the Woods': 'Король леса',
    'Belle of the Woods': 'Королева леса',
    'Team Standings': 'Командный зачёт',
    'Individual Standings': 'Личный зачёт',
    'Pro Standings': 'Рейтинг профессионалов',
    'College Standings': 'Рейтинг колледжей',
    'Event Results': 'Результаты соревнований',
    'All Results': 'Все результаты',
    'All Events': 'Все соревнования',
    # ── Timbersports events ────────────────────────────────────────────────
    'Springboard': 'Трамплин',
    'Underhand': 'Нижний удар',
    'Standing Block': 'Стоячий блок',
    'Single Buck': 'Одиночная пила',
    'Double Buck': 'Двойная пила',
    'Jack & Jill Sawing': 'Командная пила',
    'Stock Saw': 'Серийная бензопила',
    'Hot Saw': 'Скоростная пила',
    'Obstacle Pole': 'Полоса препятствий',
    'Speed Climb': 'Скоростной подъём',
    'Pole Climb': 'Подъём на шест',
    'Axe Throw': 'Метание топора',
    'Birling': 'Бёрлинг',
    'Cookie Stack': 'Стойки бревна',
    'Caber Toss': 'Метание бревна',
    'Peavey Log Roll': 'Перекатывание бревна',
    'Pulp Toss': 'Метание поленьев',
    "Chokerman's Race": 'Гонка сплавщика',
    '3-Board Jigger': 'Джиггер на 3 доски',
    '1-Board Springboard': 'Трамплин на 1 доску',
    'Pro 1-Board': 'Про трамплин на 1 доску',
    'Hard Hit': 'Силовой удар',
    'Speed': 'Скоростной',
    # ── Schedule / heats / flights ─────────────────────────────────────────
    'Flight': 'Этап',
    'Flights': 'Этапы',
    'Heat': 'Заезд',
    'Heats': 'Заезды',
    'Heat Sheet': 'Протокол заездов',
    'Heat Sheets': 'Протоколы заездов',
    'Run 1': 'Заезд 1',
    'Run 2': 'Заезд 2',
    'Run 3': 'Заезд 3',
    'Stand': 'Стойка',
    'Stands': 'Стойки',
    'Flight Schedule': 'Расписание этапов',
    'Day Schedule': 'Расписание дня',
    'Show Day': 'День шоу',
    'Generate Heats': 'Создать заезды',
    'Build Flights': 'Построить этапы',
    'Pending': 'Ожидание',
    'In Progress': 'В процессе',
    'Completed': 'Завершено',
    'Finalized': 'Финализировано',
    'Locked': 'Заблокировано',
    'Scratched': 'Снят',
    # ── Scoring / results ──────────────────────────────────────────────────
    'Score Entry': 'Ввод результатов',
    'Enter Results': 'Ввести результаты',
    'Enter Scores': 'Ввести результаты',
    'Finalize Event': 'Завершить соревнование',
    'Configure Payouts': 'Настроить выплаты',
    'Payout Summary': 'Итоги выплат',
    'Payout Settlement': 'Расчёт выплат',
    'Payouts': 'Выплаты',
    'Payout': 'Выплата',
    'Points': 'Очки',
    'Position': 'Место',
    'Place': 'Место',
    'Rank': 'Рейтинг',
    'Result': 'Результат',
    'Results': 'Результаты',
    'Score': 'Результат',
    'Time': 'Время',
    'Distance': 'Расстояние',
    'Hits': 'Удары',
    'Best Run': 'Лучший заезд',
    'Tiebreak': 'Тай-брейк',
    'Throwoff': 'Перебивка',
    'Outlier': 'Выброс',
    'Flagged': 'Отмечено',
    'Handicap': 'Гандикап',
    'Start Mark': 'Стартовая метка',
    'Championship': 'Чемпионат',
    # ── Registration ───────────────────────────────────────────────────────
    'Register Competitor': 'Зарегистрировать участника',
    'New Competitor': 'Новый участник',
    'Competitor Details': 'Данные участника',
    'Gear Sharing': 'Совместное снаряжение',
    'Gear Partner': 'Партнёр по снаряжению',
    'Scratch Competitor': 'Снять участника',
    'Pro Competitor': 'Профессиональный участник',
    'College Competitor': 'Участник колледжа',
    'Competitor': 'Участник',
    'Competitors': 'Участники',
    'Team': 'Команда',
    'Teams': 'Команды',
    'School': 'Школа',
    'Partner': 'Партнёр',
    'Partners': 'Партнёры',
    'Entry Fee': 'Взнос за участие',
    'Entry Fees': 'Взносы за участие',
    'Fee Tracker': 'Учёт взносов',
    'ALA Member': 'Член АЛА',
    'Shirt Size': 'Размер футболки',
    'Lottery': 'Жеребьёвка',
    'Opt In': 'Участвовать',
    # ── People / roles ─────────────────────────────────────────────────────
    'Judge': 'Судья',
    'Admin': 'Администратор',
    'Scorer': 'Секретарь',
    'Registrar': 'Регистратор',
    'Spectator': 'Зритель',
    'School Captain': 'Капитан команды',
    'Male': 'Мужской',
    'Female': 'Женский',
    'Men': 'Мужчины',
    'Women': 'Женщины',
    # ── Navigation / UI chrome ─────────────────────────────────────────────
    'Dashboard': 'Панель управления',
    'Tournament': 'Турнир',
    'Tournaments': 'Турниры',
    'New Tournament': 'Новый турнир',
    'Tournament Name': 'Название турнира',
    'Create Tournament': 'Создать турнир',
    'Active Tournament': 'Активный турнир',
    'Language': 'Язык',
    'Settings': 'Настройки',
    'Overview': 'Обзор',
    'Schedule': 'Расписание',
    'Reports': 'Отчёты',
    'Report': 'Отчёт',
    'Validation': 'Проверка',
    'Import': 'Импорт',
    'Export': 'Экспорт',
    'Print': 'Печать',
    'Portal': 'Портал',
    'Guide': 'Руководство',
    'Audit Log': 'Журнал аудита',
    'Users': 'Пользователи',
    'User': 'Пользователь',
    # ── Buttons / actions ──────────────────────────────────────────────────
    'Save Changes': 'Сохранить изменения',
    'Save': 'Сохранить',
    'Cancel': 'Отмена',
    'Edit': 'Редактировать',
    'Delete': 'Удалить',
    'Remove': 'Удалить',
    'Add': 'Добавить',
    'Create': 'Создать',
    'Update': 'Обновить',
    'Submit': 'Отправить',
    'Upload': 'Загрузить',
    'Download': 'Скачать',
    'Search': 'Поиск',
    'Filter': 'Фильтр',
    'Confirm': 'Подтвердить',
    'Generate': 'Сгенерировать',
    'Rebuild': 'Пересобрать',
    'Refresh': 'Обновить',
    'Reset': 'Сбросить',
    'Back': 'Назад',
    'Continue': 'Продолжить',
    'Finish': 'Завершить',
    'Close': 'Закрыть',
    'Open': 'Открыть',
    'View': 'Просмотр',
    'Show': 'Показать',
    'Hide': 'Скрыть',
    'Login': 'Войти',
    'Log In': 'Войти',
    'Logout': 'Выйти',
    'Log Out': 'Выйти',
    'Register': 'Зарегистрироваться',
    'Sign In': 'Войти',
    'Select': 'Выбрать',
    'Apply': 'Применить',
    'Assign': 'Назначить',
    'Swap': 'Переставить',
    'Move': 'Переместить',
    'Copy': 'Копировать',
    'Clone': 'Клонировать',
    'Lock': 'Заблокировать',
    'Unlock': 'Разблокировать',
    'Mark': 'Отметить',
    'Unmark': 'Снять отметку',
    'Enable': 'Включить',
    'Disable': 'Отключить',
    # ── Status / feedback ──────────────────────────────────────────────────
    'Active': 'Активный',
    'Inactive': 'Неактивный',
    'Setup': 'Настройка',
    'Loading': 'Загрузка',
    'Error': 'Ошибка',
    'Warning': 'Предупреждение',
    'Success': 'Успех',
    'Info': 'Информация',
    'Not found': 'Не найдено',
    'No results': 'Нет результатов',
    'No events': 'Нет соревнований',
    'No heats': 'Нет заездов',
    'No flights': 'Нет этапов',
    # ── Form field labels ──────────────────────────────────────────────────
    'Name': 'Имя',
    'Year': 'Год',
    'Date': 'Дата',
    'Email': 'Электронная почта',
    'Phone': 'Телефон',
    'Address': 'Адрес',
    'Gender': 'Пол',
    'Notes': 'Заметки',
    'Details': 'Детали',
    'Summary': 'Итог',
    'Total': 'Итого',
    'Status': 'Статус',
    'Actions': 'Действия',
    'Count': 'Количество',
    'Amount': 'Сумма',
    'Paid': 'Оплачено',
    'Unpaid': 'Не оплачено',
    'Balance': 'Баланс',
    'PIN': 'ПИН',
    'Access': 'Доступ',
    'Password': 'Пароль',
    'Role': 'Роль',
    # ── Days of the week ───────────────────────────────────────────────────
    'Friday': 'Пятница',
    'Saturday': 'Суббота',
    'Sunday': 'Воскресенье',
    'Monday': 'Понедельник',
    'Tuesday': 'Вторник',
    'Wednesday': 'Среда',
    'Thursday': 'Четверг',
    # ── Language names ─────────────────────────────────────────────────────
    'English': 'Английский',
    'Northern Arapaho': 'Северный Арапахо',
    'Russian': 'Русский',
    # ── College-specific ───────────────────────────────────────────────────
    'College Day': 'День колледжа',
    'Pro Day': 'День профессионалов',
    'College Dashboard': 'Панель колледжей',
    'Pro Dashboard': 'Профессиональная панель',
    'Import Teams': 'Импорт команд',
    'Team Code': 'Код команды',
    'School Name': 'Название школы',
    'Events Entered': 'Зарегистрированные соревнования',
    'Individual Points': 'Личные очки',
    'Team Points': 'Командные очки',
    # ── Wood / material planning ───────────────────────────────────────────
    'Wood': 'Дерево',
    'Log': 'Бревно',
    'Block': 'Блок',
    'Species': 'Порода',
    'Diameter': 'Диаметр',
    'Length': 'Длина',
    'Material': 'Материал',
}


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
    'ru': _merge_nested(ENGLISH, RUSSIAN_OVERRIDES),
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
    if lang_code == 'arp':
        # User glossary wins over built-ins for community-approved phrasing.
        merged = dict(ARAPAHO_VERIFIED_PHRASES)
        merged.update(_load_custom_glossary())
        return merged
    if lang_code == 'ru':
        return dict(RUSSIAN_PHRASES)
    return {}


def free_text(text_value: str, lang: str | None = None) -> str:
    """Translate free-form UI text with strict phrase-level substitutions only."""
    lang_code = lang or get_language()
    if lang_code == 'en' or not text_value:
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
    if lang_code == 'en' or not html:
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
