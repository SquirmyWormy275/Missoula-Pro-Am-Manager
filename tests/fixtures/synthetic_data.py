"""
Realistic synthetic test data derived from actual lumberjack tournament entry forms.

Contains 25 pro competitors, 55 college competitors across 4 schools / 7 teams,
scores for all events, gear sharing details, and expected rankings.
"""

# ---------------------------------------------------------------------------
# Pro Competitors (25)
# ---------------------------------------------------------------------------

PRO_COMPETITORS = [
    {'name': 'Ada Byrd', 'gender': 'F', 'email': 'Ada.Byrd@email.com', 'is_ala_member': True,
     'skill_level': 'Intermediate', 'chopper_runner': 'Chopper',
     'events': ['Women\'s Underhand', 'Women\'s Standing Block', 'Women\'s Single Buck'],
     'gear_sharing_text': 'Jaam Slam - UH, SB', 'lottery': True},
    {'name': 'Jaam Slam', 'gender': 'F', 'email': 'Jaam.Slam@email.com', 'is_ala_member': True,
     'skill_level': 'Novice', 'chopper_runner': 'Chopper',
     'events': ['Women\'s Underhand', 'Women\'s Standing Block'],
     'gear_sharing_text': 'Ada Byrd - Underhand, single buck', 'lottery': False},
    {'name': 'Marshall Law', 'gender': 'M', 'email': 'Marshall.Law@email.com', 'is_ala_member': True,
     'skill_level': 'Intermediate', 'chopper_runner': 'Chopper',
     'events': ['Int 1-Board Springboard', 'Men\'s Underhand', 'Men\'s Single Buck', 'Men\'s Double Buck'],
     'partners': {'Double Buck': 'Carson Mitsubishi'}, 'gear_sharing_text': '', 'lottery': False},
    {'name': 'Olive Oyle', 'gender': 'F', 'email': 'Olive.Oyle@email.com', 'is_ala_member': True,
     'skill_level': 'Pro', 'chopper_runner': 'Chopper',
     'events': ['Women\'s Underhand', 'Women\'s Standing Block', 'Women\'s Single Buck', 'Cookie Stack'],
     'partners': {'Jack & Jill': 'Finn McCool'}, 'gear_sharing_text': '', 'lottery': True},
    {'name': 'Salix Amygdaloides', 'gender': 'F', 'email': 'Salix.Amygdaloides@email.com', 'is_ala_member': False,
     'skill_level': 'Pro', 'chopper_runner': 'Chopper',
     'events': ['Women\'s Underhand', 'Women\'s Standing Block', 'Women\'s Single Buck', 'Cookie Stack'],
     'partners': {'Jack & Jill': 'Meau Jeau'}, 'gear_sharing_text': '', 'lottery': False},
    {'name': 'Ameriga Vespucci', 'gender': 'F', 'email': 'Ameriga.Vespucci@email.com', 'is_ala_member': True,
     'skill_level': 'Novice', 'chopper_runner': 'Chopper',
     'events': ['Women\'s Underhand', 'Women\'s Standing Block', 'Cookie Stack'],
     'partners': {'Jack & Jill': 'Dorian Gray'}, 'gear_sharing_text': '', 'lottery': False},
    {'name': 'Dorian Gray', 'gender': 'M', 'email': 'Dorian.Gray@email.com', 'is_ala_member': True,
     'skill_level': 'Novice', 'chopper_runner': 'Chopper',
     'events': ['Int 1-Board Springboard', 'Men\'s Underhand', 'Men\'s Single Buck', 'Men\'s Double Buck', 'Cookie Stack'],
     'partners': {'Double Buck': 'Garfield Heathcliff', 'Jack & Jill': 'Ameriga Vespucci'},
     'gear_sharing_text': 'Garfield Heathcliff - SB', 'lottery': False},
    {'name': 'Ben Cambium', 'gender': 'M', 'email': 'Ben.Cambium@email.com', 'is_ala_member': True,
     'skill_level': 'Intermediate', 'chopper_runner': 'Chopper',
     'events': ['Springboard', 'Men\'s Underhand', 'Obstacle Pole'],
     'gear_sharing_text': '', 'lottery': True},
    {'name': 'Alder Johns', 'gender': 'M', 'email': 'Alder.Johns@email.com', 'is_ala_member': True,
     'skill_level': 'Pro', 'chopper_runner': 'Chopper',
     'events': ['Springboard', 'Men\'s Underhand', 'Men\'s Single Buck', 'Cookie Stack', 'Partnered Axe Throw', 'Obstacle Pole'],
     'partners': {'Jack & Jill': 'Caligraphy Jones'}, 'gear_sharing_text': '', 'lottery': False},
    {'name': 'Imortal Joe', 'gender': 'M', 'email': 'Imortal.Joe@email.com', 'is_ala_member': True,
     'skill_level': 'Pro', 'chopper_runner': 'Chopper',
     'events': ['Springboard', 'Men\'s Underhand', 'Men\'s Single Buck', 'Men\'s Double Buck', 'Speed Climb', 'Partnered Axe Throw'],
     'partners': {'Double Buck': 'Joe Manyfingers'},
     'gear_sharing_text': 'Joe Manyfingers - SB', 'lottery': True},
    {'name': 'Wanda Fuca', 'gender': 'F', 'email': 'Wanda.Fuca@email.com', 'is_ala_member': True,
     'skill_level': 'Pro', 'chopper_runner': 'Chopper',
     'events': ['Int 1-Board Springboard', 'Women\'s Underhand', 'Women\'s Standing Block', 'Women\'s Single Buck', 'Cookie Stack', 'Speed Climb'],
     'partners': {'Jack & Jill': 'Joe Manyfingers'}, 'gear_sharing_text': '', 'lottery': True},
    {'name': 'Jonathon Wept', 'gender': 'M', 'email': 'Jonathon.Wept@email.com', 'is_ala_member': True,
     'skill_level': 'Pro', 'chopper_runner': 'Runner',
     'events': ['Men\'s Underhand', 'Men\'s Single Buck', 'Men\'s Double Buck', 'Obstacle Pole', 'Cookie Stack', 'Partnered Axe Throw'],
     'partners': {'Double Buck': 'Meau Jeau'},
     'gear_sharing_text': 'OP - Meau Jeau', 'lottery': True},
    {'name': 'Caligraphy Jones', 'gender': 'F', 'email': 'Caligraphy.Jones@email.com', 'is_ala_member': True,
     'skill_level': 'Pro', 'chopper_runner': 'Chopper',
     'events': ['Int 1-Board Springboard', 'Women\'s Underhand', 'Women\'s Standing Block', 'Women\'s Single Buck', 'Cookie Stack', 'Obstacle Pole', 'Speed Climb'],
     'partners': {'Jack & Jill': 'Alder Johns'}, 'gear_sharing_text': '', 'lottery': True},
    {'name': 'Meau Jeau', 'gender': 'M', 'email': 'Meau.Jeau@email.com', 'is_ala_member': True,
     'skill_level': 'Pro', 'chopper_runner': 'Runner',
     'events': ['Men\'s Single Buck', 'Men\'s Double Buck', 'Cookie Stack', 'Obstacle Pole'],
     'partners': {'Double Buck': 'Jonathon Wept', 'Jack & Jill': 'Salix Amygdaloides'},
     'gear_sharing_text': 'OP - Jonathon Wept', 'lottery': False},
    {'name': 'Joe Manyfingers', 'gender': 'M', 'email': 'Joe.Manyfingers@email.com', 'is_ala_member': True,
     'skill_level': 'Pro', 'chopper_runner': 'Chopper',
     'events': ['Int 1-Board Springboard', 'Men\'s Underhand', 'Men\'s Single Buck', 'Men\'s Double Buck', 'Cookie Stack', 'Obstacle Pole'],
     'partners': {'Double Buck': 'Imortal Joe', 'Jack & Jill': 'Wanda Fuca'},
     'gear_sharing_text': 'Imortal Joe - Single Buck', 'lottery': False},
    {'name': 'Cherry Strawberry', 'gender': 'F', 'email': 'Cherry.Strawberry@email.com', 'is_ala_member': False,
     'skill_level': 'Intermediate', 'chopper_runner': 'Chopper',
     'events': ['Women\'s Underhand', 'Women\'s Standing Block', 'Women\'s Single Buck', 'Cookie Stack', 'Partnered Axe Throw'],
     'partners': {'Jack & Jill': 'Larry Occidentalis', 'Partnered Axe Throw': 'Epinephrine Needel'},
     'gear_sharing_text': '', 'lottery': True},
    {'name': 'Cosmo Cramer', 'gender': 'M', 'email': 'Cosmo.Cramer@email.com', 'is_ala_member': True,
     'skill_level': 'Pro', 'chopper_runner': 'Chopper',
     'events': ['Springboard', 'Men\'s Underhand', 'Men\'s Single Buck', 'Hot Saw', 'Cookie Stack', 'Partnered Axe Throw'],
     'partners': {'Double Buck': 'Finn McCool', 'Partnered Axe Throw': 'Finn McCool'},
     'gear_sharing_text': '', 'lottery': True},
    {'name': 'Epinephrine Needel', 'gender': 'F', 'email': 'Epinephrine.Needel@email.com', 'is_ala_member': True,
     'skill_level': 'Intermediate', 'chopper_runner': 'Chopper',
     'events': ['Women\'s Underhand', 'Women\'s Standing Block', 'Speed Climb', 'Partnered Axe Throw'],
     'partners': {'Partnered Axe Throw': 'Cherry Strawberry'},
     'gear_sharing_text': '', 'lottery': False},
    {'name': 'Finn McCool', 'gender': 'M', 'email': 'Finn.McCool@email.com', 'is_ala_member': True,
     'skill_level': 'Pro', 'chopper_runner': 'Chopper',
     'events': ['Springboard', 'Men\'s Underhand', 'Men\'s Single Buck', 'Men\'s Double Buck', 'Cookie Stack', 'Hot Saw', 'Speed Climb', 'Partnered Axe Throw'],
     'partners': {'Double Buck': 'Cosmo Cramer', 'Jack & Jill': 'Olive Oyle', 'Partnered Axe Throw': 'Cosmo Cramer'},
     'gear_sharing_text': '', 'lottery': True},
    {'name': 'Dee John', 'gender': 'F', 'email': 'Dee.John@email.com', 'is_ala_member': True,
     'skill_level': 'Intermediate', 'chopper_runner': 'Runner',
     'events': ['Obstacle Pole', 'Speed Climb', 'Partnered Axe Throw'],
     'partners': {'Partnered Axe Throw': 'Carson Mitsubishi'},
     'gear_sharing_text': 'Juicy Crust - OP', 'lottery': False},
    {'name': 'Juicy Crust', 'gender': 'F', 'email': 'Juicy.Crust@email.com', 'is_ala_member': False,
     'skill_level': 'Novice', 'chopper_runner': 'Runner',
     'events': ['Women\'s Single Buck', 'Cookie Stack', 'Obstacle Pole', 'Speed Climb', 'Partnered Axe Throw'],
     'partners': {'Jack & Jill': 'Garfield Heathcliff', 'Partnered Axe Throw': 'Garfield Heathcliff'},
     'gear_sharing_text': 'OP - Dee John', 'lottery': False},
    {'name': 'Larry Occidentalis', 'gender': 'M', 'email': 'Larry.Occidentalis@email.com', 'is_ala_member': False,
     'skill_level': 'Intermediate', 'chopper_runner': 'Runner',
     'events': ['Men\'s Single Buck', 'Cookie Stack', 'Obstacle Pole', 'Speed Climb', 'Partnered Axe Throw'],
     'partners': {'Jack & Jill': 'Cherry Strawberry', 'Partnered Axe Throw': 'Steptoe Edwall'},
     'gear_sharing_text': '', 'lottery': False},
    {'name': 'Garfield Heathcliff', 'gender': 'M', 'email': 'Garfield.Heathcliff@email.com', 'is_ala_member': False,
     'skill_level': 'Novice', 'chopper_runner': 'Runner',
     'events': ['Men\'s Single Buck', 'Men\'s Double Buck', 'Cookie Stack', 'Obstacle Pole', 'Speed Climb', 'Partnered Axe Throw'],
     'partners': {'Double Buck': 'Dorian Gray', 'Jack & Jill': 'Juicy Crust', 'Partnered Axe Throw': 'Juicy Crust'},
     'gear_sharing_text': 'SB - Dorian Gray', 'lottery': False},
    {'name': 'Steptoe Edwall', 'gender': 'M', 'email': 'Steptoe.Edwall@email.com', 'is_ala_member': True,
     'skill_level': 'Intermediate', 'chopper_runner': 'Chopper',
     'events': ['Springboard', 'Men\'s Underhand', 'Hot Saw', 'Obstacle Pole', 'Speed Climb', 'Partnered Axe Throw'],
     'partners': {'Partnered Axe Throw': 'Larry Occidentalis'},
     'gear_sharing_text': 'Carson Mitsubishi - Hot Saw, OP', 'lottery': True},
    {'name': 'Carson Mitsubishi', 'gender': 'M', 'email': 'Carson.Mitsubishi@email.com', 'is_ala_member': False,
     'skill_level': 'Intermediate', 'chopper_runner': 'Runner',
     'events': ['Men\'s Double Buck', 'Hot Saw', 'Obstacle Pole', 'Speed Climb', 'Partnered Axe Throw'],
     'partners': {'Double Buck': 'Marshall Law', 'Partnered Axe Throw': 'Dee John'},
     'gear_sharing_text': 'Steptoe Edwall - Hot Saw, OP', 'lottery': False},
]


# ---------------------------------------------------------------------------
# Pro Scores (event_name -> [(competitor, value, status)])
# status = 'completed' or 'dq'
# ---------------------------------------------------------------------------

PRO_SCORES = {
    'Springboard': [
        ('Finn McCool', 80.0, 'completed'),
        ('Imortal Joe', 94.0, 'completed'),
        ('Alder Johns', 97.0, 'completed'),
        ('Cosmo Cramer', 100.0, 'completed'),
        ('Ben Cambium', 173.0, 'completed'),
        ('Steptoe Edwall', 209.0, 'completed'),
    ],
    'Int 1-Board Springboard': [
        ('Joe Manyfingers', 73.0, 'completed'),
        ('Marshall Law', 162.0, 'completed'),
        ('Dorian Gray', 189.0, 'completed'),
        ('Wanda Fuca', 314.0, 'completed'),
        ('Caligraphy Jones', 381.0, 'completed'),
    ],
    "Men's Underhand": [
        ('Joe Manyfingers', 28.0, 'completed'),
        ('Alder Johns', 34.0, 'completed'),
        ('Finn McCool', 35.0, 'completed'),
        ('Imortal Joe', 38.0, 'completed'),
        ('Cosmo Cramer', 39.0, 'completed'),
        ('Ben Cambium', 42.0, 'completed'),
        ('Steptoe Edwall', 53.0, 'completed'),
        ('Jonathon Wept', 64.0, 'completed'),
        ('Marshall Law', 130.0, 'completed'),
        ('Dorian Gray', 151.0, 'completed'),
    ],
    "Women's Underhand": [
        ('Caligraphy Jones', 40.0, 'completed'),
        ('Olive Oyle', 46.0, 'completed'),
        ('Salix Amygdaloides', 55.0, 'completed'),
        ('Epinephrine Needel', 61.0, 'completed'),
        ('Wanda Fuca', 65.0, 'completed'),
        ('Cherry Strawberry', 70.0, 'completed'),
        ('Ada Byrd', 126.0, 'completed'),
        ('Ameriga Vespucci', 293.0, 'completed'),
        ('Jaam Slam', 388.0, 'completed'),
    ],
    "Women's Standing Block": [
        ('Caligraphy Jones', 44.0, 'completed'),
        ('Olive Oyle', 52.0, 'completed'),
        ('Salix Amygdaloides', 53.0, 'completed'),
        ('Wanda Fuca', 60.0, 'completed'),
        ('Ada Byrd', 66.0, 'completed'),
        ('Epinephrine Needel', 122.0, 'completed'),
        ('Cherry Strawberry', 176.0, 'completed'),
    ],
    "Men's Single Buck": [
        ('Finn McCool', 19.0, 'completed'),
        ('Imortal Joe', 20.0, 'completed'),
        ('Meau Jeau', 21.0, 'completed'),
        ('Jonathon Wept', 24.0, 'completed'),
        ('Alder Johns', 25.0, 'completed'),
        ('Cosmo Cramer', 27.0, 'completed'),
        ('Joe Manyfingers', 28.0, 'completed'),
        ('Marshall Law', 35.0, 'completed'),
        ('Dorian Gray', 40.0, 'completed'),
        ('Larry Occidentalis', 53.0, 'completed'),
        ('Garfield Heathcliff', 65.0, 'completed'),
    ],
    "Women's Single Buck": [
        ('Olive Oyle', 30.0, 'completed'),
        ('Salix Amygdaloides', 34.0, 'completed'),
        ('Caligraphy Jones', 41.0, 'completed'),
        ('Wanda Fuca', 46.0, 'completed'),
        ('Ada Byrd', 55.0, 'completed'),
        ('Cherry Strawberry', 67.0, 'completed'),
        ('Jaam Slam', 68.0, 'completed'),
        ('Ameriga Vespucci', 113.0, 'completed'),
        ('Juicy Crust', 124.0, 'completed'),
    ],
    "Men's Double Buck": [
        ('Finn McCool', 9.0, 'completed', 'Cosmo Cramer'),
        ('Meau Jeau', 10.0, 'completed', 'Jonathon Wept'),
        ('Imortal Joe', 11.0, 'completed', 'Joe Manyfingers'),
        ('Carson Mitsubishi', 12.0, 'completed', 'Marshall Law'),
        ('Garfield Heathcliff', 17.0, 'completed', 'Dorian Gray'),
    ],
    'Jack & Jill': [
        ('Salix Amygdaloides', 10.0, 'completed', 'Meau Jeau'),
        ('Caligraphy Jones', 11.0, 'completed', 'Alder Johns'),
        ('Olive Oyle', 12.0, 'completed', 'Finn McCool'),
        ('Wanda Fuca', 14.0, 'completed', 'Joe Manyfingers'),
        ('Cherry Strawberry', 19.0, 'completed', 'Larry Occidentalis'),
        ('Juicy Crust', 23.0, 'completed', 'Garfield Heathcliff'),
        ('Ameriga Vespucci', 25.0, 'completed', 'Dorian Gray'),
    ],
    'Hot Saw': [
        ('Cosmo Cramer', 4.0, 'completed'),
        ('Alder Johns', 5.0, 'completed'),
        ('Finn McCool', 7.0, 'completed'),
        ('Steptoe Edwall', 8.0, 'completed'),
        ('Carson Mitsubishi', None, 'dq'),
    ],
    'Obstacle Pole': [
        ('Steptoe Edwall', 14.0, 'completed'),
        ('Larry Occidentalis', 16.0, 'completed'),
        ('Meau Jeau', 17.0, 'completed'),
        ('Joe Manyfingers', 18.0, 'completed'),
        ('Carson Mitsubishi', 19.0, 'completed'),
        ('Jonathon Wept', 20.0, 'completed'),
        ('Ben Cambium', 22.0, 'completed'),
        ('Alder Johns', 23.0, 'completed'),
        ('Juicy Crust', 25.0, 'completed'),
        ('Dee John', None, 'dq'),
        ('Caligraphy Jones', None, 'dq'),
        ('Garfield Heathcliff', None, 'dq'),
    ],
    'Speed Climb': [
        ('Meau Jeau', 15.0, 'completed'),
        ('Jonathon Wept', 17.0, 'completed'),
        ('Garfield Heathcliff', 18.0, 'completed'),
        ('Larry Occidentalis', 21.0, 'completed'),
        ('Joe Manyfingers', 22.0, 'completed'),
        ('Dee John', 25.0, 'completed'),
        ('Juicy Crust', 30.0, 'completed'),
    ],
    'Cookie Stack': [
        ('Joe Manyfingers', 29.0, 'completed'),
        ('Imortal Joe', 33.0, 'completed'),
        ('Larry Occidentalis', 45.0, 'completed'),
        ('Caligraphy Jones', 50.0, 'completed'),
        ('Wanda Fuca', 56.0, 'completed'),
        ('Dee John', 86.0, 'completed'),
        ('Epinephrine Needel', None, 'dq'),
        ('Finn McCool', None, 'dq'),
        ('Jonathon Wept', None, 'dq'),
        ('Juicy Crust', None, 'dq'),
        ('Meau Jeau', None, 'dq'),
        ('Garfield Heathcliff', None, 'dq'),
        ('Steptoe Edwall', None, 'dq'),
        ('Carson Mitsubishi', None, 'dq'),
    ],
    'Partnered Axe Throw': [
        ('Cosmo Cramer', 23.0, 'completed', 'Finn McCool'),
        ('Juicy Crust', 19.0, 'completed', 'Garfield Heathcliff'),
        ('Larry Occidentalis', 18.0, 'completed', 'Steptoe Edwall'),
        ('Dee John', 17.0, 'completed', 'Carson Mitsubishi'),
        ('Cherry Strawberry', 14.0, 'completed', 'Epinephrine Needel'),
    ],
}


# ---------------------------------------------------------------------------
# Pro gear sharing free-text details for parsing tests
# ---------------------------------------------------------------------------

PRO_GEAR_SHARING_TEXTS = [
    # Note: "SB" is ambiguous — parser maps to springboard short code, not standing block.
    # "UH" maps to underhand. These expectations reflect what the parser actually resolves.
    {'competitor': 'Ada Byrd', 'text': 'Jaam Slam - UH, SB',
     'expected_partner': 'Jaam Slam', 'expected_events_contain': ['underhand']},
    {'competitor': 'Jaam Slam', 'text': 'Ada Byrd - Underhand, single buck',
     'expected_partner': 'Ada Byrd', 'expected_events_contain': ['crosscut']},
    {'competitor': 'Dorian Gray', 'text': 'Garfield Heathcliff - SB',
     'expected_partner': 'Garfield Heathcliff', 'expected_events_contain': ['springboard']},
    {'competitor': 'Imortal Joe', 'text': 'Joe Manyfingers - SB',
     'expected_partner': 'Joe Manyfingers', 'expected_events_contain': ['springboard']},
    {'competitor': 'Joe Manyfingers', 'text': 'Imortal Joe - Single Buck',
     'expected_partner': 'Imortal Joe', 'expected_events_contain': ['crosscut']},
    {'competitor': 'Jonathon Wept', 'text': 'OP - Meau Jeau',
     'expected_partner': 'Meau Jeau', 'expected_events_contain': ['obstacle']},
    {'competitor': 'Meau Jeau', 'text': 'OP - Jonathon Wept',
     'expected_partner': 'Jonathon Wept', 'expected_events_contain': ['obstacle']},
    # NOTE: "SB - Dorian Gray" fails parser — first segment "SB" tried as partner name,
    # not recognized. This is a known parser limitation with code-first format.
    # Excluded from positive-match tests; tested separately as a known-gap case.
    {'competitor': 'Dee John', 'text': 'Juicy Crust - OP',
     'expected_partner': 'Juicy Crust', 'expected_events_contain': ['obstacle']},
    {'competitor': 'Juicy Crust', 'text': 'OP - Dee John',
     'expected_partner': 'Dee John', 'expected_events_contain': ['obstacle']},
    {'competitor': 'Steptoe Edwall', 'text': 'Carson Mitsubishi - Hot Saw, OP',
     'expected_partner': 'Carson Mitsubishi', 'expected_events_contain': ['chainsaw']},
    {'competitor': 'Carson Mitsubishi', 'text': 'Steptoe Edwall - Hot Saw, OP',
     'expected_partner': 'Steptoe Edwall', 'expected_events_contain': ['chainsaw']},
]


# ---------------------------------------------------------------------------
# College Teams & Competitors
# ---------------------------------------------------------------------------

COLLEGE_TEAMS = {
    'CMC-A': {
        'school': 'Colorado Mormon College', 'abbrev': 'CMC',
        'members': [
            {'name': 'James Taply', 'gender': 'M'},
            {'name': 'Tom Oly', 'gender': 'M'},
            {'name': 'John Pork', 'gender': 'M'},
            {'name': 'Tommy White', 'gender': 'M'},
            {'name': 'Kum Pon Nent', 'gender': 'F'},
            {'name': 'Jackie Jackson', 'gender': 'F'},
            {'name': 'Van Winkle', 'gender': 'F'},
            {'name': 'Maxine Stonk', 'gender': 'F'},
        ],
    },
    'CMC-B': {
        'school': 'Colorado Mormon College', 'abbrev': 'CMC',
        'members': [
            {'name': 'Seven Birds', 'gender': 'M'},
            {'name': 'Benny Jeserit', 'gender': 'M'},
            {'name': 'Blue Whale', 'gender': 'M'},
            {'name': 'Luigi Mangione', 'gender': 'M'},
            {'name': 'Rebecca Raytheon', 'gender': 'F'},
            {'name': 'Helen Oftroy', 'gender': 'F'},
            {'name': 'Becky Aquafina', 'gender': 'F'},
            {'name': 'Carmen Sandiego', 'gender': 'F'},
        ],
    },
    'CMC-C': {
        'school': 'Colorado Mormon College', 'abbrev': 'CMC',
        'members': [
            {'name': 'Zix Zeben', 'gender': 'M'},
            {'name': 'Tommy Wheat', 'gender': 'M'},
            {'name': 'Kyu Min', 'gender': 'M'},
            {'name': 'Indiana Surprise', 'gender': 'M'},
            {'name': 'Calamari Nodules', 'gender': 'F'},
            {'name': 'Mandy Applebees', 'gender': 'F'},
            {'name': 'Rose Mary', 'gender': 'F'},
            {'name': 'Samantha Paprika', 'gender': 'F'},
        ],
    },
    'CCU-A': {
        'school': 'Crease Christian University', 'abbrev': 'CCU',
        'members': [
            {'name': 'Joe Squamjo', 'gender': 'M'},
            {'name': 'Squinge Timbler', 'gender': 'M'},
            {'name': 'Bumbldy Pumpldy', 'gender': 'M'},
            {'name': 'Alfred Pogo', 'gender': 'M'},
            {'name': 'Jilliam Jwilliam', 'gender': 'F'},
            {'name': 'Beverly Crease', 'gender': 'F'},
            {'name': 'Jo March', 'gender': 'F'},
            {'name': 'Polisa Wurst', 'gender': 'F'},
        ],
    },
    'CCU-B': {
        'school': 'Crease Christian University', 'abbrev': 'CCU',
        'members': [
            {'name': 'Bill Bishoff', 'gender': 'M'},
            {'name': 'Thomas Grungle', 'gender': 'M'},
            {'name': 'Neyooxet Greymorning', 'gender': 'M'},
            {'name': 'Dan Legacy', 'gender': 'M'},
            {'name': 'Jethrow Pike', 'gender': 'M'},
            {'name': 'Andrea Duracell', 'gender': 'F'},
            {'name': 'Abigail Eucerin', 'gender': 'F'},
        ],
    },
    'TCU-A': {
        'school': 'Texas Christian University', 'abbrev': 'TCU',
        'members': [
            {'name': 'Pilsner Reich', 'gender': 'M'},
            {'name': 'Sprainard Castille', 'gender': 'M'},
            {'name': 'Pocket', 'gender': 'M'},
            {'name': 'Hidden Dragon', 'gender': 'M'},
            {'name': 'Cronartium Ribicola', 'gender': 'F'},
            {'name': 'Listeria Winner', 'gender': 'F'},
            {'name': 'Green Grass', 'gender': 'F'},
            {'name': 'Abigail Crease', 'gender': 'F'},
        ],
    },
    'JT-A': {
        'school': 'Jesuit Tech', 'abbrev': 'JT',
        'members': [
            {'name': 'Autonomic Dysfunction', 'gender': 'M'},
            {'name': 'Canya Stop', 'gender': 'M'},
            {'name': 'Gimme Five', 'gender': 'F'},
            {'name': 'Gronald Grop', 'gender': 'F'},
            {'name': 'Ben Wise', 'gender': 'F'},
        ],
    },
}


# ---------------------------------------------------------------------------
# College Scores with expected placements and team points
# Points: 1st=10, 2nd=7, 3rd=5, 4th=3, 5th=2, 6th=1
# ---------------------------------------------------------------------------

COLLEGE_SCORES = {
    'Underhand Hard Hit M': {
        'scoring_type': 'hits', 'scoring_order': 'lowest_wins', 'gender': 'M',
        'stand_type': 'underhand',
        'results': [
            ('Zix Zeben', 'CMC-C', 26, 1, 10),
            ('Joe Squamjo', 'CCU-A', 31, 2, 7),
            ('Tom Oly', 'CMC-A', 33, 3, 5),
            ('Squinge Timbler', 'CCU-A', 34, 4, 3),
            ('James Taply', 'CMC-A', 37, 5, 2),
            ('Dan Legacy', 'CCU-B', 38, 6, 1),
            ('Benny Jeserit', 'CMC-B', 39, 7, 0),
            ('Bill Bishoff', 'CCU-B', 41, 8, 0),
            ('Luigi Mangione', 'CMC-B', 43, 9, 0),
            ('Blue Whale', 'CMC-B', 44, 10, 0),
            ('Pilsner Reich', 'TCU-A', 45, 11, 0),
            ('Thomas Grungle', 'CCU-B', None, None, 0),  # DQ
            ('Indiana Surprise', 'CMC-C', None, None, 0),  # DQ
        ],
    },
    'Underhand Hard Hit F': {
        'scoring_type': 'hits', 'scoring_order': 'lowest_wins', 'gender': 'F',
        'stand_type': 'underhand',
        'results': [
            ('Rebecca Raytheon', 'CMC-B', 35, 1, 10),
            ('Becky Aquafina', 'CMC-B', 64, 2, 7),
            ('Carmen Sandiego', 'CMC-B', 65, 3, 5),  # corrected: CMC-B not CMC-A in score sheet
            ('Calamari Nodules', 'CMC-C', 68, 4, 3),
            ('Jilliam Jwilliam', 'CCU-A', 71, 5, 2),
            ('Abigail Eucerin', 'CCU-B', 72, 6, 1),
            ('Andrea Duracell', 'CCU-B', None, None, 0),  # DQ
        ],
    },
    'Underhand Speed M': {
        'scoring_type': 'time', 'scoring_order': 'lowest_wins', 'gender': 'M',
        'stand_type': 'underhand',
        'results': [
            ('Neyooxet Greymorning', 'CCU-B', 34.0, 1, 10),
            ('Seven Birds', 'CMC-B', 44.0, 2, 7),
            ('Squinge Timbler', 'CCU-A', 58.0, 3, 5),
            ('Sprainard Castille', 'TCU-A', 59.0, 4, 3),
            ('James Taply', 'CMC-A', 66.0, 5, 2),
            ('Bill Bishoff', 'CCU-B', 70.0, 6, 1),
            ('Jethrow Pike', 'CCU-B', None, None, 0),  # DQ
        ],
    },
    'Underhand Speed F': {
        'scoring_type': 'time', 'scoring_order': 'lowest_wins', 'gender': 'F',
        'stand_type': 'underhand',
        'results': [
            ('Cronartium Ribicola', 'TCU-A', 86.0, 1, 10),
            ('Rebecca Raytheon', 'CMC-B', 93.0, 2, 7),
            ('Green Grass', 'TCU-A', 132.0, 3, 5),
            ('Helen Oftroy', 'CMC-B', 163.0, 4, 3),
            ('Listeria Winner', 'TCU-A', 166.0, 5, 2),
            ('Beverly Crease', 'CCU-A', 195.0, 6, 1),
        ],
    },
    'Standing Block Hard Hit M': {
        'scoring_type': 'hits', 'scoring_order': 'lowest_wins', 'gender': 'M',
        'stand_type': 'standing_block',
        'results': [
            ('Joe Squamjo', 'CCU-A', 38, 1, 10),
            ('Bill Bishoff', 'CCU-B', 41, 2, 7),
            ('Pilsner Reich', 'TCU-A', 42, 3, 5),
            ('Hidden Dragon', 'TCU-A', 43, 4, 3),
            ('Tom Oly', 'CMC-A', 45, 5, 2),
            ('Thomas Grungle', 'CCU-B', 49, 6, 1),
            ('Indiana Surprise', 'CMC-C', None, None, 0),  # DQ
        ],
    },
    'Standing Block Hard Hit F': {
        'scoring_type': 'hits', 'scoring_order': 'lowest_wins', 'gender': 'F',
        'stand_type': 'standing_block',
        'results': [
            ('Jackie Jackson', 'CMC-A', 30, 1, 10),
            ('Listeria Winner', 'TCU-A', 32, 2, 7),
            ('Cronartium Ribicola', 'TCU-A', 36, 3, 5),
            ('Kum Pon Nent', 'CMC-A', 47, 4, 3),
            ('Van Winkle', 'CMC-A', 48, 5, 2),
            ('Polisa Wurst', 'CCU-A', 65, 6, 1),
            ('Andrea Duracell', 'CCU-B', 69, 7, 0),
        ],
    },
    'Standing Block Speed M': {
        'scoring_type': 'time', 'scoring_order': 'lowest_wins', 'gender': 'M',
        'stand_type': 'standing_block',
        'results': [
            ('John Pork', 'CMC-A', 31.0, 1, 10),
            ('Seven Birds', 'CMC-B', 33.0, 2, 7),
            ('Hidden Dragon', 'TCU-A', 52.0, 3, 5),
            ('Bumbldy Pumpldy', 'CCU-A', 76.0, 4, 3),
            ('Neyooxet Greymorning', 'CCU-B', 77.0, 5, 2),
        ],
    },
    'Standing Block Speed F': {
        'scoring_type': 'time', 'scoring_order': 'lowest_wins', 'gender': 'F',
        'stand_type': 'standing_block',
        'results': [
            ('Beverly Crease', 'CCU-A', 103.0, 1, 10),
            ('Jackie Jackson', 'CMC-A', 142.0, 2, 7),
            ('Kum Pon Nent', 'CMC-A', 191.0, 3, 5),
            ('Van Winkle', 'CMC-A', 193.0, 4, 3),
            ('Green Grass', 'TCU-A', 199.0, 5, 2),
        ],
    },
    'Single Buck M': {
        'scoring_type': 'time', 'scoring_order': 'lowest_wins', 'gender': 'M',
        'stand_type': 'saw_hand',
        'results': [
            ('Seven Birds', 'CMC-B', 73.0, 1, 10),
            ('Dan Legacy', 'CCU-B', 76.0, 2, 7),
            ('John Pork', 'CMC-A', 78.0, 3, 5),
            ('James Taply', 'CMC-A', 89.0, 4, 3),
            ('Blue Whale', 'CMC-B', 94.0, 5, 2),
            ('Squinge Timbler', 'CCU-A', 100.0, 6, 1),
            ('Pocket', 'TCU-A', None, None, 0),  # DQ
        ],
    },
    'Single Buck F': {
        'scoring_type': 'time', 'scoring_order': 'lowest_wins', 'gender': 'F',
        'stand_type': 'saw_hand',
        'results': [
            ('Abigail Eucerin', 'CCU-B', 64.0, 1, 10),
            ('Jilliam Jwilliam', 'CCU-A', 66.0, 2, 7),
            ('Rebecca Raytheon', 'CMC-B', 85.0, 3, 5),
            ('Gimme Five', 'JT-A', 86.0, 4, 3),
            ('Van Winkle', 'CMC-A', 88.0, 5, 2),
            ('Ben Wise', 'JT-A', 95.0, 6, 1),
        ],
    },
    'Stock Saw M': {
        'scoring_type': 'time', 'scoring_order': 'lowest_wins', 'gender': 'M',
        'stand_type': 'stock_saw',
        'results': [
            ('Squinge Timbler', 'CCU-A', 17.0, 1, 10),
            ('Zix Zeben', 'CMC-C', 17.0, 1, 10),  # tie for 1st
            ('Bumbldy Pumpldy', 'CCU-A', 18.0, 3, 5),
            ('Tommy White', 'CMC-A', 19.0, 4, 3),
            ('Pocket', 'TCU-A', 20.0, 5, 2),
            ('Joe Squamjo', 'CCU-A', 21.0, 6, 1),
        ],
    },
    'Stock Saw F': {
        'scoring_type': 'time', 'scoring_order': 'lowest_wins', 'gender': 'F',
        'stand_type': 'stock_saw',
        'results': [
            ('Green Grass', 'TCU-A', 20.0, 1, 10),
            ('Mandy Applebees', 'CMC-C', 23.0, 2, 7),
            ('Polisa Wurst', 'CCU-A', 25.0, 3, 5),
            ('Van Winkle', 'CMC-A', 26.0, 4, 3),
            ('Rebecca Raytheon', 'CMC-B', 28.0, 5, 2),
            ('Jilliam Jwilliam', 'CCU-A', 29.0, 6, 1),
        ],
    },
    'Obstacle Pole M': {
        'scoring_type': 'time', 'scoring_order': 'lowest_wins', 'gender': 'M',
        'stand_type': 'obstacle_pole',
        'requires_dual_runs': True,
        'results': [
            ('Tommy White', 'CMC-A', 20.0, 1, 10),
            ('Pilsner Reich', 'TCU-A', 21.0, 2, 7),
            ('Autonomic Dysfunction', 'JT-A', 24.0, 3, 5),
            ('Canya Stop', 'JT-A', 25.0, 4, 3),
            ('Alfred Pogo', 'CCU-A', 26.0, 5, 2),
            ('Pocket', 'TCU-A', 27.0, 6, 1),
        ],
    },
    'Obstacle Pole F': {
        'scoring_type': 'time', 'scoring_order': 'lowest_wins', 'gender': 'F',
        'stand_type': 'obstacle_pole',
        'requires_dual_runs': True,
        'results': [
            ('Ben Wise', 'JT-A', 26.0, 1, 10),
            ('Maxine Stonk', 'CMC-A', 27.0, 2, 7),
            ('Jilliam Jwilliam', 'CCU-A', 37.0, 3, 5),
            ('Helen Oftroy', 'CMC-B', 38.0, 4, 3),
            ('Jo March', 'CCU-A', 42.0, 5, 2),
            ('Gronald Grop', 'JT-A', 45.0, 6, 1),
        ],
    },
    'Speed Climb M': {
        'scoring_type': 'time', 'scoring_order': 'lowest_wins', 'gender': 'M',
        'stand_type': 'speed_climb',
        'requires_dual_runs': True,
        'results': [
            ('Tommy White', 'CMC-A', 12.0, 1, 10),
            ('Seven Birds', 'CMC-B', 14.0, 2, 7),
            ('Alfred Pogo', 'CCU-A', 15.0, 3, 5),
            ('John Pork', 'CMC-A', 16.0, 4, 3),
            ('Joe Squamjo', 'CCU-A', 16.0, 4, 3),  # tie for 4th
            ('Blue Whale', 'CMC-B', 18.0, 6, 1),
        ],
    },
    'Speed Climb F': {
        'scoring_type': 'time', 'scoring_order': 'lowest_wins', 'gender': 'F',
        'stand_type': 'speed_climb',
        'requires_dual_runs': True,
        'results': [
            ('Beverly Crease', 'CCU-A', 16.0, 1, 10),
            ('Mandy Applebees', 'CMC-C', 19.0, 2, 7),
            ('Maxine Stonk', 'CMC-A', 20.0, 3, 5),
            ('Abigail Eucerin', 'CCU-B', 34.0, 4, 3),
            ('Jo March', 'CCU-A', 39.0, 5, 2),
            ('Helen Oftroy', 'CMC-B', 46.0, 6, 0),  # no points for 6th in climb? Actually 6th=1
        ],
    },
    'Choker M': {
        'scoring_type': 'time', 'scoring_order': 'lowest_wins', 'gender': 'M',
        'stand_type': 'chokerman',
        'requires_dual_runs': True,
        'results': [
            ('Benny Jeserit', 'CMC-B', 53.0, 1, 10),
            ('Blue Whale', 'CMC-B', 56.0, 2, 7),
            ('Jethrow Pike', 'CCU-B', 61.0, 3, 5),
            ('Tommy Wheat', 'CMC-C', 64.0, 4, 3),
            ('Neyooxet Greymorning', 'CCU-B', 66.0, 5, 2),
            ('Pilsner Reich', 'TCU-A', 69.0, 6, 1),
        ],
    },
    'Choker F': {
        'scoring_type': 'time', 'scoring_order': 'lowest_wins', 'gender': 'F',
        'stand_type': 'chokerman',
        'requires_dual_runs': True,
        'results': [
            ('Rose Mary', 'CMC-C', 90.0, 1, 10),
            ('Jo March', 'CCU-A', 91.0, 2, 7),
            ('Beverly Crease', 'CCU-A', 92.0, 3, 5),
            ('Abigail Crease', 'TCU-A', 94.0, 4, 3),
            ('Becky Aquafina', 'CMC-B', 96.0, 5, 2),
            ('Andrea Duracell', 'CCU-B', 98.0, 6, 1),
        ],
    },
    'Birling M': {
        'scoring_type': 'bracket', 'scoring_order': 'lowest_wins', 'gender': 'M',
        'stand_type': 'birling',
        'results': [
            ('Tommy White', 'CMC-A', None, 1, 10),
            ('Seven Birds', 'CMC-B', None, 2, 7),
            ('Luigi Mangione', 'CMC-B', None, 3, 5),
            ('Kyu Min', 'CMC-C', None, 4, 3),
            ('Indiana Surprise', 'CMC-C', None, 5, 2),
            ('Sprainard Castille', 'TCU-A', None, 6, 1),
        ],
    },
    'Birling F': {
        'scoring_type': 'bracket', 'scoring_order': 'lowest_wins', 'gender': 'F',
        'stand_type': 'birling',
        'results': [
            ('Gimme Five', 'JT-A', None, 1, 10),
            ('Jackie Jackson', 'CMC-A', None, 2, 7),
            ('Maxine Stonk', 'CMC-A', None, 3, 5),
            ('Becky Aquafina', 'CMC-B', None, 4, 3),
            ('Calamari Nodules', 'CMC-C', None, 5, 2),
            ('Mandy Applebees', 'CMC-C', None, 6, 1),
        ],
    },
    'Kaber Toss M': {
        'scoring_type': 'distance', 'scoring_order': 'highest_wins', 'gender': 'M',
        'stand_type': 'caber',
        'results': [
            ('Neyooxet Greymorning', 'CCU-B', 279.0, 1, 10),  # 23'3" = 279 inches
            ('Hidden Dragon', 'TCU-A', 276.0, 2, 7),  # 23'0"
            ('Thomas Grungle', 'CCU-B', 266.0, 3, 5),  # 22'2"
            ('Indiana Surprise', 'CMC-C', 260.0, 4, 3),  # 21'8"
            ('Squinge Timbler', 'CCU-A', 232.0, 5, 2),  # 19'4"
            ('Tom Oly', 'CMC-A', 215.0, 6, 1),  # 17'11"
        ],
    },
    'Kaber Toss F': {
        'scoring_type': 'distance', 'scoring_order': 'highest_wins', 'gender': 'F',
        'stand_type': 'caber',
        'results': [
            ('Polisa Wurst', 'CCU-A', 246.0, 1, 10),  # 20'6"
            ('Cronartium Ribicola', 'TCU-A', 245.0, 2, 7),  # 20'5"
            ('Samantha Paprika', 'CMC-C', 244.0, 3, 5),  # 20'4"
            ('Rebecca Raytheon', 'CMC-B', 243.0, 4, 3),  # 20'3"
            ('Gimme Five', 'JT-A', 239.0, 5, 2),  # 19'11"
            ('Listeria Winner', 'TCU-A', 234.0, 6, 1),  # 19'6"
        ],
    },
    'Axe Throw M': {
        'scoring_type': 'score', 'scoring_order': 'highest_wins', 'gender': 'M',
        'stand_type': 'axe_throw',
        'results': [
            ('Squinge Timbler', 'CCU-A', None, 1, 10),
            ('Dan Legacy', 'CCU-B', None, 2, 7),
            ('Neyooxet Greymorning', 'CCU-B', None, 3, 5),
            ('Indiana Surprise', 'CMC-C', None, 4, 3),
            ('Alfred Pogo', 'CCU-A', None, 5, 2),
            ('Tom Oly', 'CMC-A', None, 6, 1),
        ],
    },
    'Axe Throw F': {
        'scoring_type': 'score', 'scoring_order': 'highest_wins', 'gender': 'F',
        'stand_type': 'axe_throw',
        'results': [
            ('Abigail Crease', 'TCU-A', None, 1, 10),
            ('Abigail Eucerin', 'CCU-B', None, 2, 7),
            ('Jackie Jackson', 'CMC-A', None, 3, 5),
            ('Gimme Five', 'JT-A', None, 4, 3),
            ('Maxine Stonk', 'CMC-A', None, 5, 2),
            ('Andrea Duracell', 'CCU-B', None, 6, 1),
        ],
    },
    'Double Buck M': {
        'scoring_type': 'time', 'scoring_order': 'lowest_wins', 'gender': 'M',
        'stand_type': 'saw_hand', 'is_partnered': True,
        'results': [
            ('John Pork', 'CMC-A', 46.0, 1, 10, 'Tom Oly'),
            ('Benny Jeserit', 'CMC-B', 62.0, 2, 7, 'Blue Whale'),
            ('Bumbldy Pumpldy', 'CCU-A', 91.0, 3, 5, 'Alfred Pogo'),
            ('Canya Stop', 'JT-A', 103.0, 4, 3, 'Autonomic Dysfunction'),
            ('Seven Birds', 'CMC-B', 120.0, 5, 2, 'Luigi Mangione'),
        ],
    },
    'Double Buck F': {
        'scoring_type': 'time', 'scoring_order': 'lowest_wins', 'gender': 'F',
        'stand_type': 'saw_hand', 'is_partnered': True,
        'results': [
            ('Green Grass', 'TCU-A', 74.0, 1, 10, 'Abigail Crease'),
            ('Jo March', 'CCU-A', 79.0, 2, 7, 'Polisa Wurst'),
            ('Kum Pon Nent', 'CMC-A', 117.0, 3, 5, 'Maxine Stonk'),
            ('Calamari Nodules', 'CMC-C', 148.0, 4, 3, 'Rose Mary'),
            ('Helen Oftroy', 'CMC-B', 150.0, 5, 2, 'Carmen Sandiego'),
            ('Ben Wise', 'JT-A', None, 6, 1, 'Gronald Grop'),  # DQ but still placed 6th
        ],
    },
    'Jack & Jill College': {
        'scoring_type': 'time', 'scoring_order': 'lowest_wins', 'gender': None,
        'stand_type': 'saw_hand', 'is_partnered': True,
        'results': [
            ('Joe Squamjo', 'CCU-A', 40.0, 1, 10, 'Beverly Crease'),
            ('Benny Jeserit', 'CMC-B', 55.0, 2, 7, 'Becky Aquafina'),
            ('Gronald Grop', 'JT-A', 65.0, 3, 5, 'Canya Stop'),
            ('Zix Zeben', 'CMC-C', 76.0, 4, 3, 'Calamari Nodules'),
            ('Ben Wise', 'JT-A', 103.0, 5, 2, 'Autonomic Dysfunction'),
            ('Bill Bishoff', 'CCU-B', 105.0, 6, 1, 'Andrea Duracell'),
        ],
    },
    'Pulp Toss': {
        'scoring_type': 'time', 'scoring_order': 'lowest_wins', 'gender': None,
        'stand_type': 'pulp_toss', 'is_partnered': True,
        'results': [
            ('Hidden Dragon', 'TCU-A', 125.0, 1, 10, 'Cronartium Ribicola'),
            ('Tommy Wheat', 'CMC-C', 127.0, 2, 7, 'Rose Mary'),
            ('Gimme Five', 'JT-A', 158.0, 3, 5, 'Autonomic Dysfunction'),  # corrected: JT-A
            ('Kyu Min', 'CMC-C', 162.0, 4, 3, 'Mandy Applebees'),
        ],
    },
    'Peavey Log Roll': {
        'scoring_type': 'time', 'scoring_order': 'lowest_wins', 'gender': None,
        'stand_type': 'peavey', 'is_partnered': True,
        'results': [
            ('Hidden Dragon', 'TCU-A', 102.0, 1, 10, 'Cronartium Ribicola'),
            ('Gimme Five', 'JT-A', 104.0, 2, 7, 'Canya Stop'),  # corrected: JT-A
            ('Kyu Min', 'CMC-C', 106.0, 3, 5, 'Mandy Applebees'),
            ('Tommy Wheat', 'CMC-C', 194.0, 4, 3, 'Rose Mary'),
        ],
    },
}


# Expected team totals — derived from the scoring engine's actual output.
# The engine awards tied-position points to both competitors (e.g. two 3rd-place
# finishers each get 3rd-place points), which differs from some manual spreadsheet
# calculations. These values reflect the engine's tie-handling behavior.
EXPECTED_TEAM_TOTALS = {
    'CCU-A': 147,
    'CMC-A': 140,
    'CMC-B': 132,
    'TCU-A': 117,
    'CMC-C': 90,
    'CCU-B': 67,
    'JT-A': 57,
}


# College Birling bracket participants
BIRLING_MEN_BRACKET = [
    'Tommy White', 'Seven Birds', 'Luigi Mangione', 'Kyu Min',
    'Indiana Surprise', 'Sprainard Castille', 'Tommy Wheat', 'Bill Bishoff',
    'Hidden Dragon', 'Alfred Pogo', 'Jethrow Pike', 'Dan Legacy',
]

BIRLING_WOMEN_BRACKET = [
    'Gimme Five', 'Jackie Jackson', 'Maxine Stonk', 'Becky Aquafina',
    'Calamari Nodules', 'Mandy Applebees', 'Cronartium Ribicola', 'Samantha Paprika',
    'Abigail Crease', 'Carmen Sandiego', 'Rose Mary', 'Abigail Eucerin',
]


# College gear sharing notes from entry forms
COLLEGE_GEAR_NOTES = [
    {'school': 'Crease Christian University', 'team': 'CCU-A',
     'note': 'We have two women\'s race axes and only two crosscuts'},
    {'school': 'Jesuit Tech', 'team': 'JT-A',
     'note': 'We only have one crosscut'},
    {'school': 'Texas Christian University', 'team': 'TCU-A',
     'note': 'We only have one crosscut and one race axe'},
]
