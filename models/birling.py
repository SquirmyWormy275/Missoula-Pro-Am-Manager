"""Birling double-elimination bracket, as rows.

D13-C commit A1. Until this revision a birling bracket lived entirely inside
``events.payouts``, a Text column named for something else, holding a JSON
document of about five and a half kilobytes: an entrant list, a seed order, a
nested list-of-lists of match dicts for each side of the bracket, two singleton
match dicts for the finals, a list of falls inside every match, and a dict of
final placements keyed by a stringified competitor id.

Every competitor reference in that document was a bare integer. The pro and
college id sequences overlap, so a bare integer does not name a human, and the
production mirror proves the cost: both stored brackets carry references to
college competitors by their pre-reseed ids, which now resolve to entirely
different people on the pro side. Ten distinct ids across the two events, 55
findings, every one of them a person the blob names correctly and identifies
wrongly. That is the failure mode a foreign key exists to make impossible.

These five tables are the Birling bracket with real references. The bracket
service reads and writes them as the sole persisted state. That is the shape
D12-C used for heat rosters and it is used again here for the same reason: a
bracket is live judge state on race day and there is no sitting in which it can
be unavailable.

Why there is no parent ``birling_brackets`` table
=================================================
The obvious shape gives each of these tables a ``bracket_id`` onto a parent row
holding ``event_id`` unique and a creation timestamp. That parent carries no
information its children do not already imply: a bracket exists exactly when
the event has seed rows, and nothing in the tree reads a bracket's creation
time. It would also collide by name with ``services.birling_bracket
.BirlingBracket``, the service class, which A2 has to import models into.

So every table here hangs off ``event_id`` directly, which is also what the
uniqueness of the parent's ``event_id`` was going to say anyway. ``birling_falls``
is the one exception and hangs off its match, because a fall belongs to a match
and not to an event.

What these tables deliberately do not carry
===========================================
**The legacy ``(competitor_id, competitor_type)`` pair.** ``heat_assignments``
kept it because a dozen readers already spoke that language and one commit
could not rewrite them all. Here there is no reader at all yet, so a mirror
column would be built in A1 purely to be deleted in A4. A caller that needs the
bare id joins ``college_competitors`` or ``pro_competitors`` on the uid, which
is the direction this program has been moving everything anyway.

**``current_round``.** Written once by ``BirlingBracket._load_bracket_data``
and read by nothing in the tree. It has said ``'winners_1'`` on both production
brackets since they were generated, through four decided rounds.

**``eliminated_position``.** Present on every losers bracket match dict and
never assigned by anything. ``services/birling_print.py`` sets it to None
explicitly. It is a slot someone meant to use.

**``round``.** The string ``'winners_2'`` is ``side`` and ``round_index``
spelled differently, and the service already parses it back apart from
``match_id`` in nine places.

**The cached competitor name.** ``competitors[].name`` held a display name at
the moment the bracket was generated. A join gives the current one.
"""
from database import db

from ._types import BIG_ID

#: The four places a match can sit. ``winners`` and ``losers`` are the two
#: sides of a double-elimination draw and carry a real ``round_index``;
#: ``finals`` and ``true_finals`` are singletons and carry round_index 0.
MATCH_SIDES = ('winners', 'losers', 'finals', 'true_finals')


class BirlingSeed(db.Model):
    """One entrant, at one seed position.

    This is ``competitors[]`` and ``seeding[]`` collapsed into one thing, which
    they always were: on both production brackets the entrant list is the seed
    order, element for element. Keeping them apart in the blob meant they could
    disagree and nothing would notice.

    ``seed_number`` is 1-based and is the position in ``seeding``, which is the
    order ``generate_bracket`` pairs from, which is the order the judge sheet
    prints.

    The presence of any row here is what it means for an event to have a
    bracket.
    """

    __tablename__ = 'birling_seeds'
    __table_args__ = (
        db.UniqueConstraint('event_id', 'seed_number',
                            name='uq_birling_seeds_event_seed'),
        db.UniqueConstraint('event_id', 'uid',
                            name='uq_birling_seeds_event_uid'),
        db.CheckConstraint('seed_number >= 1',
                           name='ck_birling_seeds_seed_positive'),
    )

    id = db.Column(db.Integer, primary_key=True)
    event_id = db.Column(db.Integer, db.ForeignKey('events.id'),
                         nullable=False)
    seed_number = db.Column(db.Integer, nullable=False)
    uid = db.Column(BIG_ID, db.ForeignKey('competitors.uid'), nullable=False)

    def __repr__(self):
        return f'<BirlingSeed {self.seed_number} uid={self.uid}>'


class BirlingPreSeed(db.Model):
    """An intended seed for a competitor, set before any bracket exists.

    This is ``payouts['pre_seedings']``, written by the ability rankings page
    from each school's stated running order and read by
    ``routes/scheduling/birling.py`` as the default seed order when the
    generate form carries no manual seeds.

    It is a separate table from ``birling_seeds`` and not a nullable column on
    it, which is the one structural fact the blob obscured by storing both in
    the same document: a pre-seeding is an input to bracket generation and is
    routinely present when no bracket has been generated at all.
    """

    __tablename__ = 'birling_pre_seeds'
    __table_args__ = (
        db.UniqueConstraint('event_id', 'uid',
                            name='uq_birling_pre_seeds_event_uid'),
        db.UniqueConstraint('event_id', 'seed_number',
                            name='uq_birling_pre_seeds_event_seed'),
        db.CheckConstraint('seed_number >= 1',
                           name='ck_birling_pre_seeds_seed_positive'),
    )

    id = db.Column(db.Integer, primary_key=True)
    event_id = db.Column(db.Integer, db.ForeignKey('events.id'),
                         nullable=False)
    uid = db.Column(BIG_ID, db.ForeignKey('competitors.uid'), nullable=False)
    seed_number = db.Column(db.Integer, nullable=False)

    def __repr__(self):
        return f'<BirlingPreSeed event={self.event_id} seed={self.seed_number}>'


class BirlingMatch(db.Model):
    """One match slot.

    ``match_id`` is kept as a stored natural key rather than derived, because
    it is what the service, the routes, the templates and the print catalog all
    already speak, and because a stored key means a match keeps its name if the
    derivation rule ever changes. ``side``, ``round_index`` and ``position``
    are the same name parsed apart, stored so the bracket can be read in order
    without string surgery in SQL.

    All four competitor columns are nullable because a match slot legitimately
    holds nothing: a later round's slots are empty until the round that feeds
    them is decided, a bye has no ``competitor2``, and an undecided match has
    no winner or loser.

    ``needed`` is meaningful only on the true finals, where it records whether
    the losers bracket champion beat the winners bracket champion in the grand
    finals and therefore earned a second match. It is null everywhere else
    rather than false, because false would claim the question was asked.
    """

    __tablename__ = 'birling_matches'
    __table_args__ = (
        db.UniqueConstraint('event_id', 'match_id',
                            name='uq_birling_matches_event_match'),
        db.UniqueConstraint('event_id', 'side', 'round_index', 'position',
                            name='uq_birling_matches_event_slot'),
        db.CheckConstraint(
            "side IN ('winners', 'losers', 'finals', 'true_finals')",
            name='ck_birling_matches_side_valid'),
        db.CheckConstraint('round_index >= 0',
                           name='ck_birling_matches_round_nonneg'),
        db.CheckConstraint('position >= 1',
                           name='ck_birling_matches_position_positive'),
    )

    id = db.Column(db.Integer, primary_key=True)
    event_id = db.Column(db.Integer, db.ForeignKey('events.id'),
                         nullable=False)
    match_id = db.Column(db.String(20), nullable=False)
    side = db.Column(db.String(16), nullable=False)
    round_index = db.Column(db.Integer, nullable=False)
    position = db.Column(db.Integer, nullable=False)

    competitor1_uid = db.Column(BIG_ID, db.ForeignKey('competitors.uid'),
                                nullable=True)
    competitor2_uid = db.Column(BIG_ID, db.ForeignKey('competitors.uid'),
                                nullable=True)
    winner_uid = db.Column(BIG_ID, db.ForeignKey('competitors.uid'),
                           nullable=True)
    loser_uid = db.Column(BIG_ID, db.ForeignKey('competitors.uid'),
                          nullable=True)

    is_bye = db.Column(db.Boolean, nullable=False, default=False)
    needed = db.Column(db.Boolean, nullable=True)

    falls = db.relationship('BirlingFall', back_populates='match',
                            cascade='all, delete-orphan',
                            order_by='BirlingFall.fall_number')

    def __repr__(self):
        return f'<BirlingMatch {self.match_id} event={self.event_id}>'


class BirlingFall(db.Model):
    """One fall inside a best-of-three birling match.

    A birling match is decided by the first competitor to win two falls, so a
    match carries one, two or three of these. ``record_match_result`` called
    without any falls having been recorded writes two synthetic ones for the
    winner so the match reads consistently, which is why a fall row is not by
    itself evidence a judge watched one.
    """

    __tablename__ = 'birling_falls'
    __table_args__ = (
        db.UniqueConstraint('match_row_id', 'fall_number',
                            name='uq_birling_falls_match_number'),
        db.CheckConstraint('fall_number >= 1 AND fall_number <= 3',
                           name='ck_birling_falls_number_range'),
    )

    id = db.Column(db.Integer, primary_key=True)
    match_row_id = db.Column(db.Integer, db.ForeignKey('birling_matches.id'),
                             nullable=False)
    fall_number = db.Column(db.Integer, nullable=False)
    winner_uid = db.Column(BIG_ID, db.ForeignKey('competitors.uid'),
                           nullable=False)
    recorded_at = db.Column(db.DateTime, nullable=True)

    match = db.relationship('BirlingMatch', back_populates='falls')

    def __repr__(self):
        return f'<BirlingFall {self.fall_number} match={self.match_row_id}>'


class BirlingPlacement(db.Model):
    """A competitor's final position in the bracket.

    Written when a competitor is eliminated, from the back of the field
    forward, and again by the finals for positions 1 and 2. Position is not
    unique within an event: the grand finals write 1 and 2 while the losers
    bracket has already written the same numbers downward from the field size,
    and reconciling that is the service's business, not the schema's.
    """

    __tablename__ = 'birling_placements'
    __table_args__ = (
        db.UniqueConstraint('event_id', 'uid',
                            name='uq_birling_placements_event_uid'),
        db.CheckConstraint('position >= 1',
                           name='ck_birling_placements_position_positive'),
    )

    id = db.Column(db.Integer, primary_key=True)
    event_id = db.Column(db.Integer, db.ForeignKey('events.id'),
                         nullable=False)
    uid = db.Column(BIG_ID, db.ForeignKey('competitors.uid'), nullable=False)
    position = db.Column(db.Integer, nullable=False)

    def __repr__(self):
        return f'<BirlingPlacement uid={self.uid} position={self.position}>'
