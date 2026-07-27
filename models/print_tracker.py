"""PrintTracker model for the Print Hub staleness indicator.

One row per (tournament, doc_key, entity_id) tuple. `entity_id` is nullable —
fixed docs (heat sheets, all-results, etc.) store NULL; dynamic docs (per-event
results) store the entity primary key (event_id).

Fingerprint-based staleness: each print route computes a short sha1 of the
data underlying the print; if the fingerprint changes between the last print
and the next Hub page load, the row is flagged STALE.
"""

from datetime import datetime

from database import db


class PrintTracker(db.Model):
    """Records the last time a print route ran + the data fingerprint then."""

    __tablename__ = "print_trackers"

    id = db.Column(db.Integer, primary_key=True)
    tournament_id = db.Column(
        db.Integer,
        db.ForeignKey("tournaments.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    doc_key = db.Column(db.String(64), nullable=False)
    # Nullable — fixed docs store NULL, dynamic docs store entity PK.
    # NOT a foreign key: different dynamic docs reference different tables
    # (events today; could be heats, competitors later) and a real FK would
    # force a discriminator column. Decorator is responsible for writing only
    # ids that exist; orphans are acceptable historical bookkeeping.
    entity_id = db.Column(db.Integer, nullable=True)

    last_printed_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    last_printed_fingerprint = db.Column(db.String(64), nullable=False)
    last_printed_by_user_id = db.Column(
        db.Integer,
        db.ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )

    __table_args__ = (
        db.UniqueConstraint(
            "tournament_id",
            "doc_key",
            "entity_id",
            name="uq_print_tracker_tournament_doc_entity",
        ),
    )

    def __repr__(self):
        ent = f" entity={self.entity_id}" if self.entity_id is not None else ""
        return f"<PrintTracker t={self.tournament_id} {self.doc_key}{ent} @{self.last_printed_at}>"
