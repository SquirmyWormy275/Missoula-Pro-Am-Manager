from datetime import datetime


def test_utc_now_naive_returns_naive_datetime():
    from services.time_utils import utc_now_naive

    value = utc_now_naive()

    assert isinstance(value, datetime)
    assert value.tzinfo is None


def test_utc_timestamp_for_filename_is_compact():
    from services.time_utils import utc_timestamp_for_filename

    value = utc_timestamp_for_filename()

    assert len(value) == 15
    assert value[8] == '_'
    assert value.replace('_', '').isdigit()
