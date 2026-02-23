"""
Centralized text labels for the Missoula Pro Am Tournament Manager.

Edit values here to change any user-visible text across the app.
"""

# Navigation bar labels
NAV = {
    'brand':     'Missoula Pro Am',
    'home':      'Home',
    'dashboard': 'Dashboard',
    'college':   'College',
    'pro':       'Pro',
    'events':    'Events',
}

# Competition / section display names
COMPETITION = {
    'app_title':      'Missoula Pro Am Tournament Manager',
    'app_footer':     'Missoula Pro Am Tournament Manager',
    'college_title':  'College Competition',
    'college_day':    'Friday',
    'pro_title':      'Pro Competition',
    'pro_day':        'Saturday',
    'bull_of_woods':  'Bull of the Woods',
    'belle_of_woods': 'Belle of the Woods',
    'team_standings': 'Team Standings',
}

# Flash messages — use .format(**kwargs) for dynamic values
FLASH = {
    # Tournament management
    'tournament_created':   'Tournament "{name} {year}" created successfully!',
    'college_active':       'College competition is now active!',
    'pro_active':           'Professional competition is now active!',
    'invalid_comp_type':    'Invalid competition type.',
    # Registration
    'no_file':              'No file selected.',
    'import_success':       'Successfully imported {teams} team(s) with {competitors} competitor(s).',
    'import_error':         'Error processing file: {error}',
    'invalid_file_type':    'Invalid file type. Please upload an Excel file (.xlsx or .xls).',
    'competitor_added':     'Competitor "{name}" added successfully!',
    'competitor_scratched': 'Competitor "{name}" has been scratched.',
    # Scheduling
    'events_configured':    'Events configured successfully!',
    'heats_generated':      'Generated {num_heats} heat(s) for {event_name}.',
    'heats_error':          'Error generating heats: {error}',
    'flights_built':        'Built {num_flights} flight(s) for pro competition.',
    'flights_error':        'Error building flights: {error}',
    # Scoring
    'heat_saved':           'Heat results saved successfully!',
    'event_finalized':      '{event_name} has been finalized.',
    'pro_only_payouts':     'Payouts can only be configured for pro events.',
    'payouts_saved':        'Payouts configured successfully!',
}
