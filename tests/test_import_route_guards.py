"""Pure guards at the pro-import confirmation boundary."""

from routes.import_routes import _gender_mismatch_messages


def test_gender_mismatch_messages_find_every_invalid_event():
    messages = _gender_mismatch_messages([
        {
            'name': 'Wrong Division',
            'gender': 'M',
            'events': ["Women's Standing Block Speed", 'Hot Saw'],
        },
    ])

    assert messages == [
        "Wrong Division: GENDER MISMATCH: Male competitor signed up for "
        "Women's Standing Block Speed"
    ]


def test_gender_mismatch_messages_allow_neutral_events():
    assert _gender_mismatch_messages([
        {'name': 'Valid Entry', 'gender': 'F', 'events': ['Hot Saw']},
    ]) == []
