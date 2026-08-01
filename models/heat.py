"""
Heat and Flight models for scheduling competition runs.
"""
import json
from datetime import datetime, timezone

import sqlalchemy as sa

from config import HEAT_LOCK_TTL_SECONDS  # noqa: F401 — single source in config.py
from database import db

# services/entity_key.py imports nothing but dataclasses, so a model importing
# it does not create the models-import-services cycle that would normally make
# this the wrong direction. It is here rather than inside the function because
# a lazy import would hide that dependency from anyone reading the header.
from services.entity_key import EntityKey, resolve_uids

from ._types import BIG_ID


class BadHeatAssignment(ValueError):
    """A heat's competitors JSON cannot be turned into assignment rows.

    Raised by ``Heat.set_roster`` before it writes anything, in the three
    cases the database would otherwise reject with a bare driver error: an id
    that is not usable as a reference at all, an id that names no competitor of
    that kind, and the same competitor listed twice in one heat.

    It is a ``ValueError`` so a caller that has never heard of it still sees a
    programming error rather than a database error, and it names the heat and
    the offending ids so an operator can find the heat without reading a
    traceback.
    """


class HeatAssignment(db.Model):
    """Represents a competitor's assignment to a specific heat.

    ``uid`` is the real reference: a foreign key onto the identity spine,
    unique within a heat.  ``competitor_id`` and ``competitor_type`` are the
    legacy pair, written by ``Heat.set_roster`` alongside the uid and still
    what every reader in this tree uses.  Neither of those two is constrained, separately
    or together, which is the whole reason the uid exists: the pro and college
    id sequences overlap, so the integer alone does not name a human.  D12-C
    phase 2 moves the readers across; this revision only makes the row capable
    of being read that way.
    """

    __tablename__ = 'heat_assignments'
    __table_args__ = (
        db.UniqueConstraint('heat_id', 'uid', name='uq_heat_assignments_heat_uid'),
        db.Index('ix_heat_assignments_uid', 'uid'),
    )

    id = db.Column(db.Integer, primary_key=True)
    heat_id = db.Column(db.Integer, db.ForeignKey('heats.id'), nullable=False)
    uid = db.Column(BIG_ID, db.ForeignKey('competitors.uid'), nullable=False)
    competitor_id = db.Column(db.Integer, nullable=False)
    competitor_type = db.Column(db.String(20), nullable=False)  # 'pro' or 'college'
    stand_number = db.Column(db.Integer, nullable=True)

    heat = db.relationship('Heat', back_populates='assignments')

    def __repr__(self):
        return f'<HeatAssignment heat={self.heat_id} competitor={self.competitor_id}>'


class Heat(db.Model):
    """Represents a heat within an event (group of competitors running together)."""

    __tablename__ = 'heats'
    __table_args__ = (
        db.UniqueConstraint('event_id', 'heat_number', 'run_number', name='uq_event_heat_run'),
        db.Index('ix_heats_event_status', 'event_id', 'status'),
        db.Index('ix_heats_flight_id', 'flight_id'),
        db.CheckConstraint('heat_number >= 1', name='ck_heats_heat_number_positive'),
        db.CheckConstraint('run_number >= 1', name='ck_heats_run_number_positive'),
        db.CheckConstraint(
            "status IN ('pending', 'in_progress', 'completed')",
            name='ck_heats_status_valid',
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    event_id = db.Column(db.Integer, db.ForeignKey('events.id'), nullable=False)

    # Heat identification
    heat_number = db.Column(db.Integer, nullable=False)
    run_number = db.Column(db.Integer, nullable=False, default=1)  # For dual-run events (1 or 2)

    # Competitors and stand assignments - stored as JSON
    competitors = db.Column(db.Text, nullable=False, default='[]')  # List of competitor IDs
    stand_assignments = db.Column(db.Text, nullable=False, default='{}')  # Dict: competitor_id -> stand_number

    # Status
    status = db.Column(db.String(20), nullable=False, default='pending')  # pending, in_progress, completed
    version_id = db.Column(
        db.Integer, nullable=False, default=1, server_default=sa.text("'1'")
    )

    __mapper_args__ = {
        'version_id_col': version_id,
    }

    # Edit lock — prevents two judges on different devices from simultaneously entering the same heat.
    # Acquired when the entry form is opened; auto-expires after HEAT_LOCK_TTL_SECONDS.
    locked_by_user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    locked_at = db.Column(db.DateTime, nullable=True)

    # Optional flight assignment (pro only)
    flight_id = db.Column(db.Integer, db.ForeignKey('flights.id'), nullable=True)
    flight_position = db.Column(db.Integer, nullable=True)  # 1-based order within a flight

    #: This heat's roster, in running order (D12-C phase 2, commit C).
    #:
    #: Ordered by ``HeatAssignment.id`` because that is the order
    #: :meth:`set_roster` inserts them in, which is the order the judge sheet
    #: prints. There is no explicit position column and adding one is not this
    #: commit's business; the insert order is load-bearing either way, and
    #: saying so here is cheaper than a reader discovering it.
    #:
    #: ``delete-orphan`` because an assignment row has no meaning apart from
    #: its heat. Detaching one and leaving it behind would produce exactly the
    #: orphan ``heat_assignments`` rows that D12-C exists to make impossible.
    #:
    #: Six places delete heats or rosters with a bulk
    #: ``HeatAssignment.query...delete()``, which bypasses this cascade
    #: entirely. They are correct as they stand and are not being converted
    #: here: a bulk delete is the right tool for "every row in this
    #: tournament", and the ORM cascade is the right tool for one heat's
    #: roster. What matters is that neither leaves rows behind.
    assignments = db.relationship(
        'HeatAssignment',
        back_populates='heat',
        order_by='HeatAssignment.id',
        cascade='all, delete-orphan',
    )

    def __repr__(self):
        run_str = f" Run {self.run_number}" if self.run_number > 1 else ""
        return f'<Heat {self.heat_number}{run_str}>'

    def get_competitors(self):
        """Return list of competitor IDs in this heat."""
        try:
            return json.loads(self.competitors or '[]')
        except json.JSONDecodeError:
            return []

    def set_competitors(self, competitor_ids):
        """Set the list of competitor IDs."""
        self.competitors = json.dumps(competitor_ids)

    def add_competitor(self, competitor_id):
        """Add a competitor to this heat."""
        comps = self.get_competitors()
        if competitor_id not in comps:
            comps.append(competitor_id)
            self.competitors = json.dumps(comps)

    def remove_competitor(self, competitor_id):
        """Remove a competitor from this heat."""
        comps = self.get_competitors()
        if competitor_id in comps:
            comps.remove(competitor_id)
            self.competitors = json.dumps(comps)

    def get_stand_assignments(self):
        """Return dict of competitor_id -> stand_number."""
        try:
            return json.loads(self.stand_assignments or '{}')
        except json.JSONDecodeError:
            return {}

    def set_stand_assignment(self, competitor_id, stand_number):
        """Assign a competitor to a specific stand."""
        assignments = self.get_stand_assignments()
        assignments[str(competitor_id)] = stand_number
        self.stand_assignments = json.dumps(assignments)

    def get_stand_for_competitor(self, competitor_id):
        """Get the stand number assigned to a competitor."""
        assignments = self.get_stand_assignments()
        return assignments.get(str(competitor_id))

    def set_roster(self, competitor_type, comp_ids, stands=None) -> bool:
        """Write this heat's roster to ``heat_assignments``, then render the JSON.

        This is the write target. D12-C commit A gave the rows a real reference;
        this is where they become the thing being written TO, with
        ``competitors`` and ``stand_assignments`` demoted to a rendering of what
        the rows say rather than the record the rows are copied from. Phase 2
        moves the readers across and drops the two columns, at which point
        :meth:`_project_json` is the only thing that has to be deleted.

        ``competitor_type`` is 'pro' or 'college' and matches event.event_type.
        ``comp_ids`` is the running order: the order the judge sheet prints and
        the order the rows are written in. ``stands`` maps a competitor id to a
        stand number and may be keyed by int or by str.

        Does not require ``self.id``. The rows go on the ``assignments``
        relationship, so a heat that has not been flushed yet gets a roster and
        the rows pick up their ``heat_id`` when it does. That matters because
        ``services/heat_generator.py`` builds a heat, sets its roster and only
        then adds it to the session, which the previous version of this method
        could not have served.

        Returns True if anything was rewritten, rows or JSON, and False if the
        heat already said exactly this. The two bulk sweepers,
        ``run_preflight_autofix`` and ``heat_sync_fix``, walk every heat in a
        tournament or an event and use the return value to tell a heat they
        repaired from a heat they merely visited; without it they reported the
        walk count as a repair count and rewrote every row in the table to
        produce it. The JSON half is inside that answer on purpose: until phase
        2 lands, most readers are still reading the JSON, so a heat whose rows
        are right and whose JSON disagrees is exactly as broken to a reader as
        the reverse, and a sweeper that fixed it should say so.

        Raises :class:`BadHeatAssignment`, before touching anything, when the
        roster names a competitor that does not exist or names one twice. Both
        are constraint violations on the table as of s8a0b2c3d4e5. Catching them
        here is what turns a driver IntegrityError arriving from an unrelated
        autoflush into an error that says which heat.
        """
        raw_ids = list(comp_ids or [])
        stands = {str(k): v for k, v in (stands or {}).items()}
        resolved = self._resolve_assignment_uids(raw_ids, competitor_type)

        # Keyed on the canonical int rather than on the raw JSON value, so `5`
        # and `"5"` are one competitor with one stand instead of two entries the
        # unique constraint would then have to reject.
        roster = [resolved[raw][:2] + (stands.get(str(resolved[raw][0])),)
                  for raw in raw_ids]

        rows = list(self.assignments)

        # Sorted lists, not sets. A set collapses a duplicate row and would
        # report a heat clean forever. All four columns the rebuild writes are
        # carried: competitor_type because the two id sequences overlap, so a
        # row with the wrong type points at the wrong person while holding the
        # right number, and uid for the same reason from the other end.
        wanted = sorted((cid, competitor_type, stand, uid)
                        for cid, uid, stand in roster)
        existing = sorted((r.competitor_id, r.competitor_type, r.stand_number,
                           r.uid) for r in rows)

        rows_changed = existing != wanted
        if rows_changed:
            # Insert in roster order, not in `wanted` order. `wanted` is sorted
            # for the comparison only; rebuilding from it would silently reorder
            # the rows, and their autoincrement ids, relative to the heat's
            # running order, which is a change nobody asked for.
            self._replace_assignments([
                HeatAssignment(
                    uid=uid,
                    competitor_id=cid,
                    competitor_type=competitor_type,
                    stand_number=stand,
                )
                for cid, uid, stand in roster
            ], rows)
            projection = [(cid, stand) for cid, _uid, stand in roster]
        else:
            # Render the rows that are actually there, in the order they are
            # actually in. When the rows were not rewritten this can differ from
            # `roster`, and the rows are the ones that win. That is the whole
            # difference between a projection and an echo of the argument.
            projection = [(r.competitor_id, r.stand_number) for r in rows]

        json_changed = self._project_json(projection)
        return rows_changed or json_changed

    def _replace_assignments(self, new_rows, old_rows):
        """Swap this heat's roster rows out for ``new_rows``, in two steps.

        The two steps are the whole point, and collapsing them into a single
        ``self.assignments = new_rows`` is wrong in a way that only shows up on
        real data.

        SQLAlchemy's unit of work orders a flush by mapper operation, INSERTs
        before DELETEs, not by the order the collection was mutated. A wholesale
        replacement therefore tries to insert the new rows while the old ones
        are still in the table, and ``uq_heat_assignments_heat_uid`` rejects
        that the moment one competitor appears in both rosters. Which is most of
        them: the common edit is a stand change or a reorder, where every
        competitor is in both. The constraint is not DEFERRABLE and cannot be
        made so on SQLite, so there is nothing to defer it to.

        Reconciling the rows in place instead, matching old to new by position
        and mutating, has the same defect from the other side: two competitors
        swapping stands is two UPDATEs, and whichever one lands first collides
        with the row the other has not vacated yet.

        So: detach the old rows, flush the DELETEs on their own, then attach the
        new ones. The flush is not a new behaviour. The bulk
        ``HeatAssignment.query.filter_by(...).delete()`` this replaced autoflushed
        the session and emitted its DELETE immediately, which is precisely why
        the old code was safe.

        The flush is skipped when none of the old rows was ever persisted, which
        is the case for a heat built and rostered before its first flush. Nothing
        is in the table to collide with, and flushing there would push a
        half-built heat at the database earlier than the caller asked for.
        """
        needs_flush = any(r.id is not None for r in old_rows)
        self.assignments = []
        if needs_flush:
            db.session.flush()
        self.assignments = new_rows

    def sync_assignments(self, competitor_type: str) -> bool:
        """Write the roster the JSON columns currently describe.

        The compatibility shim for the call sites that still express a roster
        change by mutating ``competitors`` or ``stand_assignments`` and then
        asking the rows to catch up. Each of those becomes a :meth:`set_roster`
        call as it is converted, and this method is what keeps them working in
        the meantime. It is what gets deleted when the last one moves.

        Reading the JSON and projecting it straight back is not a no-op. What
        lands in the column afterwards is what the rows say, normalised, not
        what the caller wrote: a stand assignment naming somebody who is not in
        the heat is dropped, `"5"` and `5` collapse to one competitor, and a
        heat whose rows disagree with its JSON is rendered from the rows.

        Same return value and same refusals as :meth:`set_roster`.
        """
        return self.set_roster(competitor_type, self.get_competitors(),
                               self.get_stand_assignments())

    def _project_json(self, projection):
        """Render ``competitors`` and ``stand_assignments`` from the rows.

        ``projection`` is a list of ``(competitor_id, stand_number)`` in row
        order. Returns True if either column changed.

        Compares the serialised form rather than the parsed one, because the
        stored column is what every reader in this tree parses, and a difference
        that survives ``json.dumps`` is a difference a reader can see. It also
        keeps this from dirtying the ``Heat`` row for a change that is not one:
        ``Heat`` carries a ``version_id_col``, so a pointless assignment here
        would bump the version and hand a StaleDataError to whichever other
        request was holding the heat.

        A row with no stand contributes no key, which is the shape this column
        has always had: ``stand_assignments`` has never carried an explicit
        null.
        """
        competitors = json.dumps([cid for cid, _stand in projection])
        stand_assignments = json.dumps(
            {str(cid): stand for cid, stand in projection if stand is not None})

        if (competitors == self.competitors
                and stand_assignments == self.stand_assignments):
            return False
        self.competitors = competitors
        self.stand_assignments = stand_assignments
        return True

    def _resolve_assignment_uids(self, comp_ids, competitor_type):
        """Map every id in `comp_ids` to its canonical id and a competitors.uid.

        Returns ``{raw_json_id: (canonical_id, uid)}``, keyed by the value the
        caller already has so it does not have to rebuild an EntityKey per row,
        and carrying the canonical id because the raw one may be a string. The
        canonical id is what reaches the integer column and what keys the stand
        lookup, so `5` and `"5"` cannot describe two different competitors with
        two different stands.

        The three refusals are the three ways a roster can describe a heat the
        database will not store. All are raised before the delete, so a heat
        that fails this leaves its existing rows exactly as they were: this is
        fail-closed, not fail-halfway, which is the only version that is safe to
        hit during a live show.

        The duplicate check is made on the EntityKey rather than on the raw
        value, and so runs after the keys are built. `5` and `"5"` are two
        distinct JSON entries that name one competitor and would resolve to one
        uid, and checking the raw values would let that pair through to the
        unique constraint as an IntegrityError. Neither the production data nor
        any parity mirror contains a non-integer id; the check is written for
        the ones this code has not seen.
        """
        if not comp_ids:
            return {}

        # A list of pairs, not a dict, so that two JSON entries which normalise
        # onto the same key are both still present for the duplicate check.
        pairs = []
        malformed = []
        for comp_id in comp_ids:
            try:
                key = EntityKey.from_legacy(comp_id, competitor_type)
            except (TypeError, ValueError) as exc:
                malformed.append(f'{comp_id!r} ({exc})')
                continue
            if key is None:
                malformed.append(f'{comp_id!r} (null competitor id)')
                continue
            pairs.append((comp_id, key))
        if malformed:
            raise BadHeatAssignment(
                f'heat {self.id} carries competitor ids that are not usable as '
                f'a {competitor_type!r} reference: ' + '; '.join(malformed)
            )

        seen = set()
        duplicates = set()
        for _, key in pairs:
            if key in seen:
                duplicates.add(key.id)
            seen.add(key)
        if duplicates:
            raise BadHeatAssignment(
                f'heat {self.id} lists {competitor_type} competitor(s) '
                f'{sorted(duplicates)} more than once; a competitor runs in a '
                f'heat once'
            )

        resolved = resolve_uids(db.session, seen)

        out = {}
        missing = []
        for comp_id, key in pairs:
            uid = resolved.get(key)
            if uid is None:
                missing.append(key.id)
            else:
                out[comp_id] = (key.id, uid)
        if missing:
            raise BadHeatAssignment(
                f'heat {self.id} names {competitor_type} competitor(s) '
                f'{sorted(missing)} that do not exist, or that carry no '
                f'identity row'
            )
        return out

    @property
    def competitor_count(self):
        """Return number of competitors in this heat."""
        return len(self.get_competitors())

    def is_locked(self) -> bool:
        """True if the heat is currently locked by another judge (non-expired)."""
        if not self.locked_by_user_id or not self.locked_at:
            return False
        now = datetime.now(timezone.utc)
        locked_at = self.locked_at
        if locked_at.tzinfo is None:
            locked_at = locked_at.replace(tzinfo=timezone.utc)
        return (now - locked_at).total_seconds() < HEAT_LOCK_TTL_SECONDS

    def acquire_lock(self, user_id: int) -> bool:
        """Attempt to acquire the edit lock. Returns True if successful."""
        if self.is_locked() and self.locked_by_user_id != user_id:
            return False
        self.locked_by_user_id = user_id
        self.locked_at = datetime.now(timezone.utc)
        return True

    def release_lock(self, user_id: int) -> None:
        """Release the lock if held by user_id."""
        if self.locked_by_user_id == user_id:
            self.locked_by_user_id = None
            self.locked_at = None


class Flight(db.Model):
    """Represents a flight in pro competition (group of heats from different events)."""

    __tablename__ = 'flights'
    __table_args__ = (
        db.CheckConstraint('flight_number >= 1', name='ck_flights_flight_number_positive'),
        db.CheckConstraint(
            "status IN ('pending', 'in_progress', 'completed')",
            name='ck_flights_status_valid',
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    tournament_id = db.Column(db.Integer, db.ForeignKey('tournaments.id'), nullable=False)

    # Flight identification
    flight_number = db.Column(db.Integer, nullable=False)
    name = db.Column(db.String(100), nullable=True)  # Optional custom name

    # Status
    status = db.Column(db.String(20), nullable=False, default='pending')  # pending, in_progress, completed

    # Notes
    notes = db.Column(db.Text, nullable=True)  # For special instructions

    # Relationships
    # O3: default order matches get_heats_ordered below minus the NULL
    # handling (relationship order_by is a plain clause list; the case()
    # there exists for cross-backend NULL placement and callers who need
    # it use get_heats_ordered). id last so the order is total.
    heats = db.relationship('Heat', backref='flight', lazy='dynamic',
                            order_by='Heat.flight_position, Heat.id')

    def __repr__(self):
        return f'<Flight {self.flight_number}>'

    def get_heats_ordered(self):
        """Return heats in this flight, ordered by their sequence."""
        return self.heats.order_by(
            db.case((Heat.flight_position.is_(None), 1), else_=0),
            Heat.flight_position,
            Heat.id,
        ).all()

    def add_heat(self, heat):
        """Add a heat to this flight."""
        heat.flight_id = self.id

    @property
    def heat_count(self):
        """Return number of heats in this flight."""
        return self.heats.count()

    @property
    def event_variety(self):
        """Return count of unique events represented in this flight."""
        heats = self.heats.all()
        event_ids = set(h.event_id for h in heats)
        return len(event_ids)
