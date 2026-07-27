"""Time helpers for UTC-safe service code."""
from datetime import datetime, timezone


def utc_now_naive() -> datetime:
    """Return current UTC time as a naive datetime for legacy DateTime columns."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def utc_timestamp_for_filename() -> str:
    """Return a compact UTC timestamp safe for filenames."""
    return utc_now_naive().strftime('%Y%m%d_%H%M%S')
