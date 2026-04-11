"""
Event and EventResult models for tournament events.
"""
import json

import sqlalchemy as sa

from database import db


class Event(db.Model):
    """Represents a competition event (e.g., Men's Underhand Speed)."""

    __tablename__ = 'events'
    __table_args__ = (
        db.Index('ix_events_tournament_type_status', 'tournament_id', 'event_type', 'status'),
    )

    id = db.Column(db.Integer, primary_key=True)
    tournament_id = db.Column(db.Integer, db.ForeignKey('tournaments.id'), nullable=False)

    # Event identification
    name = db.Column(db.String(200), nullable=False)
    event_type = db.Column(db.String(20), nullable=False)  # 'college' or 'pro'
    gender = db.Column(db.String(10), nullable=True)  # 'M', 'F', or None for mixed

    # Scoring configuration
    scoring_type = db.Column(db.String(20), nullable=False)  # 'time', 'score', 'distance', 'hits', 'bracket'
    scoring_order = db.Column(db.String(20), nullable=False, default='lowest_wins')  # 'lowest_wins' or 'highest_wins'

    # Event classification (college only)
    is_open = db.Column(db.Boolean, nullable=False, default=False)  # True = OPEN event, False = CLOSED

    # Competition format (underhand, standing block, springboard only)
    is_handicap = db.Column(db.Boolean, nullable=False, default=False)  # False = Championship, True = Handicap

    # Event characteristics
    is_partnered = db.Column(db.Boolean, nullable=False, default=False)
    partner_gender_requirement = db.Column(db.String(10), nullable=True)  # 'same', 'mixed', 'any'

    # Run configuration
    # requires_dual_runs: two separate heats (run 1 & run 2); best run counts.
    #   Used by: Speed Climb, Chokerman's Race, Caber Toss.
    requires_dual_runs = db.Column(db.Boolean, nullable=False, default=False)
    # requires_triple_runs: three throw inputs in a single heat; sum counts.
    #   Used by: Axe Throw, Partnered Axe Throw. Tie detected → throw-off required.
    requires_triple_runs = db.Column(db.Boolean, nullable=False, default=False)

    # Stand configuration
    stand_type = db.Column(db.String(50), nullable=True)
    max_stands = db.Column(db.Integer, nullable=True)

    # Pro event specifics
    has_prelims = db.Column(db.Boolean, nullable=False, default=False)  # True for Partnered Axe Throw

    # Payout configuration (pro only) - stored as JSON
    payouts = db.Column(db.Text, nullable=False, default='{}')  # Dict: position -> amount

    # Status
    status = db.Column(db.String(20), nullable=False, default='pending')  # pending, in_progress, completed

    # Explicit finalization lock — set True after _calculate_positions() succeeds.
    # Editing a result on a finalized event resets this to False, requiring re-finalization.
    is_finalized = db.Column(db.Boolean, nullable=False, default=False)

    # Relationships
    heats = db.relationship('Heat', backref='event', lazy='dynamic', cascade='all, delete-orphan')
    results = db.relationship('EventResult', backref='event', lazy='dynamic', cascade='all, delete-orphan')

    def __repr__(self):
        gender_str = f" ({self.gender})" if self.gender else ""
        return f'<Event {self.name}{gender_str}>'

    @property
    def display_name(self):
        """Return event name with gender prefix if applicable."""
        if self.gender == 'M':
            return f"Men's {self.name}"
        elif self.gender == 'F':
            return f"Women's {self.name}"
        return self.name

    @property
    def is_hard_hit(self):
        """True for Hard-Hit events where ties break on time."""
        import config
        return self.name in config.HARD_HIT_EVENTS

    @property
    def is_axe_throw_cumulative(self):
        """True for axe throw events using 3-throw cumulative scoring."""
        import config
        return self.name in config.AXE_THROW_CUMULATIVE_EVENTS

    @property
    def uses_payouts_for_state(self):
        """True when the payouts column stores state-machine data instead of
        payout amounts (Pro-Am Relay, Partnered Axe Throw, Birling bracket)."""
        return (self.has_prelims
                or self.scoring_type == 'bracket'
                or self.name == 'Pro-Am Relay')

    def get_payouts(self):
        """Return dict of position -> payout amount."""
        try:
            return json.loads(self.payouts or '{}')
        except json.JSONDecodeError:
            return {}

    def set_payouts(self, payout_dict):
        """Set the payout structure."""
        self.payouts = json.dumps(payout_dict)

    def get_payout_for_position(self, position):
        """Get payout amount for a specific position."""
        payouts = self.get_payouts()
        return payouts.get(str(position), 0)

    def get_competitors(self):
        """Return list of competitors entered in this event."""
        return [r.competitor_name for r in self.results.all()]

    def get_results_sorted(self):
        """Return results sorted by final position."""
        return self.results.order_by(EventResult.final_position).all()


class EventResult(db.Model):
    """Represents a competitor's result in an event."""

    __tablename__ = 'event_results'
    __table_args__ = (
        db.UniqueConstraint('event_id', 'competitor_id', 'competitor_type', name='uq_event_result_competitor'),
        db.Index('ix_event_results_event_status', 'event_id', 'status'),
    )

    id = db.Column(db.Integer, primary_key=True)
    event_id = db.Column(db.Integer, db.ForeignKey('events.id'), nullable=False)

    # Competitor info (flexible - works for both college and pro)
    competitor_id = db.Column(db.Integer, nullable=False)
    competitor_type = db.Column(db.String(20), nullable=False)  # 'college' or 'pro'
    competitor_name = db.Column(db.String(200), nullable=False)

    # For partnered events
    partner_name = db.Column(db.String(200), nullable=True)

    # Results - flexible storage
    result_value = db.Column(db.Float, nullable=True)  # Ranked metric: best run, cumulative score, or raw value
    result_unit = db.Column(db.String(20), nullable=True)  # 'seconds', 'points', 'feet', 'hits'

    # Dual-run events (Speed Climb, Chokerman, Caber Toss)
    run1_value = db.Column(db.Float, nullable=True)
    run2_value = db.Column(db.Float, nullable=True)
    best_run = db.Column(db.Float, nullable=True)  # The better of two runs per scoring_order

    # Triple-run events (Axe Throw, Partnered Axe Throw) — stored individually, result_value = sum
    run3_value = db.Column(db.Float, nullable=True)

    # Dual-judge timer readings (Phase 1 of scoring fix — V2.8.0).
    # Every timed event in this codebase (college and pro, single-run AND dual-run)
    # has TWO judge stopwatches on each physical run.  The two readings are averaged
    # into the run's "scored time" (run1_value / run2_value / result_value) — see
    # _save_heat_results_submission() in routes/scoring.py.
    #
    # Single-run events (e.g., Pro Underhand, College Single Buck):
    #   t1_run1, t2_run1 → averaged into result_value (and run1_value)
    #   t1_run2, t2_run2 stay NULL
    #
    # Dual-run events (Speed Climb, Chokerman's Race, Caber Toss):
    #   t1_run1, t2_run1 → averaged into run1_value
    #   t1_run2, t2_run2 → averaged into run2_value
    #   best_run = min(run1_value, run2_value) for lowest_wins, max for highest_wins
    #
    # All four columns are NULL until the heat is scored.  Numeric(8, 2) gives us
    # 6 integer digits + 2 decimal places — supports times up to 999999.99 seconds
    # with hundredth-of-a-second precision (the resolution of typical race timers).
    t1_run1 = db.Column(db.Numeric(8, 2), nullable=True)
    t2_run1 = db.Column(db.Numeric(8, 2), nullable=True)
    t1_run2 = db.Column(db.Numeric(8, 2), nullable=True)
    t2_run2 = db.Column(db.Numeric(8, 2), nullable=True)

    # Secondary tiebreak metric (Hard-Hit events only).
    # Stores elapsed time in seconds; lowest time wins the tiebreak when hit counts are equal.
    tiebreak_value = db.Column(db.Float, nullable=True)

    # Throw-off flag — set True when the system detects a cumulative-score tie on axe throw.
    # Judge must record throw-off positions before is_finalized can be set True.
    throwoff_pending = db.Column(db.Boolean, nullable=False, default=False)

    # STRATHMARK handicap start mark in seconds.
    # _metric() in scoring_engine subtracts this from raw time when event.is_handicap is True
    # and scoring_type == 'time'.  Default 0.0 means scratch (no start mark).
    # Populated by services/mark_assignment.py → assign_handicap_marks().
    handicap_factor = db.Column(db.Float, nullable=False, default=0.0)

    # STRATHMARK predicted completion time in seconds — the raw time HandicapCalculator
    # expected this competitor to post.  Stored here so that after the event runs,
    # _record_prediction_residuals_for_pro_event() can compare predicted vs actual and push
    # the residual to the STRATHMARK Supabase bias-learning table.
    # Populated by mark_assignment.assign_handicap_marks() when a MarkResult is available.
    # NULL means no prediction was recorded (mark assignment not run, or competitor scratched).
    predicted_time = db.Column(db.Float, nullable=True, default=None)

    # Placement
    final_position = db.Column(db.Integer, nullable=True)  # 1st, 2nd, 3rd, etc.

    # Points awarded (college only).
    # Changed Integer → Numeric(6, 2) in V2.8.0 (Phase 1B of scoring fix) so that
    # split-tie placements can produce fractional point values (e.g., two competitors
    # tied for 5th each receive (2 + 1) / 2 = 1.5 points per the AWFC tie-split rule).
    # Numeric(6, 2) supports values up to 9999.99 — far above the per-event maximum
    # of 10.00 even after a many-way tie split.
    points_awarded = db.Column(
        db.Numeric(6, 2),
        nullable=False,
        default=0,
        server_default=sa.text("'0.00'"),
    )

    # Payout (pro only)
    payout_amount = db.Column(db.Float, nullable=False, default=0.0)

    # Score discrepancy flag
    is_flagged = db.Column(db.Boolean, nullable=False, default=False)

    # Status — allowed values: pending, completed, scratched, dnf, dq, partial
    # 'dq' (disqualified) was added alongside 'status_reason' so judges can record
    # the ground for a disqualification (illegal axe, stepped out of stand, etc.).
    # 'status_reason' also applies to 'scratched' and 'dnf' — it's a free-text
    # explanation for any non-completion state.
    status = db.Column(db.String(20), nullable=False, default='pending')
    status_reason = db.Column(db.Text, nullable=True)
    version_id = db.Column(db.Integer, nullable=False, default=1)

    __mapper_args__ = {
        'version_id_col': version_id,
    }

    def __repr__(self):
        return f'<EventResult {self.competitor_name} - {self.result_value}>'

    def calculate_best_run(self, scoring_order='lowest_wins'):
        """
        For dual-run events: store the run that counts based on scoring_order.
          lowest_wins  → min(run1, run2)   e.g. Speed Climb, Chokerman
          highest_wins → max(run1, run2)   e.g. Caber Toss (distance)
        Also updates result_value so the ranking sort always uses best_run.
        """
        runs = [v for v in [self.run1_value, self.run2_value] if v is not None]
        if not runs:
            return self.best_run
        if scoring_order == 'highest_wins':
            self.best_run = max(runs)
        else:
            self.best_run = min(runs)
        self.result_value = self.best_run
        return self.best_run

    def calculate_cumulative_score(self):
        """
        For triple-run events (Axe Throw): result_value = sum of all throws entered so far.
        Missing throws (not yet entered) are treated as 0 and NOT counted so partial sums
        update live — the final sum is only authoritative once all three are entered.
        """
        values = [v for v in [self.run1_value, self.run2_value, self.run3_value] if v is not None]
        self.result_value = sum(values) if values else None
        return self.result_value
