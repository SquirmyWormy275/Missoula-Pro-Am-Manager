"""
Pro competitor entry importer for Google Forms xlsx exports.

Reads the first sheet of an xlsx workbook (Google always puts "Form Responses 1"
first) and returns a list of parsed entry dicts ready for review and DB commit.
"""
import openpyxl
from datetime import datetime

# Waiver column is identified by this prefix (full text is too long to quote here)
_WAIVER_HEADER_START = 'I know that logging events'

# Maps stripped form header -> (canonical event name, fee amount)
_EVENT_MAP = {
    'Springboard (L)':                  ('Springboard (L)',          10),
    'Springboard (R)':                  ('Springboard (R)',          10),
    'Intermediate 1-Board Springboard': ('1-Board Springboard',      10),
    "Men's Underhand":                  ("Men's Underhand",          10),
    "Women's Underhand":                ("Women's Underhand",        10),
    "Women's Standing Block":           ("Women's Standing Block",   10),
    "Men's Single Buck":                ("Men's Single Buck",         5),
    "Women's Single Buck":              ("Women's Single Buck",       5),
    "Men's Double Buck":                ("Men's Double Buck",         5),
    'Jack & Jill':                      ('Jack & Jill',               5),
    'Hot Saw':                          ('Hot Saw',                   5),
    'Obstacle Pole':                    ('Obstacle Pole',             5),
    'Speed Climb':                      ('Speed Climb',               5),
    'Cookie Stack':                     ('Cookie Stack',              5),
    'Partnered Axe Throw':              ('Partnered Axe Throw',       5),
}

# Maps lowercased stripped partner-column header -> canonical event name
_PARTNER_COLS = {
    "men's double buck partner name": "Men's Double Buck",
    "jack & jill partner name":       "Jack & Jill",
    "partnered axe throw 2":          "Partnered Axe Throw",
}


def _yes(val) -> bool:
    """Return True only when value is the string 'Yes' (case-insensitive)."""
    return str(val).strip().lower() == 'yes' if val is not None else False


def _get(row: tuple, col) -> object:
    """Safely retrieve a value from a row tuple by column index."""
    if col is None or col >= len(row):
        return None
    return row[col]


def parse_pro_entries(filepath: str) -> list:
    """
    Parse a Google Forms xlsx export (first sheet) and return a list of dicts.

    Each dict contains all form data needed for review and DB import.
    Datetime objects are converted to ISO strings for JSON serialisability.
    Rows where 'Full Name' is blank are silently skipped.
    """
    wb = openpyxl.load_workbook(filepath, data_only=True)
    ws = wb.worksheets[0]

    # Read and strip every header cell from row 1
    raw_headers = next(ws.iter_rows(min_row=1, max_row=1, values_only=True))
    stripped = [h.strip() if isinstance(h, str) else (h or '') for h in raw_headers]

    # Exact-match lookup: stripped_header -> 0-based column index
    hmap = {h: i for i, h in enumerate(stripped) if h}

    # Prefix-matched special columns
    waiver_col      = next((i for i, h in enumerate(stripped) if h.startswith(_WAIVER_HEADER_START)), None)
    gear_detail_col = next((i for i, h in enumerate(stripped) if h.lower().startswith('if yes, provide')), None)
    notes_col       = next((i for i, h in enumerate(stripped) if h.lower().startswith('anything else we should know')), None)

    entries = []

    for row in ws.iter_rows(min_row=2, values_only=True):
        # Skip rows with no name (trailing blank rows, etc.)
        name_val = _get(row, hmap.get('Full Name'))
        if not name_val or not str(name_val).strip():
            continue

        # --- Timestamp ---
        ts = _get(row, hmap.get('Timestamp'))
        timestamp_str = ts.isoformat() if isinstance(ts, datetime) else (str(ts) if ts else None)

        # --- Basic personal fields ---
        email = str(_get(row, hmap.get('Email Address')) or '').strip() or None
        name  = str(name_val).strip()

        gender_raw = str(_get(row, hmap.get('Gender')) or '').strip()
        if gender_raw == 'Male':
            gender = 'M'
        elif gender_raw == 'Female':
            gender = 'F'
        else:
            gender = gender_raw[:1].upper() if gender_raw else ''

        mailing_address = str(_get(row, hmap.get('Mailing Address')) or '').strip() or None

        # Phone: Google exports as float (e.g. 3033496312.0) -> int -> str
        phone_val = _get(row, hmap.get('Phone Number'))
        if phone_val is not None:
            try:
                phone = str(int(float(phone_val)))
            except (TypeError, ValueError):
                phone = str(phone_val).strip() or None
        else:
            phone = None

        ala_member = _yes(_get(row, hmap.get('Are you a current ALA member?')))

        # --- Events and fees ---
        events        = []
        chopping_fees = 0
        other_fees    = 0

        for form_header, (event_name, fee) in _EVENT_MAP.items():
            if _yes(_get(row, hmap.get(form_header))):
                events.append(event_name)
                if fee == 10:
                    chopping_fees += 10
                else:
                    other_fees += 5

        # --- Relay lottery ---
        relay_lottery = _yes(_get(row, hmap.get('I would like to enter into the Pro-Am lottery')))
        relay_fee     = 5 if relay_lottery else 0
        total_fees    = chopping_fees + other_fees + relay_fee

        # --- Partners (by lowercased stripped header match) ---
        partners = {}
        for stripped_header, event_name in _PARTNER_COLS.items():
            col_idx = next(
                (i for i, h in enumerate(stripped) if h.lower() == stripped_header),
                None
            )
            if col_idx is not None:
                val = _get(row, col_idx)
                if val and str(val).strip():
                    partners[event_name] = str(val).strip()

        # --- Gear sharing ---
        gear_sharing = _yes(_get(row, hmap.get('Are you sharing gear?')))
        gd_val = _get(row, gear_detail_col) if gear_detail_col is not None else None
        gear_sharing_details = str(gd_val).strip() if gd_val and str(gd_val).strip() else None

        # --- Waiver ---
        waiver_val = _get(row, waiver_col) if waiver_col is not None else None
        if waiver_val:
            wv = str(waiver_val).strip()
            waiver_accepted = wv == 'Yes' or wv.startswith('I know')
        else:
            waiver_accepted = False

        sig_val = _get(row, hmap.get('Signature'))
        waiver_signature = str(sig_val).strip() if sig_val and str(sig_val).strip() else None

        # --- Notes ---
        nv = _get(row, notes_col) if notes_col is not None else None
        notes = str(nv).strip() if nv and str(nv).strip() else None

        entries.append({
            'submission_timestamp': timestamp_str,
            'email':                email,
            'name':                 name,
            'gender':               gender,
            'mailing_address':      mailing_address,
            'phone':                phone,
            'ala_member':           ala_member,
            'events':               events,
            'relay_lottery':        relay_lottery,
            'partners':             partners,
            'gear_sharing':         gear_sharing,
            'gear_sharing_details': gear_sharing_details,
            'waiver_accepted':      waiver_accepted,
            'waiver_signature':     waiver_signature,
            'notes':                notes,
            'chopping_fees':        chopping_fees,
            'other_fees':           other_fees,
            'relay_fee':            relay_fee,
            'total_fees':           total_fees,
        })

    return entries


def compute_review_flags(entries: list) -> list:
    """
    Add 'flags' (list of warning strings) and 'flag_class' (Bootstrap row class)
    to each entry dict in-place.  Returns the same list for convenience.

    Rules
    -----
    - Red  (table-danger)  : waiver_accepted is False -> 'NO WAIVER'
    - Yellow (table-warning): partner name listed but not found in this batch
                              -> 'PARTNER NOT FOUND: <name>'
    - Yellow (table-warning): gear_sharing True but gear_sharing_details blank
                              -> 'GEAR SHARING DETAILS MISSING'
    """
    all_names = {e['name'].strip().lower() for e in entries}

    for entry in entries:
        flags      = []
        flag_class = ''

        if not entry['waiver_accepted']:
            flags.append('NO WAIVER')
            flag_class = 'table-danger'

        for event_name, partner_name in entry.get('partners', {}).items():
            if partner_name and partner_name.strip().lower() not in all_names:
                flags.append(f'PARTNER NOT FOUND: {partner_name}')
                if not flag_class:
                    flag_class = 'table-warning'

        if entry['gear_sharing'] and not entry['gear_sharing_details']:
            flags.append('GEAR SHARING DETAILS MISSING')
            if not flag_class:
                flag_class = 'table-warning'

        entry['flags']      = flags
        entry['flag_class'] = flag_class

    return entries
