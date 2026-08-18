"""
Flight builder service for pro competition scheduling.
Builds flights with event variety for crowd engagement.
Ensures competitors have maximum rest between their events using tiered spacing:
  Tier 1 (springboard):  min=6, target=8 heats between appearances
  Tier 2 (saw_hand):     min=5, target=7 heats between appearances
  Tier 3 (all others):   min=4, target=5 heats between appearances
"""
import json
import logging
import math
import threading
from collections import defaultdict
from contextlib import contextmanager
from functools import wraps

from config import DAY_SPLIT_EVENT_NAMES
from database import db
from models import Event, Flight, Heat, HeatAssignment, Tournament

logger = logging.getLogger(__name__)


# Global fallback spacing (used for unknown stand types and college heats)
MIN_HEAT_SPACING = 4
TARGET_HEAT_SPACING = 5

PARTNERED_AXE_EVENT_NAME = 'Partnered Axe Throw'
PARTNERED_AXE_SHOW_TEAM_COUNT = 4

# Placement modes for non-Chokerman spillover events (locked plan-eng-review
# decision: Speed Climb Run 2 defaults to round-robin with spacing, but judges
# can toggle to 'cluster' mode to greedy-fill Saturday flights from flight 1
# forward). Chokerman's Race is unaffected — it is always the show closer.
PLACEMENT_MODE_ROUNDROBIN = 'roundrobin'
PLACEMENT_MODE_CLUSTER = 'cluster'
VALID_PLACEMENT_MODES = {PLACEMENT_MODE_ROUNDROBIN, PLACEMENT_MODE_CLUSTER}
DEFAULT_HEATS_PER_FLIGHT = 8

# Phase 5: track LH flight-contention warnings from the most recent
# build_pro_flights() call so the route handler can surface them to the
# operator via flash. Keyed by tournament.id → list of warning dicts.
_last_lh_flight_warnings: dict[int, list[dict]] = {}

# SQLite ignores SELECT FOR UPDATE. Keep every schedule writer serialized by
# tournament within this process while PostgreSQL uses a stable Tournament row
# as the first lock in every write transaction.
_sqlite_schedule_locks_guard = threading.Lock()
_sqlite_schedule_locks: dict[tuple[str, int], threading.RLock] = {}


@contextmanager
def sqlite_schedule_writer_guard(tournament_or_id):
    """Serialize one tournament's schedule writers when the backend is SQLite."""
    tournament_id = int(getattr(tournament_or_id, 'id', tournament_or_id))
    bind = db.session.get_bind()
    if bind.dialect.name != 'sqlite':
        yield
        return

    engine = getattr(bind, 'engine', bind)
    lock_key = (str(engine.url), tournament_id)
    with _sqlite_schedule_locks_guard:
        lock = _sqlite_schedule_locks.setdefault(lock_key, threading.RLock())
    with lock:
        yield


def serialize_sqlite_schedule_writer(func):
    """Hold SQLite's per-tournament writer guard for an entire entry point."""
    @wraps(func)
    def serialized(*args, **kwargs):
        tournament_or_id = args[0] if args else (
            kwargs.get('tournament')
            or kwargs.get('tournament_id')
            or kwargs.get('target_tournament_id')
        )
        if tournament_or_id is None:
            raise TypeError(
                f'{func.__name__} requires a tournament or tournament_id'
            )
        with sqlite_schedule_writer_guard(tournament_or_id):
            return func(*args, **kwargs)

    return serialized


def lock_tournament_schedule(tournament_or_id) -> Tournament:
    """Acquire the canonical PostgreSQL parent lock for a schedule write."""
    tournament_id = int(getattr(tournament_or_id, 'id', tournament_or_id))
    return (
        Tournament.query
        .filter_by(id=tournament_id)
        .order_by(Tournament.id)
        .populate_existing()
        .with_for_update()
        .one()
    )


def get_last_lh_flight_warnings(tournament_id: int) -> list[dict]:
    """Return LH-flight-contention warnings from the most recent build for
    the given tournament (empty list when there were none).

    Warning dicts contain:
        flight_number (int), lh_count (int)
    """
    return list(_last_lh_flight_warnings.get(int(tournament_id), []))

# How many independent greedy passes to run; best result is kept.
N_OPTIMIZATION_PASSES = 5

# Per-stand-type spacing tiers: (min_spacing, target_spacing).
# Springboard is the most physically demanding (long axe strokes, 3 boards per cut).
# Saw events are moderately demanding. Everything else uses the global minimum.
EVENT_SPACING_TIERS: dict[str, tuple[int, int]] = {
    'springboard':    (6, 8),
    'saw_hand':       (5, 7),
    'underhand':      (4, 5),
    'standing_block': (4, 5),
    'cookie_stack':   (4, 5),
    'obstacle_pole':  (4, 5),
    'hot_saw':        (4, 5),
    'speed_climb':    (4, 5),
    'stock_saw':      (4, 5),
}

_CONFLICTING_STANDS: dict[str, set[str]] = {
    # Cookie Stack and Standing Block share 5 physical stands. Original entry.
    'standing_block': {'cookie_stack'},
    'cookie_stack': {'standing_block'},
    # Stock Saw uses saw stands 7-8 (DOMAIN_CONTRACT, both pro and college).
    # Hand saw events (Single Buck, Double Buck, Jack & Jill) use the same
    # physical 8-stand set in two groups of 4 — group [5-8] overlaps with
    # the stock-saw stands. Back-to-back scheduling forces a changeover
    # with no break for the crowd. Same physical-clash class as
    # Cookie/Standing. Hot Saw kept as a defensive entry — historic
    # adjacency penalty preserved.
    'stock_saw': {'saw_hand', 'hot_saw'},
    'saw_hand': {'stock_saw'},
    'hot_saw': {'stock_saw'},
    # Obstacle Pole uses Pole 1 + Pole 2; Speed Climb uses Pole 2 + Pole 4.
    # Shared Pole 2 means simultaneous use is impossible. Speed Climb is a
    # day-split run-2 spillover so the collision is rare on Saturday but
    # real when both events land in adjacent flight slots after spillover.
    'obstacle_pole': {'speed_climb'},
    'speed_climb': {'obstacle_pole'},
}
# Minimum gap between conflicting stand types (approximately one flight block)
_STAND_CONFLICT_GAP = 8

# Same-stand-type CROSS-event adjacency: events sharing a stand_type use the same
# physical stands (Men's Underhand + Women's Underhand both draw from the 5 underhand
# stands; Single Buck + Double Buck + Jack & Jill all draw from the 8 hand-saw stands).
# Truly back-to-back placement (gap=1) forces the crew to reset the same stands with
# no break for the crowd. Penalize gap=1 only — gap>=2 means one heat of a different
# stand type breaks up the sequence, which is acceptable.
# Penalty is deliberately smaller than the distribution cap penalty so even-distribution
# still dominates scheduling; adjacency is a secondary ordering concern.
_SAME_STAND_TYPE_MIN_GAP = 2
_SAME_STAND_TYPE_PENALTY = 200.0

# Per-event per-flight distribution: each event's heats are targeted to spread
# evenly across flights as ceil(N_e / target_flights) per flight. Penalty per
# heat over that cap must outweigh first-appearance (+1000) and springboard
# opener (+500) so crowd variety wins over local spacing optimization when a
# heat's competitors never appear in any other event.
EVENT_FLIGHT_CAP_PENALTY = 2000.0
# Scoring-pass penalty is smaller (per-excess-heat) because it's summed across
# the whole ordering rather than applied at a single candidate step.
EVENT_FLIGHT_CAP_SCORE_PENALTY = 500.0


class FlightRebuildSafetyError(RuntimeError):
    """Raised when a schedule mutation would violate a race-day invariant."""

    def __init__(self, message: str, *, reason: str = 'schedule_safety'):
        super().__init__(message)
        self.reason = reason


def _is_chokerman_run2_heat(heat: Heat) -> bool:
    """Return whether a heat is a flighted college Chokerman Run 2 closer."""
    return (
        heat.event is not None
        and heat.event.event_type == 'college'
        and heat.event.name == "Chokerman's Race"
        and heat.run_number == 2
    )


def validate_chokerman_closer_invariant(
    tournament: Tournament,
    *,
    flights: list[Flight] | None = None,
    projected_order: list[Heat] | None = None,
) -> dict:
    """Require every flighted Chokerman Run 2 heat to close the final flight.

    Routes may call this with only a tournament to validate persisted schedule
    state. Scheduling code can pass its locked flights and projected heat order
    so the exact same invariant validates an in-memory plan before it is flushed.
    """
    ordered_flights = list(flights) if flights is not None else (
        Flight.query
        .filter_by(tournament_id=tournament.id)
        .order_by(Flight.flight_number, Flight.id)
        .all()
    )
    ordered_flights.sort(key=lambda flight: (flight.flight_number, flight.id))

    if projected_order is None:
        flight_indexes = {
            flight.id: index for index, flight in enumerate(ordered_flights)
        }
        if flight_indexes:
            projected_order = (
                Heat.query
                .filter(Heat.flight_id.in_(list(flight_indexes)))
                .all()
            )
            projected_order.sort(
                key=lambda heat: (
                    flight_indexes[heat.flight_id],
                    heat.flight_position is None,
                    heat.flight_position if heat.flight_position is not None else 0,
                    heat.id,
                )
            )
        else:
            projected_order = []
    else:
        projected_order = list(projected_order)

    closers = [heat for heat in projected_order if _is_chokerman_run2_heat(heat)]
    if not closers:
        return {
            'valid': True,
            'last_flight_id': ordered_flights[-1].id if ordered_flights else None,
            'chokerman_run2_heat_ids': [],
        }

    last_flight_id = ordered_flights[-1].id if ordered_flights else None
    first_closer_index = next(
        index
        for index, heat in enumerate(projected_order)
        if _is_chokerman_run2_heat(heat)
    )
    closer_is_final_suffix = (
        last_flight_id is not None
        and all(heat.flight_id == last_flight_id for heat in closers)
        and all(
            _is_chokerman_run2_heat(heat)
            for heat in projected_order[first_closer_index:]
        )
    )
    if not closer_is_final_suffix:
        raise FlightRebuildSafetyError(
            "College Chokerman's Race Run 2 heats must form the final suffix "
            'of the last flight.',
            reason='chokerman_closer_suffix',
        )

    expected_closers = sorted(
        closers,
        key=lambda heat: (
            heat.event.gender or '',
            heat.event_id,
            heat.heat_number,
            heat.id,
        ),
    )
    if [heat.id for heat in closers] != [heat.id for heat in expected_closers]:
        raise FlightRebuildSafetyError(
            "College Chokerman's Race Run 2 heats must preserve heat-number "
            'order within the final suffix.',
            reason='chokerman_closer_order',
        )

    return {
        'valid': True,
        'last_flight_id': last_flight_id,
        'chokerman_run2_heat_ids': [heat.id for heat in closers],
    }


def _get_spacing(event: Event | None) -> tuple[int, int]:
    """Return (min_spacing, target_spacing) for this event's stand type."""
    st = getattr(event, 'stand_type', None) or ''
    return EVENT_SPACING_TIERS.get(st, (MIN_HEAT_SPACING, TARGET_HEAT_SPACING))


@serialize_sqlite_schedule_writer
def build_pro_flights(tournament: Tournament, num_flights: int = None, commit: bool = True) -> int:
    """
    Build flights for pro competition with event variety and competitor spacing.

    Flights mix heats from different events to keep crowd engaged while
    ensuring competitors have adequate rest between their events.

    Springboard heats naturally open each flight via a large scoring bonus.
    Hot Saw heats receive a bonus when placed as the flight closer.
    The greedy algorithm runs N_OPTIMIZATION_PASSES times and keeps the best result.

    Args:
        tournament: Tournament to build flights for
        num_flights: Total number of flights to create. When provided, heats are
                     distributed evenly across that many flights. When omitted,
                     defaults to distributing in blocks of 8 heats per flight.
        commit: When True (default), commit the transaction at the end. When False,
                flush only so the caller can chain additional work
                (e.g. integrate_college_spillover_into_flights) inside a single
                outer transaction. Mirrors ProAmRelay._save_relay_data(commit=False).

    Returns:
        Number of flights created
    """
    lock_tournament_schedule(tournament)

    # A completed heat's flight and position are the published show record.
    # Reject before clearing any relationship so every caller, including async
    # and one-click paths, preserves exactly the same schedule snapshot.
    existing_flights = (
        Flight.query
        .filter_by(tournament_id=tournament.id)
        .order_by(Flight.id)
        .with_for_update()
        .all()
    )
    active_flights = [flight for flight in existing_flights if flight.status != 'pending']
    if active_flights:
        blocked = ', '.join(
            f'{flight.flight_number} ({flight.status})'
            for flight in active_flights
        )
        raise FlightRebuildSafetyError(
            'Cannot rebuild flights after a flight starts; blocked flight(s): '
            f'{blocked}.'
        )

    existing_flight_ids = [flight.id for flight in existing_flights]
    if existing_flight_ids:
        completed_heat_count = Heat.query.filter(
            Heat.flight_id.in_(existing_flight_ids),
            Heat.status == 'completed',
        ).count()
        if completed_heat_count:
            raise FlightRebuildSafetyError(
                'Flights cannot be rebuilt after scoring begins because completed '
                'heat placements are historical records.'
            )
        active_heat_count = Heat.query.filter(
            Heat.flight_id.in_(existing_flight_ids),
            Heat.status == 'in_progress',
        ).count()
        if active_heat_count:
            raise FlightRebuildSafetyError(
                'Cannot rebuild flights while a heat is in progress because '
                'its published placement is active race-day state.'
            )

    # Clear existing flights (null out Heat.flight_id first to satisfy FK constraints)
    if existing_flight_ids:
        Heat.query.filter(Heat.flight_id.in_(existing_flight_ids)).update(
            {'flight_id': None, 'flight_position': None}, synchronize_session=False
        )
    Flight.query.filter_by(tournament_id=tournament.id).delete(synchronize_session=False)

    # Get all pro event heats
    pro_events = tournament.events.filter_by(event_type='pro').all()
    partnered_axe_event = next(
        (event for event in pro_events if event.name == PARTNERED_AXE_EVENT_NAME),
        None
    )
    partnered_axe_heats = _prepare_partnered_axe_show_heats(partnered_axe_event)

    # Exclude Friday Night Feature events from Saturday flight building.
    # FNF events (e.g. Pro 1-Board, 3-Board Jigger) run Friday evening as a separate
    # showcase and must NOT appear in Saturday pro flights.
    fnf_event_ids: set[int] = set()
    try:
        schedule_config = tournament.get_schedule_config() or {}
        fnf_event_ids = {
            int(eid) for eid in schedule_config.get('friday_pro_event_ids', [])
            if str(eid).strip()
        }
    except Exception:
        logger.warning('flight_builder: could not read friday_pro_event_ids', exc_info=True)

    # Collect all non-axe heats with their competitor information.
    # Batch-load all heats for non-axe events in a single query to avoid N+1.
    non_axe_events = [
        e for e in pro_events
        if not (partnered_axe_event and e.id == partnered_axe_event.id)
        and e.id not in fnf_event_ids
    ]
    non_axe_event_ids = [e.id for e in non_axe_events]
    event_by_id = {e.id: e for e in non_axe_events}

    batched_heats = (
        Heat.query
        .filter(
            Heat.event_id.in_(non_axe_event_ids),
            Heat.run_number == 1,
            Heat.status != 'completed',
        )
        .order_by(Heat.event_id, Heat.heat_number)
        .all()
    ) if non_axe_event_ids else []
    logger.debug('flight_builder: loaded %d non-axe heats for %d events',
                 len(batched_heats), len(non_axe_event_ids))

    # Batch-load left-handed-springboard flags for all springboard-heat
    # competitors so the optimizer can penalize >1 LH heat per flight
    # (domain rule: only one physical LH dummy on site, so one LH cutter
    # per flight time-slot).  Single .in_() query — no N+1.
    lh_comp_ids: set[int] = set()
    for heat in batched_heats:
        event = event_by_id.get(heat.event_id)
        if event and getattr(event, 'stand_type', None) == 'springboard':
            lh_comp_ids.update(heat.get_competitors())

    lh_flags: dict[int, bool] = {}
    if lh_comp_ids:
        from models.competitor import ProCompetitor
        lh_rows = (
            ProCompetitor.query
            .filter(
                ProCompetitor.id.in_(lh_comp_ids),
                ProCompetitor.is_left_handed_springboard.is_(True),
            )
            .with_entities(ProCompetitor.id)
            .all()
        )
        lh_flags = {row.id: True for row in lh_rows}

    all_heats = []
    for heat in batched_heats:
        event = event_by_id.get(heat.event_id)
        if event:
            comps = set(heat.get_competitors())
            # contains_lh is only meaningful for springboard heats — that is the
            # only event type that physically uses the LH dummy. A LH competitor
            # racing obstacle pole or cookie stack has no bearing on the dummy.
            is_springboard = getattr(event, 'stand_type', None) == 'springboard'
            contains_lh = is_springboard and any(lh_flags.get(cid, False) for cid in comps)
            all_heats.append({
                'heat': heat,
                'event': event,
                'competitors': comps,
                'contains_lh': contains_lh,
            })

    if not all_heats and not partnered_axe_heats:
        return 0

    # Derive heats_per_flight from caller-supplied num_flights, or fall back to default of 8.
    # A flight is a grouping of heats from different events for crowd variety, so enforce a
    # minimum of 2 heats per flight — otherwise "flights" are just heats in a wrapper.
    total_non_axe = len(all_heats)
    MIN_HEATS_PER_FLIGHT = 2
    if num_flights and num_flights > 0 and total_non_axe > 0:
        target_flights = int(num_flights)
        heats_per_flight = math.ceil(total_non_axe / target_flights)
        if heats_per_flight < MIN_HEATS_PER_FLIGHT and total_non_axe >= MIN_HEATS_PER_FLIGHT:
            heats_per_flight = MIN_HEATS_PER_FLIGHT
            clamped = math.ceil(total_non_axe / heats_per_flight)
            logger.warning(
                'flight_builder: requested %d flights for %d heats would give <%d per flight; '
                'clamped to %d flights (%d heats each)',
                num_flights, total_non_axe, MIN_HEATS_PER_FLIGHT, clamped, heats_per_flight,
            )
            target_flights = clamped
    else:
        heats_per_flight = DEFAULT_HEATS_PER_FLIGHT
        target_flights = math.ceil(total_non_axe / heats_per_flight) if total_non_axe else 0

    # Pre-compute gear-sharing conflict pairs for adjacency penalty.
    gear_conflict_pairs: dict[int, set[int]] = {}
    try:
        from services.gear_sharing import build_gear_conflict_pairs
        gear_conflict_pairs = build_gear_conflict_pairs(tournament)
    except Exception:
        logger.warning('flight_builder: could not load gear conflict pairs', exc_info=True)

    # Build optimized heat order using multi-pass greedy algorithm.
    # Springboard opener and Hot Saw closer bonuses are baked into the scoring.
    ordered_heats = _optimize_heat_order(all_heats, heats_per_flight, N_OPTIMIZATION_PASSES,
                                         gear_conflict_pairs=gear_conflict_pairs)
    total_heats = len(ordered_heats)

    # Partnered axe requires one heat per flight, so ensure enough flights.
    if target_flights == 0 and partnered_axe_heats:
        target_flights = 1

    # Create flights and assign non-axe heats
    flights_created = 0
    heat_index = 0
    created_flights: list[Flight] = []
    lh_count_per_flight: dict[int, int] = {}  # flight_number -> LH heat count

    for flight_num in range(1, target_flights + 1):
        flight = Flight(
            tournament_id=tournament.id,
            flight_number=flight_num
        )
        db.session.add(flight)
        db.session.flush()
        created_flights.append(flight)

        heats_in_flight = 0
        while heats_in_flight < heats_per_flight and heat_index < total_heats:
            heat_data = ordered_heats[heat_index]
            heat_data['heat'].flight_id = flight.id
            heat_data['heat'].flight_position = heats_in_flight + 1
            if heat_data.get('contains_lh'):
                lh_count_per_flight[flight_num] = lh_count_per_flight.get(flight_num, 0) + 1
            heat_index += 1
            heats_in_flight += 1

        flights_created += 1

    # Post-slice sanity check: if any flight ended up with >1 LH-containing heat,
    # the scoring penalty was dominated by spacing constraints. Log a warning so
    # the admin knows the LH dummy will be over-subscribed in those flights,
    # AND record the warning in _last_lh_flight_warnings so the route handler
    # can surface it to the operator via flash message (Phase 5).
    lh_warnings_for_tournament: list[dict] = []
    for fnum, count in lh_count_per_flight.items():
        if count > 1:
            logger.warning(
                'LH DUMMY CONTENTION: flight %d has %d LH-containing heats. '
                'Only one physical LH springboard dummy exists. '
                'Manual review recommended.',
                fnum, count,
            )
            lh_warnings_for_tournament.append({
                'flight_number': fnum,
                'lh_count': count,
            })
    if lh_warnings_for_tournament:
        _last_lh_flight_warnings[tournament.id] = lh_warnings_for_tournament
    else:
        _last_lh_flight_warnings.pop(tournament.id, None)

    # Insert partnered axe heats with deterministic flight placement.
    _insert_partnered_axe_heats(created_flights, partnered_axe_heats)

    if commit:
        db.session.commit()
    else:
        db.session.flush()
    return flights_created


def _prepare_partnered_axe_show_heats(event: Event | None) -> list[Heat]:
    """
    Return partnered axe heats to place into the show.

    If prelim standings are available, rebuild partnered axe to the top
    PARTNERED_AXE_SHOW_TEAM_COUNT pairs.
    """
    if not event:
        return []

    qualifier_pairs = _get_partnered_axe_qualifier_pairs(event, PARTNERED_AXE_SHOW_TEAM_COUNT)
    if not qualifier_pairs:
        return event.heats.filter_by(run_number=1).order_by(Heat.heat_number).all()

    # Partnered Axe owns its prelim/finals state machine, so its prelim
    # EventResult rows are not enough to distinguish a safe first show build
    # from a final already underway. Once a final score exists, reseeding from
    # prelim standings would discard the live final card and its score state.
    state = _partnered_axe_state(event)
    finalists = state.get('finalists') if isinstance(state, dict) else []
    if (
        (isinstance(state, dict) and state.get('stage') == 'completed')
        or any(pair.get('final_score') is not None for pair in finalists if isinstance(pair, dict))
    ):
        return event.heats.filter_by(run_number=1).order_by(Heat.heat_number).all()

    heat_ids = [h.id for h in Heat.query.filter_by(event_id=event.id).with_entities(Heat.id).all()]
    if heat_ids:
        HeatAssignment.query.filter(HeatAssignment.heat_id.in_(heat_ids)).delete(synchronize_session=False)
    Heat.query.filter_by(event_id=event.id).delete(synchronize_session=False)

    created = []
    for idx, pair in enumerate(qualifier_pairs, start=1):
        comp1 = pair.get('competitor1', {}) or {}
        comp2 = pair.get('competitor2', {}) or {}
        comp_ids = []
        if isinstance(comp1.get('id'), int):
            comp_ids.append(comp1['id'])
        if isinstance(comp2.get('id'), int):
            comp_ids.append(comp2['id'])

        heat = Heat(
            event_id=event.id,
            heat_number=idx,
            run_number=1
        )
        # One roster write per heat, stands included.  Both partners of a
        # qualifier pair share stand 1, which is what the two-line version of
        # this said and what this says.
        heat.set_roster('pro', comp_ids, {cid: 1 for cid in comp_ids})
        db.session.add(heat)
        created.append(heat)

    # The rows went on each heat's `assignments` before it was added, so this
    # flush inserts heats and rows together.  The `sync_assignments` pass that
    # used to follow it had nothing left to copy.
    db.session.flush()
    return created


def _partnered_axe_state(event: Event) -> dict:
    """Return the dedicated Partnered Axe state document, or an empty dict."""
    raw_state = getattr(event, 'event_state', None) or event.payouts
    try:
        state = json.loads(raw_state or '{}')
    except (json.JSONDecodeError, TypeError):
        return {}
    return state if isinstance(state, dict) else {}


def _get_partnered_axe_qualifier_pairs(event: Event, count: int) -> list[dict]:
    """Read prelim standings from partnered axe event state and return top N pairs."""
    state = _partnered_axe_state(event)

    prelim_results = state.get('prelim_results')
    if not isinstance(prelim_results, list):
        prelim_results = []

    if not prelim_results:
        pairs = state.get('pairs', [])
        if isinstance(pairs, list):
            prelim_results = [p for p in pairs if p.get('prelim_score') is not None]
            prelim_results.sort(key=lambda x: x.get('prelim_score', 0), reverse=True)

    valid_pairs = []
    for pair in prelim_results:
        comp1 = pair.get('competitor1', {}) or {}
        comp2 = pair.get('competitor2', {}) or {}
        if not isinstance(comp1.get('id'), int) or not isinstance(comp2.get('id'), int):
            continue
        valid_pairs.append(pair)

    return valid_pairs[:count]


def _insert_partnered_axe_heats(flights: list[Flight], axe_heats: list[Heat]) -> None:
    """
    Assign partnered axe heats across flights in deterministic order.

    Heats are sorted by heat_number (which reflects prelim ranking) and
    distributed one-per-flight in flight_number order. This is deterministic
    and reproducible — no random shuffle.
    """
    if not flights or not axe_heats:
        return

    sorted_flights = sorted(flights, key=lambda f: f.flight_number)
    sorted_heats = sorted(axe_heats, key=lambda h: h.heat_number)

    for idx, heat in enumerate(sorted_heats):
        flight = sorted_flights[idx % len(sorted_flights)]
        heat.flight_id = flight.id
        heat.flight_position = _next_flight_position(flight.id)


def _next_flight_position(flight_id: int) -> int:
    """Return next 1-based display position within a flight."""
    max_pos = db.session.query(db.func.max(Heat.flight_position)).filter(
        Heat.flight_id == flight_id
    ).scalar()
    return int(max_pos or 0) + 1


def _optimize_heat_order(all_heats: list, heats_per_flight: int = 8,
                         n_passes: int = N_OPTIMIZATION_PASSES,
                         gear_conflict_pairs: dict[int, set[int]] | None = None) -> list:
    """
    Optimize heat order using a multi-pass greedy algorithm.

    Runs the greedy n_passes times, each time rotating the initial event order to
    explore different orderings. Keeps the run with the highest quality score.

    Within each pass:
    - Only the NEXT unplaced heat from each event is eligible (sequential guarantee).
    - Scoring uses per-event tiered spacing, springboard opener bonus, Hot Saw
      closer bonus, and event recency bonus to encourage flight-block variety.
    - Tie-breaking: prefer the event with the most remaining unplaced heats.

    Args:
        all_heats: List of heat data dicts with 'heat', 'event', 'competitors'
        heats_per_flight: Size of each flight block (used for opener/closer bonuses)
        n_passes: Number of independent greedy passes to run

    Returns:
        Ordered list of heat data dicts
    """
    if not all_heats:
        return []

    from collections import defaultdict

    # Build a sorted queue for each event (by heat_number then run_number).
    event_queues: dict[int, list] = defaultdict(list)
    for heat_data in all_heats:
        event_queues[heat_data['heat'].event_id].append(heat_data)
    for eid in event_queues:
        event_queues[eid].sort(
            key=lambda h: (h['heat'].heat_number, h['heat'].run_number)
        )
    event_ids = list(event_queues.keys())

    # First principle: each event's heats spread evenly across flights.
    # Cap = ceil(N_e / target_flights). Mathematically F * cap >= N_e always,
    # so a feasible distribution exists. The algorithm still honors the
    # sequential-heat-number guarantee because event queues are FIFO.
    total_heats = len(all_heats)
    target_flights = (
        max(1, math.ceil(total_heats / heats_per_flight))
        if heats_per_flight > 0 else 1
    )
    event_per_flight_cap: dict[int, int] = {
        eid: max(1, math.ceil(len(queue) / target_flights))
        for eid, queue in event_queues.items()
    }

    best_ordered: list = []
    best_score = float('-inf')

    actual_passes = min(n_passes, max(1, len(event_ids)))
    for pass_num in range(actual_passes):
        # Rotate event_ids to create different greedy starting conditions.
        rotated = event_ids[pass_num:] + event_ids[:pass_num]
        candidate = _single_pass_optimize(
            event_queues, rotated, heats_per_flight,
            gear_conflict_pairs=gear_conflict_pairs,
            event_per_flight_cap=event_per_flight_cap,
        )
        score = _score_ordering(
            candidate, heats_per_flight,
            gear_conflict_pairs=gear_conflict_pairs,
            event_per_flight_cap=event_per_flight_cap,
        )
        if score > best_score:
            best_score = score
            best_ordered = candidate

    return best_ordered


def _single_pass_optimize(event_queues: dict, event_id_order: list,
                           heats_per_flight: int,
                           gear_conflict_pairs: dict[int, set[int]] | None = None,
                           event_per_flight_cap: dict[int, int] | None = None) -> list:
    """
    Execute a single greedy pass through the event queues.

    At each step, the next unplaced heat from each event is scored and the
    highest-scoring candidate is selected. Tie-breaking prefers the event
    with the most remaining heats (encourages balanced distribution).
    """
    event_ptrs: dict[int, int] = {eid: 0 for eid in event_id_order}
    ordered: list = []
    competitor_last_heat: dict[int, int] = {}
    stand_type_last_position: dict[str, int] = {}
    # Track which flight block each event last appeared in (for recency bonus).
    event_last_block: dict[int, int] = {}
    # Count of this event's heats already placed in the current flight block,
    # used to enforce the even-distribution cap. Key: (block, event_id).
    event_heats_in_block: dict[tuple[int, int], int] = {}
    # Track (position, event_id) for the last heat of each stand_type so the
    # greedy can penalize CROSS-event same-stand-type adjacency.
    stand_type_last_event: dict[str, tuple[int, int]] = {}

    while True:
        candidates = [
            (eid, event_queues[eid][event_ptrs[eid]])
            for eid in event_id_order
            if event_ptrs[eid] < len(event_queues[eid])
        ]
        if not candidates:
            break

        current_position = len(ordered)
        remaining_counts = {
            eid: len(event_queues[eid]) - event_ptrs[eid]
            for eid in event_id_order
        }

        # Score all candidates.
        scored = [
            (
                _calculate_heat_score(
                    hd['competitors'],
                    competitor_last_heat,
                    current_position,
                    hd['event'],
                    stand_type_last_position,
                    heats_per_flight,
                    event_last_block,
                    gear_conflict_pairs=gear_conflict_pairs,
                    previous_heat_comps=ordered[-1]['competitors'] if ordered else set(),
                    event_per_flight_cap=event_per_flight_cap,
                    event_heats_in_block=event_heats_in_block,
                    stand_type_last_event=stand_type_last_event,
                ),
                remaining_counts[eid],   # tie-break: more remaining = preferred
                eid,
                hd,
            )
            for eid, hd in candidates
        ]

        best_score, _, best_eid, best_heat_data = max(scored, key=lambda x: (x[0], x[1]))

        # If every candidate is blocked by a stand conflict, re-score ignoring it.
        if best_score < 0:
            scored_nc = [
                (
                    _calculate_heat_score(
                        hd['competitors'],
                        competitor_last_heat,
                        current_position,
                        hd['event'],
                        None,  # disable stand conflict check
                        heats_per_flight,
                        event_last_block,
                        event_per_flight_cap=event_per_flight_cap,
                        event_heats_in_block=event_heats_in_block,
                        stand_type_last_event=stand_type_last_event,
                    ),
                    remaining_counts[eid],
                    eid,
                    hd,
                )
                for eid, hd in candidates
            ]
            _, _, best_eid, best_heat_data = max(scored_nc, key=lambda x: (x[0], x[1]))

        ordered.append(best_heat_data)
        event_ptrs[best_eid] += 1

        pos = len(ordered) - 1
        for comp_id in best_heat_data['competitors']:
            competitor_last_heat[comp_id] = pos
        stand_type = getattr(best_heat_data['event'], 'stand_type', None)
        if stand_type:
            stand_type_last_position[stand_type] = pos
        event_id = best_heat_data['heat'].event_id
        if stand_type and event_id is not None:
            stand_type_last_event[stand_type] = (pos, event_id)
        current_block = pos // heats_per_flight if heats_per_flight > 0 else 0
        event_last_block[event_id] = current_block
        event_heats_in_block[(current_block, event_id)] = (
            event_heats_in_block.get((current_block, event_id), 0) + 1
        )

    return ordered


def _score_ordering(ordered: list, heats_per_flight: int,
                    gear_conflict_pairs: dict[int, set[int]] | None = None,
                    event_per_flight_cap: dict[int, int] | None = None) -> float:
    """
    Compute a quality score for a complete heat ordering. Higher is better.

    Used to compare multiple greedy passes and select the best result.
    Rewards adequate competitor spacing, penalizes spacing violations,
    and gives a small bonus for event variety within each flight block.
    """
    if not ordered:
        return 0.0

    competitor_last: dict[int, int] = {}
    event_blocks_seen: dict[tuple, bool] = {}  # (block, event_id) -> seen
    total = 0.0

    for pos, hd in enumerate(ordered):
        event = hd['event']
        min_sp, target_sp = _get_spacing(event)
        block = pos // heats_per_flight if heats_per_flight > 0 else 0

        for cid in hd['competitors']:
            if cid in competitor_last:
                spacing = pos - competitor_last[cid]
                if spacing < min_sp:
                    total -= (min_sp - spacing) * 50  # heavy violation penalty
                elif spacing >= target_sp:
                    total += 20                        # target spacing bonus
                else:
                    total += spacing * 2               # linear partial bonus
            competitor_last[cid] = pos

        # Variety bonus: first time this event appears in this flight block
        block_key = (block, hd['heat'].event_id)
        if block_key not in event_blocks_seen:
            total += 10
            event_blocks_seen[block_key] = True

    # Gear adjacency penalty across the full ordering.
    # Raised from -30 per gear audit 2026-04-07 — must outweigh spacing bonus
    # so back-to-back gear-share heats are an exception, not normal output.
    if gear_conflict_pairs:
        for pos in range(1, len(ordered)):
            prev_comps = ordered[pos - 1]['competitors']
            curr_comps = ordered[pos]['competitors']
            for cid in curr_comps:
                partner_ids = gear_conflict_pairs.get(cid)
                if partner_ids:
                    overlap = partner_ids & prev_comps
                    if overlap:
                        total -= 200 * len(overlap)

    # Left-handed springboard flight constraint: at most one LH-containing
    # heat per flight block.  Only one physical LH dummy on site, so two
    # LH heats in the same flight would need simultaneous use.  Penalty must
    # outweigh typical spacing bonuses across a flight (~20 × 8 = 160 max)
    # so the optimizer always prefers spreading LH heats across flights.
    if heats_per_flight > 0:
        lh_per_block: dict[int, int] = {}
        for pos, hd in enumerate(ordered):
            if hd.get('contains_lh'):
                block = pos // heats_per_flight
                lh_per_block[block] = lh_per_block.get(block, 0) + 1
        for count in lh_per_block.values():
            if count > 1:
                total -= 1000 * (count - 1)

    # Per-event per-flight distribution penalty: mirrors the per-step cap
    # used during greedy placement so multi-pass comparison rewards the
    # pass that best spreads each event's heats across flights.
    if event_per_flight_cap and heats_per_flight > 0:
        event_heats_per_block: dict[tuple[int, int], int] = {}
        for pos, hd in enumerate(ordered):
            block = pos // heats_per_flight
            eid = hd['heat'].event_id
            event_heats_per_block[(block, eid)] = (
                event_heats_per_block.get((block, eid), 0) + 1
            )
        for (_block, eid), count in event_heats_per_block.items():
            cap = event_per_flight_cap.get(eid)
            if cap is not None and count > cap:
                total -= EVENT_FLIGHT_CAP_SCORE_PENALTY * (count - cap)

    # Same-stand-type CROSS-event adjacency penalty across the full ordering.
    # Mirrors the per-step penalty in _calculate_heat_score. Only penalizes
    # when consecutive same-stand-type heats come from DIFFERENT events
    # (e.g. Men's UH → Women's UH) — intra-event adjacency is handled by
    # competitor spacing and the distribution cap.
    prev_by_stand: dict[str, tuple[int, int]] = {}  # stand_type -> (pos, event_id)
    for pos, hd in enumerate(ordered):
        st = getattr(hd.get('event'), 'stand_type', None)
        if not st:
            continue
        eid = hd['heat'].event_id
        last = prev_by_stand.get(st)
        if last is not None:
            last_pos, last_eid = last
            if last_eid != eid:  # cross-event only
                gap = pos - last_pos
                if gap < _SAME_STAND_TYPE_MIN_GAP:
                    total -= _SAME_STAND_TYPE_PENALTY * (_SAME_STAND_TYPE_MIN_GAP - gap)
        prev_by_stand[st] = (pos, eid)

    return total


def _calculate_heat_score(competitors: set, competitor_last_heat: dict,
                           current_position: int, event: Event,
                           stand_type_last_position: dict | None,
                           heats_per_flight: int = 8,
                           event_last_block: dict | None = None,
                           gear_conflict_pairs: dict[int, set[int]] | None = None,
                           previous_heat_comps: set | None = None,
                           event_per_flight_cap: dict[int, int] | None = None,
                           event_heats_in_block: dict | None = None,
                           stand_type_last_event: dict[str, tuple[int, int]] | None = None) -> float:
    """
    Calculate a score for placing a heat at the current position.

    Higher score = better placement. Components:
    - Stand conflict enforcement (cookie_stack / standing_block mutual exclusion) → -1 if violated
    - Per-event tiered spacing (Tier 1=springboard, Tier 2=saw, Tier 3=others)
    - Rebalanced formula: min_spacing × 5 + avg_spacing × 5
    - Springboard opener bonus: +500 when at the start of a flight block
    - Hot Saw closer bonus: +300 when at the end of a flight block
    - Event recency bonus: +30 when this event hasn't appeared yet in the current block

    Args:
        competitors: Set of competitor IDs in this heat
        competitor_last_heat: Dict of competitor_id -> last heat index
        current_position: Current position in the ordered list
        event: The event this heat belongs to
        stand_type_last_position: Dict of stand_type -> last position (None = disabled)
        heats_per_flight: Flight block size for positional bonuses
        event_last_block: Dict of event_id -> last block number appeared in

    Returns:
        Score (higher is better), or -1 if blocked by stand conflict
    """
    stand_type = getattr(event, 'stand_type', None)

    # Enforce stand type conflicts (events that share physical stands).
    # _CONFLICTING_STANDS is now dict[str, set[str]] so a single stand_type
    # can have multiple physical conflicts (e.g. obstacle_pole shares Pole 2
    # with speed_climb; future events may add more shared-stand pairs).
    if stand_type and stand_type in _CONFLICTING_STANDS and stand_type_last_position is not None:
        for conflict_type in _CONFLICTING_STANDS[stand_type]:
            last_conflict = stand_type_last_position.get(conflict_type)
            if last_conflict is not None and (current_position - last_conflict) < _STAND_CONFLICT_GAP:
                return -1.0

    min_sp, target_sp = _get_spacing(event)

    if not competitors:
        return 100.0  # Empty heats can go anywhere

    min_spacing = float('inf')
    total_spacing = 0
    competitor_count = 0

    for comp_id in competitors:
        last_heat = competitor_last_heat.get(comp_id)
        if last_heat is not None:
            spacing = current_position - last_heat
            min_spacing = min(min_spacing, spacing)
            total_spacing += spacing
            competitor_count += 1

    # All competitors are new — great placement
    if competitor_count == 0:
        score = 1000.0
    elif min_spacing < min_sp:
        # Below minimum spacing — penalize but don't hard-reject
        penalty = (min_sp - min_spacing) * 100
        score = max(0.0, 50.0 - penalty)
    else:
        avg_spacing = total_spacing / competitor_count
        # Rebalanced formula (#13): equal weight to min and average spacing
        score = min_spacing * 5 + avg_spacing * 5
        if min_spacing >= target_sp:
            score += 50

    # Springboard opener bonus (#5): strongly prefer springboard at the start of every
    # flight block so each flight opens with a springboard cut (crowd favourite).
    if stand_type == 'springboard' and heats_per_flight > 0:
        if current_position % heats_per_flight == 0:
            score += 500

    # Hot Saw closer bonus (#7): Hot Saw is a dramatic crowd-pleaser — give it a bonus
    # when it would be placed as the last heat of a flight block.
    if stand_type == 'hot_saw' and heats_per_flight > 0:
        if (current_position + 1) % heats_per_flight == 0:
            score += 300

    # Event recency bonus (#11): encourage variety within each flight block by rewarding
    # placing an event that hasn't appeared yet in the current block.
    if event_last_block is not None and heats_per_flight > 0:
        current_block = current_position // heats_per_flight
        event_id = getattr(event, 'id', None)
        if event_id is not None:
            last_block = event_last_block.get(event_id)
            if last_block is None or last_block < current_block:
                score += 30

    # Gear adjacency penalty: penalize placing a heat immediately after one that
    # contains a gear-sharing partner.  This gives equipment time to be moved
    # between stands.  Penalty (-200 per conflict) — does not hard-block, but
    # is large enough to outweigh spacing bonuses.  Raised from -30 per gear
    # audit 2026-04-07 — must outweigh spacing bonus.
    if gear_conflict_pairs and previous_heat_comps:
        for comp_id in competitors:
            partner_ids = gear_conflict_pairs.get(comp_id)
            if partner_ids:
                overlap = partner_ids & previous_heat_comps
                if overlap:
                    score -= 200 * len(overlap)

    # Per-event per-flight distribution cap. Without this, heats whose
    # competitors never appear in any other event all score 1000
    # (first-appearance) and greedily stack into one flight — which is the
    # opposite of the crowd-variety first principle flights exist to serve.
    # Penalty scales with how far over the cap the placement would go and is
    # large enough to override the first-appearance bonus.
    if (event_per_flight_cap is not None and event_heats_in_block is not None
            and heats_per_flight > 0):
        event_id = getattr(event, 'id', None)
        if event_id is not None:
            current_block = current_position // heats_per_flight
            cap = event_per_flight_cap.get(event_id)
            if cap is not None:
                already = event_heats_in_block.get((current_block, event_id), 0)
                if already >= cap:
                    score -= EVENT_FLIGHT_CAP_PENALTY * (already - cap + 1)

    # Same-stand-type cross-event adjacency penalty. Back-to-back heats from
    # DIFFERENT events that share a stand_type (Men's UH → Women's UH,
    # Single Buck → Jack & Jill) reuse the same physical stands with no
    # crew-reset or crowd-variety break. Intra-event adjacency (Men's UH H1
    # → H2) is handled by competitor spacing + the distribution cap and is
    # not penalized here.
    # Penalty is smaller than the distribution cap (2000) and first-appearance
    # bonus (1000) so it shifts preference without hard-blocking when the only
    # remaining candidates are same-stand-type.
    if (stand_type and stand_type_last_event is not None):
        last_event_of_stand = stand_type_last_event.get(stand_type)
        if last_event_of_stand is not None:
            last_pos, last_eid = last_event_of_stand
            event_id = getattr(event, 'id', None)
            if event_id is not None and last_eid != event_id:
                gap = current_position - last_pos
                if gap < _SAME_STAND_TYPE_MIN_GAP:
                    score -= _SAME_STAND_TYPE_PENALTY * (_SAME_STAND_TYPE_MIN_GAP - gap)

    return score


def optimize_flight_for_ability(flight: Flight, event: Event):
    """
    Reorder heats within a flight to group by ability.

    For springboard events, competitors flagged springboard_slow_heat=True are
    consolidated into dedicated heats at the back of the flight block so that
    slow cutters do not dilute faster heats.  Heat assignments are rewritten in
    place; competitors and stand assignments are preserved — only which heat a
    competitor appears in changes.

    For non-springboard events the function is a no-op.  Predicted-time-based
    grouping (STRATHMARK) can be layered in here later.

    Args:
        flight: Flight whose heats will be reordered
        event: Event within that flight to optimise
    """
    if event.stand_type != 'springboard':
        return

    from models.competitor import ProCompetitor

    event_heats = (
        flight.heats.filter_by(event_id=event.id)
        .order_by(Heat.flight_position)
        .all()
    )
    if len(event_heats) <= 1:
        return

    max_per_heat = event.max_stands or 4

    # Collect all competitor IDs from these heats with their slow_heat flag.
    all_comp_ids: list[int] = []
    for heat in event_heats:
        all_comp_ids.extend(heat.get_competitors())

    if not all_comp_ids:
        return

    slow_flag: dict[int, bool] = {}
    for comp in ProCompetitor.query.filter(ProCompetitor.id.in_(all_comp_ids)).all():
        slow_flag[comp.id] = bool(getattr(comp, 'springboard_slow_heat', False))

    normal = [cid for cid in all_comp_ids if not slow_flag.get(cid)]
    slow = [cid for cid in all_comp_ids if slow_flag.get(cid)]

    # Rebuild: normal competitors fill the front heats, slow the back heats.
    reordered: list[list[int]] = []
    for i in range(0, len(normal), max_per_heat):
        reordered.append(normal[i:i + max_per_heat])
    for i in range(0, len(slow), max_per_heat):
        reordered.append(slow[i:i + max_per_heat])

    # If nothing changed there is nothing to write.
    flat_before = all_comp_ids
    flat_after = [cid for group in reordered for cid in group]
    if flat_before == flat_after:
        return

    # Write new compositions back to existing heat rows.
    for idx, heat in enumerate(event_heats):
        if idx < len(reordered):
            group = reordered[idx]
        else:
            group = []
        # Stand number is the competitor's position in the reordered group.
        # Written with the roster in one call: these are existing heat rows, so
        # a stand-at-a-time version would rebuild each heat's assignment rows
        # once per competitor and flush between them.
        heat.set_roster(
            'pro', group,
            {comp_id: position
             for position, comp_id in enumerate(group, start=1)},
        )


def insert_axe_throw_finals(tournament: Tournament, top_teams: list):
    """
    Insert Partnered Axe Throw finals into flights.
    One team throws per flight.

    Args:
        tournament: Tournament
        top_teams: List of top 4 team identifiers from prelims
    """
    flights = Flight.query.filter_by(tournament_id=tournament.id).order_by(Flight.flight_number).limit(4).all()

    axe_event = tournament.events.filter_by(name='Partnered Axe Throw', event_type='pro').first()

    if not axe_event or not flights:
        return

    for i, team in enumerate(top_teams[:4]):
        if i < len(flights):
            heat = Heat(
                event_id=axe_event.id,
                heat_number=100 + i,  # High number to indicate finals
                run_number=1,
                flight_id=flights[i].id
            )
            db.session.add(heat)

    db.session.commit()


def get_flight_summary(tournament: Tournament) -> list:
    """
    Get a summary of all flights for display.

    Returns:
        List of flight summaries with event breakdown
    """
    flights = Flight.query.filter_by(tournament_id=tournament.id).order_by(Flight.flight_number).all()

    summaries = []
    for flight in flights:
        heats = flight.heats.all()

        event_counts = {}
        for heat in heats:
            event_name = heat.event.display_name if heat.event else 'Unknown'
            event_counts[event_name] = event_counts.get(event_name, 0) + 1

        summaries.append({
            'flight': flight,
            'heat_count': len(heats),
            'event_counts': event_counts,
            'event_variety': len(event_counts),
            'status': flight.status
        })

    return summaries


def validate_competitor_spacing(tournament: Tournament) -> dict:
    """
    Validate that competitor spacing meets tier requirements.

    Returns:
        Dict with validation results and any violations
    """
    flights = Flight.query.filter_by(tournament_id=tournament.id).order_by(Flight.flight_number).all()

    all_heats = []
    for flight in flights:
        flight_heats = flight.heats.order_by(Heat.flight_position).all()
        all_heats.extend(flight_heats)

    competitor_appearances = {}
    violations = []

    for i, heat in enumerate(all_heats):
        min_sp, _ = _get_spacing(heat.event)
        competitors = heat.get_competitors()
        for comp_id in competitors:
            if comp_id in competitor_appearances:
                last_appearance = competitor_appearances[comp_id]
                spacing = i - last_appearance
                if spacing < min_sp:
                    violations.append({
                        'competitor_id': comp_id,
                        'heat_1': last_appearance + 1,
                        'heat_2': i + 1,
                        'spacing': spacing,
                        'required': min_sp
                    })
            competitor_appearances[comp_id] = i

    return {
        'valid': len(violations) == 0,
        'total_heats': len(all_heats),
        'violations': violations,
        'violation_count': len(violations)
    }


def find_stand_conflicts(ordered_heats: list[Heat]) -> list[dict]:
    """Return physical-stand conflicts in one proposed global heat order.

    ``ordered_heats`` must be in the actual show sequence, spanning every
    flight. The result deliberately identifies the heat pair, rather than only
    the stand types, so a mutation route can distinguish a pre-existing
    unavoidable fallback from a newly introduced conflict.
    """
    last_seen: dict[str, tuple[int, Heat]] = {}
    conflicts: list[dict] = []
    for position, heat in enumerate(ordered_heats):
        stand_type = getattr(heat.event, 'stand_type', None)
        if not stand_type:
            continue
        for conflict_type in _CONFLICTING_STANDS.get(stand_type, ()):
            previous = last_seen.get(conflict_type)
            if previous is None:
                continue
            previous_position, previous_heat = previous
            gap = position - previous_position
            if gap < _STAND_CONFLICT_GAP:
                conflicts.append({
                    'heat_ids': (previous_heat.id, heat.id),
                    'stand_types': (conflict_type, stand_type),
                    'gap': gap,
                })
        last_seen[stand_type] = (position, heat)
    return conflicts


def build_flight_audit_report(tournament: Tournament) -> dict:
    """
    Build a post-flight-construction audit report.

    Checks:
    1. Sequential heat order within each event (heats must appear in ascending
       heat_number order across the full show).
    2. Competitor spacing violations (actual gap vs tier minimum).
    3. Per-competitor spacing statistics (min, avg, max actual gaps).
    4. Event variety per flight (distinct events per flight block).
    5. Gear sharing adjacency conflicts (gear partners in back-to-back heats).
    6. Physical stand conflicts in the global show order.

    Returns a dict suitable for display in the scheduling UI and for storage
    as a JSON audit record.
    """
    flights = Flight.query.filter_by(tournament_id=tournament.id).order_by(Flight.flight_number).all()
    if not flights:
        return {'error': 'No flights built yet.'}

    # Build global ordered heat list across all flights in display order.
    # Previous: per-flight Heat.query.filter_by(flight_id=...) — N+1 across
    # flight count. Replaced with a single batch query joined by flight_id
    # then grouped in memory; same ordering guaranteed by sorting the
    # batched rows by (flight_number, flight_position).
    flight_ids = [f.id for f in flights]
    flight_lookup = {f.id: f for f in flights}
    batched_heats = (
        Heat.query
        .filter(Heat.flight_id.in_(flight_ids))
        .order_by(Heat.flight_id, Heat.flight_position)
        .all()
    )
    # Sort to flight_number-major order to match the original per-flight loop.
    batched_heats.sort(
        key=lambda h: (
            flight_lookup[h.flight_id].flight_number,
            h.flight_position if h.flight_position is not None else 0,
        )
    )
    all_heat_data = []
    for heat in batched_heats:
        flight = flight_lookup[heat.flight_id]
        all_heat_data.append({
                'heat': heat,
                'event': heat.event,
                'flight_number': flight.flight_number,
                'flight_position': heat.flight_position,
                'competitors': list(heat.get_competitors()),
            })

    stand_conflicts = find_stand_conflicts([hd['heat'] for hd in all_heat_data])

    # 1. Sequential order check (#15)
    event_last_heat_num: dict[int, int] = {}
    event_last_flight_pos: dict[int, int] = {}
    sequential_violations: list[dict] = []
    for pos, hd in enumerate(all_heat_data):
        eid = hd['heat'].event_id
        hn = hd['heat'].heat_number
        if eid in event_last_heat_num and hn < event_last_heat_num[eid]:
            sequential_violations.append({
                'event': hd['event'].display_name if hd['event'] else str(eid),
                'heat_number': hn,
                'previous_heat_number': event_last_heat_num[eid],
                'global_position': pos,
                'previous_global_position': event_last_flight_pos[eid],
                'flight': hd['flight_number'],
            })
        event_last_heat_num[eid] = hn
        event_last_flight_pos[eid] = pos

    # 2 + 3. Competitor spacing audit (#16)
    competitor_last: dict[int, int] = {}
    competitor_spacings: dict[int, list[int]] = {}
    spacing_violations: list[dict] = []

    for pos, hd in enumerate(all_heat_data):
        event = hd['event']
        min_sp, _ = _get_spacing(event)
        for cid in hd['competitors']:
            if cid in competitor_last:
                spacing = pos - competitor_last[cid]
                competitor_spacings.setdefault(cid, []).append(spacing)
                if spacing < min_sp:
                    spacing_violations.append({
                        'competitor_id': cid,
                        'position_1': competitor_last[cid],
                        'position_2': pos,
                        'spacing': spacing,
                        'required': min_sp,
                        'event': event.display_name if event else '?',
                    })
            competitor_last[cid] = pos

    # Per-competitor stats
    competitor_stats: list[dict] = []
    for cid, spacings in competitor_spacings.items():
        competitor_stats.append({
            'competitor_id': cid,
            'appearances': len(spacings) + 1,
            'min_spacing': min(spacings),
            'avg_spacing': round(sum(spacings) / len(spacings), 1),
            'max_spacing': max(spacings),
        })
    all_spacings = [s for sl in competitor_spacings.values() for s in sl]
    avg_spacing_overall = round(sum(all_spacings) / len(all_spacings), 2) if all_spacings else 0

    # 4. Event variety per flight
    variety_report: list[dict] = []
    for flight in flights:
        flight_heats = [hd for hd in all_heat_data if hd['flight_number'] == flight.flight_number]
        distinct_events = len({hd['heat'].event_id for hd in flight_heats})
        variety_report.append({
            'flight_number': flight.flight_number,
            'heat_count': len(flight_heats),
            'distinct_events': distinct_events,
        })

    # 5. Gear sharing adjacency check (#18) — warn if gear partners appear in back-to-back heats
    gear_adjacency_warnings: list[dict] = []
    for i in range(len(all_heat_data) - 1):
        curr_comps = set(all_heat_data[i]['competitors'])
        next_comps = set(all_heat_data[i + 1]['competitors'])
        overlap = curr_comps & next_comps
        if not overlap:
            continue
        # Same competitor in consecutive heats — not gear sharing but is a spacing issue
        for cid in overlap:
            gear_adjacency_warnings.append({
                'competitor_id': cid,
                'position': i,
                'next_position': i + 1,
                'type': 'back_to_back',
            })

    return {
        'total_heats': len(all_heat_data),
        'total_flights': len(flights),
        'sequential_violations': sequential_violations,
        'passes_sequential': len(sequential_violations) == 0,
        'spacing_violations': spacing_violations,
        'spacing_violation_count': len(spacing_violations),
        'passes_spacing': len(spacing_violations) == 0,
        'avg_competitor_spacing': avg_spacing_overall,
        'competitor_stats': sorted(competitor_stats, key=lambda x: x['min_spacing']),
        'variety_per_flight': variety_report,
        'gear_adjacency_warnings': gear_adjacency_warnings,
        'stand_conflicts': stand_conflicts,
        'passes_stand_conflicts': len(stand_conflicts) == 0,
    }


@serialize_sqlite_schedule_writer
def integrate_college_spillover_into_flights(
    tournament: Tournament,
    college_event_ids: list[int] | None = None,
    commit: bool = False,
    placement_mode: str | None = None,
) -> dict:
    """
    Assign selected college spillover heats into existing Saturday pro flights.

    Day-split events (config.DAY_SPLIT_EVENT_NAMES — Chokerman's Race and Speed
    Climb) contribute Run 2 only, per Missoula rules. Chokerman's Race Run 2
    heats are always placed at the end of the last flight to serve as the show
    climax — no other heats are inserted after them. Speed Climb Run 2 and
    other selected spillover events (Obstacle Pole, Standing Block Speed, etc.)
    are distributed according to `placement_mode`.

    Day-split events are auto-added to the mandatory set — operators do NOT need
    to check them explicitly on the spillover form.

    Args:
        tournament: Tournament to integrate spillover into.
        college_event_ids: Explicitly selected spillover event ids. All events
            in DAY_SPLIT_EVENT_NAMES are auto-added (Chokerman, Speed Climb).
        commit: When True, commit the transaction at the end. Defaults to False
            because historically this function flushed and left the caller to
            commit; preserving that default keeps existing call sites safe.
            Phase 1 async chain passes True at the final step.
        placement_mode: How to distribute non-Chokerman spillover heats across
            Saturday flights. When None (default), read from
            tournament.schedule_config['saturday_college_placement_mode'] so
            existing callers pick up the operator's UI choice automatically.
            - PLACEMENT_MODE_ROUNDROBIN ('roundrobin'): cycle through flights in
              flight_number order.
            - PLACEMENT_MODE_CLUSTER ('cluster'): prefer the earliest flight.
            Both modes evaluate the projected global show order. Competitor
            spacing is ranked first, then placements that add no new physical
            stand-conflict identity, then the mode preference as a deterministic
            fallback.

    Returns:
        Integration counts plus ignored_non_college_event_ids and any
        unavoidable_stand_conflicts introduced by the selected placements.
    """
    lock_tournament_schedule(tournament)

    if placement_mode is None:
        try:
            cfg = tournament.get_schedule_config() or {}
            placement_mode = cfg.get('saturday_college_placement_mode') or PLACEMENT_MODE_ROUNDROBIN
        except Exception:
            placement_mode = PLACEMENT_MODE_ROUNDROBIN
    if placement_mode not in VALID_PLACEMENT_MODES:
        logger.warning(
            'integrate_college_spillover_into_flights: unknown placement_mode %r, '
            'falling back to %r', placement_mode, PLACEMENT_MODE_ROUNDROBIN,
        )
        placement_mode = PLACEMENT_MODE_ROUNDROBIN

    requested_ids = set(int(v) for v in (college_event_ids or []))
    resolved_requested = (
        tournament.events.filter(Event.id.in_(requested_ids)).all()
        if requested_ids else []
    )
    ignored_non_college_event_ids = sorted(
        event.id for event in resolved_requested if event.event_type != 'college'
    )
    selected_events = [
        event for event in resolved_requested
        if event.event_type == 'college'
    ]

    # Auto-add every DAY_SPLIT_EVENT_NAMES event (Chokerman + Speed Climb M/F).
    # Operators never have to tick these on the UI — Run 2 is non-negotiable
    # per Missoula rules and the spec (FlightLogic.md §4.1).
    mandatory_events = tournament.events.filter(
        Event.event_type == 'college',
        Event.name.in_(list(DAY_SPLIT_EVENT_NAMES)),
    ).all()
    events_by_id = {event.id: event for event in selected_events}
    events_by_id.update((event.id, event) for event in mandatory_events)
    events = list(events_by_id.values())

    def _result(message, **values):
        result = {
            'integrated_heats': 0,
            'skipped_completed': 0,
            'events': 0,
            'ignored_non_college_event_ids': ignored_non_college_event_ids,
            'unavoidable_stand_conflicts': [],
            'message': message,
        }
        result.update(values)
        return result

    flights = (
        Flight.query
        .filter_by(tournament_id=tournament.id)
        .order_by(Flight.id)
        .with_for_update()
        .all()
    )
    flights.sort(key=lambda flight: (flight.flight_number, flight.id))
    if not flights:
        return _result('No flights available.')

    for previous_flight, flight in zip(flights, flights[1:]):
        if previous_flight.flight_number == flight.flight_number:
            raise ValueError(
                f'Tournament {tournament.id} has duplicate flight_number '
                f'{flight.flight_number} on flights {previous_flight.id} and '
                f'{flight.id}.'
            )

    last_flight = flights[-1]
    integrated = 0
    skipped_completed = 0
    per_event = 0
    flight_idx = 0
    flight_list_indexes = {flight.id: index for index, flight in enumerate(flights)}
    flight_heats = {flight.id: [] for flight in flights}
    batched_flight_heats = (
        Heat.query
        .filter(Heat.flight_id.in_(list(flight_list_indexes)))
        .all()
    )
    batched_flight_heats.sort(
        key=lambda heat: (
            flight_list_indexes[heat.flight_id],
            heat.flight_position is None,
            heat.flight_position if heat.flight_position is not None else 0,
            heat.id,
        )
    )
    for heat in batched_flight_heats:
        flight_heats[heat.flight_id].append(heat)

    for flight in flights:
        seen_positions = {}
        for heat in flight_heats[flight.id]:
            position = heat.flight_position
            if position is None:
                raise ValueError(
                    f'Flight {flight.flight_number} heat {heat.id} has missing '
                    'flight_position.'
                )
            if position <= 0:
                raise ValueError(
                    f'Flight {flight.flight_number} heat {heat.id} has '
                    f'non-positive flight_position {position}.'
                )
            if position in seen_positions:
                raise ValueError(
                    f'Flight {flight.flight_number} has duplicate '
                    f'flight_position {position} for heats '
                    f'{seen_positions[position]} and {heat.id}.'
                )
            seen_positions[position] = heat.id

    if not events:
        message = 'No selected spillover events.'
        if ignored_non_college_event_ids:
            message += (
                f' Ignored {len(ignored_non_college_event_ids)} selected '
                'non-college event ID(s).'
            )
        return _result(message)

    def _candidate_insert_index(candidate_flight, candidate_heat):
        placed_heats = flight_heats[candidate_flight.id]
        if (
            candidate_flight is not last_flight
            or _is_chokerman_run2_heat(candidate_heat)
        ):
            return len(placed_heats)
        insert_at = len(placed_heats)
        while (
            insert_at > 0
            and _is_chokerman_run2_heat(placed_heats[insert_at - 1])
            and placed_heats[insert_at - 1].status == 'pending'
        ):
            insert_at -= 1
        return insert_at

    def _projected_order(candidate_flight=None, candidate_heat=None):
        ordered = []
        for flight in flights:
            placed_heats = flight_heats[flight.id]
            if candidate_flight is flight and candidate_heat is not None:
                insert_at = _candidate_insert_index(flight, candidate_heat)
                ordered.extend(placed_heats[:insert_at])
                ordered.append(candidate_heat)
                ordered.extend(placed_heats[insert_at:])
            else:
                ordered.extend(placed_heats)
        return ordered

    validate_chokerman_closer_invariant(
        tournament,
        flights=flights,
        projected_order=_projected_order(),
    )

    def _conflict_identity(conflict):
        return tuple(conflict['heat_ids'])

    def _spacing_cost(projected_order, candidate_heat):
        candidate_position = next(
            index for index, placed_heat in enumerate(projected_order)
            if placed_heat is candidate_heat
        )
        violation_count = 0
        total_shortfall = 0
        candidate_uids = {assignment.uid for assignment in candidate_heat.assignments}
        for competitor_uid in candidate_uids:
            other_positions = [
                index for index, placed_heat in enumerate(projected_order)
                if placed_heat is not candidate_heat
                and competitor_uid in {
                    assignment.uid for assignment in placed_heat.assignments
                }
            ]
            previous = [position for position in other_positions if position < candidate_position]
            following = [position for position in other_positions if position > candidate_position]
            neighboring_positions = []
            if previous:
                neighboring_positions.append(max(previous))
            if following:
                neighboring_positions.append(min(following))
            for other_position in neighboring_positions:
                gap = abs(candidate_position - other_position)
                if gap < MIN_HEAT_SPACING:
                    violation_count += 1
                    total_shortfall += MIN_HEAT_SPACING - gap
        return violation_count, total_shortfall

    def _event_order_key(event):
        chokerman_last = 1 if event.name == "Chokerman's Race" else 0
        return (chokerman_last, event.name, event.gender or '')

    event_heat_groups = []
    for event in sorted(events, key=_event_order_key):
        if event.name in DAY_SPLIT_EVENT_NAMES:
            heats = event.heats.filter_by(run_number=2).order_by(Heat.heat_number).all()
            if not heats:
                raise FlightRebuildSafetyError(
                    f'Mandatory Saturday event {event.display_name} has no '
                    'Run 2 heats. Generate both runs before building flights.'
                )
        else:
            heats = event.heats.order_by(Heat.run_number, Heat.heat_number).all()
        if heats:
            event_heat_groups.append((event, heats))

    unplaced_heats = [
        heat
        for event, heats in event_heat_groups
        for heat in heats
        if heat.status != 'completed' and heat.flight_id is None
    ]
    nonpending_flights = [flight for flight in flights if flight.status != 'pending']
    if unplaced_heats and nonpending_flights:
        blocked = ', '.join(
            f'{flight.flight_number} ({flight.status})'
            for flight in nonpending_flights
        )
        raise FlightRebuildSafetyError(
            'Cannot integrate new spillover because all candidate flights must '
            f'be pending; blocked flight(s): {blocked}.'
        )
    nonpending_flight_heats = [
        heat for heat in batched_flight_heats if heat.status != 'pending'
    ]
    if unplaced_heats and nonpending_flight_heats:
        statuses = ', '.join(
            f'{heat.id} ({heat.status})' for heat in nonpending_flight_heats
        )
        raise FlightRebuildSafetyError(
            'Cannot integrate new spillover because all flighted heats must be '
            f'pending; blocked heat(s): {statuses}.'
        )
    initial_order = _projected_order()
    initial_conflict_ids = {
        _conflict_identity(conflict) for conflict in find_stand_conflicts(initial_order)
    }

    def _choose_flight(heat):
        nonlocal flight_idx

        if placement_mode == PLACEMENT_MODE_CLUSTER:
            preferred_flights = [
                flight for flight in flights
                if len(flight_heats[flight.id]) < DEFAULT_HEATS_PER_FLIGHT
            ]
            if not preferred_flights:
                minimum_size = min(len(flight_heats[flight.id]) for flight in flights)
                preferred_flights = [
                    flight for flight in flights
                    if len(flight_heats[flight.id]) == minimum_size
                ]
        else:
            preferred_flights = [
                flights[(flight_idx + offset) % len(flights)]
                for offset in range(len(flights))
            ]

        current_conflict_ids = {
            _conflict_identity(conflict)
            for conflict in find_stand_conflicts(_projected_order())
        }
        candidates = []
        for preference, candidate_flight in enumerate(preferred_flights):
            projected_order = _projected_order(candidate_flight, heat)
            spacing_count, spacing_shortfall = _spacing_cost(projected_order, heat)
            projected_conflicts = find_stand_conflicts(projected_order)
            new_conflicts = [
                conflict for conflict in projected_conflicts
                if _conflict_identity(conflict) not in current_conflict_ids
            ]
            stand_conflict_shortfall = sum(
                _STAND_CONFLICT_GAP - conflict['gap']
                for conflict in new_conflicts
            )
            rank = (
                spacing_count > 0,
                spacing_count,
                spacing_shortfall,
                len(new_conflicts) > 0,
                len(new_conflicts),
                stand_conflict_shortfall,
                preference,
            )
            candidates.append((rank, candidate_flight))

        target = min(candidates, key=lambda item: item[0])[1]
        if placement_mode == PLACEMENT_MODE_ROUNDROBIN:
            flight_idx = (flight_list_indexes[target.id] + 1) % len(flights)
        return target

    def _append_to_flight(heat, target):
        placed_heats = flight_heats[target.id]
        insert_at = _candidate_insert_index(target, heat)
        if insert_at < len(placed_heats):
            tail = placed_heats[insert_at:]
            tail_positions = [tail_heat.flight_position for tail_heat in tail]

            # Vacate the suffix before shifting it. The database enforces
            # unique (flight_id, flight_position), so an in-place 2 -> 3
            # update can collide with the row still occupying position 3.
            # NULL is a transaction-local staging value on both supported
            # engines and the preflight validation above guarantees these
            # positions were present, positive, and unique before mutation.
            for tail_heat in tail:
                tail_heat.flight_position = None
            db.session.flush()

            heat.flight_id = target.id
            heat.flight_position = tail_positions[0]
            for tail_heat, old_position in zip(tail, tail_positions):
                tail_heat.flight_position = old_position + 1
            placed_heats.insert(insert_at, heat)
        else:
            positions = [placed.flight_position for placed in placed_heats]
            heat.flight_id = target.id
            heat.flight_position = max(positions, default=0) + 1
            placed_heats.append(heat)

    for event, heats in event_heat_groups:
        per_event += 1
        for heat in heats:
            if heat.status == 'completed':
                skipped_completed += 1
                continue
            # Keep preexisting placement if already integrated.
            if heat.flight_id is not None:
                continue
            if event.name == "Chokerman's Race":
                # Always place at end of last flight (show climax — sealed position).
                _append_to_flight(heat, last_flight)
            else:
                _append_to_flight(heat, _choose_flight(heat))
            integrated += 1

    validate_chokerman_closer_invariant(
        tournament,
        flights=flights,
        projected_order=_projected_order(),
    )
    db.session.flush()
    unavoidable_stand_conflicts = [
        conflict for conflict in find_stand_conflicts(_projected_order())
        if _conflict_identity(conflict) not in initial_conflict_ids
    ]
    if commit:
        db.session.commit()
    message = 'College spillover heats integrated into flights.'
    if skipped_completed:
        message += f' {skipped_completed} completed heat(s) left unchanged.'
    if ignored_non_college_event_ids:
        message += (
            f' Ignored {len(ignored_non_college_event_ids)} selected '
            'non-college event ID(s).'
        )
    if unavoidable_stand_conflicts:
        message += (
            f' {len(unavoidable_stand_conflicts)} unavoidable stand conflict(s) '
            'introduced.'
        )
    return _result(
        message,
        integrated_heats=integrated,
        skipped_completed=skipped_completed,
        events=per_event,
        unavoidable_stand_conflicts=unavoidable_stand_conflicts,
    )


@serialize_sqlite_schedule_writer
def integrate_proam_relay_into_final_flight(tournament: Tournament, commit: bool = True) -> dict:
    """
    Place a single pseudo-Heat representing Pro-Am Relay in the final flight.

    Phase 4 of the flight-fixes plan. Pro-Am Relay has no snake-draft heats —
    state lives in Event.event_state / Event.payouts as a JSON blob managed
    by services.proam_relay.ProAmRelay. This function synthesises a single
    Heat row so heat-sheet rendering can show a "PRO-AM RELAY" card in the
    final flight without inventing new rendering plumbing.

    Ordering: the relay is inserted immediately before an existing college
    Chokerman Run 2 suffix. This keeps reruns safe regardless of whether relay
    placement or college spillover runs first while preserving Chokerman as
    the show climax.

    Idempotent: wipes any existing Heat rows for the relay event before
    inserting a fresh pseudo-heat, so repeated calls don't duplicate.

    Args:
        tournament: Tournament to place the relay into.
        commit: When True (default), commit the transaction. When False,
            flush only — used inside the async-chain atomic sequence.

    Returns:
        dict with:
            placed (bool), heat_id (int | None), flight_id (int | None),
            team_count (int), reason (str, only when placed=False).
    """
    from services.proam_relay import ProAmRelay

    relay_event = Event.query.filter_by(
        tournament_id=tournament.id, name='Pro-Am Relay',
    ).first()
    if not relay_event:
        return {'placed': False, 'reason': 'no_relay_event', 'team_count': 0}

    # ProAmRelay.__init__(self, tournament) — takes Tournament, not Event.
    # State is loaded into self.relay_data during __init__ (no public get_state()).
    relay = ProAmRelay(tournament)
    state = relay.relay_data or {}
    teams = state.get('teams') or []
    status = state.get('status')
    # Post-draw states: drawn → in_progress → completed. All of these mean
    # the lottery has been run and real teams exist — the relay still belongs
    # in the show schedule even after results start landing. Only 'not_drawn'
    # (or missing/empty teams) should skip placement.
    if status not in ('drawn', 'in_progress', 'completed') or not teams:
        return {'placed': False, 'reason': 'not_drawn', 'team_count': 0}

    # Lock the stable parent before any flight or heat row. PostgreSQL keeps
    # this lock through the caller's outer transaction when commit=False.
    lock_tournament_schedule(tournament)
    flights = (
        Flight.query
        .filter_by(tournament_id=tournament.id)
        .order_by(Flight.id)
        .with_for_update()
        .all()
    )
    flights.sort(key=lambda flight: (flight.flight_number, flight.id))
    existing_heats = (
        Heat.query.filter_by(event_id=relay_event.id).order_by(Heat.id).all()
    )
    completed_heat = next((heat for heat in existing_heats if heat.status == 'completed'), None)
    if completed_heat is not None:
        # The pseudo-heat is still a heat-sheet record. Re-integrating
        # spillover after the relay ran must not erase its published position.
        return {
            'placed': True,
            'heat_id': completed_heat.id,
            'flight_id': completed_heat.flight_id,
            'team_count': len(teams),
            'preserved_history': True,
        }

    if not flights:
        return {'placed': False, 'reason': 'no_flights', 'team_count': len(teams)}

    flight_indexes = {
        flight.id: index for index, flight in enumerate(flights)
    }
    flighted_heats = Heat.query.filter(
        Heat.flight_id.in_(list(flight_indexes))
    ).all()
    flighted_heats.sort(
        key=lambda heat: (
            flight_indexes[heat.flight_id],
            heat.flight_position is None,
            heat.flight_position if heat.flight_position is not None else 0,
            heat.id,
        )
    )

    nonpending_flights = [flight for flight in flights if flight.status != 'pending']
    if nonpending_flights:
        blocked = ', '.join(
            f'{flight.flight_number} ({flight.status})'
            for flight in nonpending_flights
        )
        raise FlightRebuildSafetyError(
            'Cannot integrate Pro-Am Relay because all flights must be pending; '
            f'blocked flight(s): {blocked}.'
        )

    nonpending_heats = [heat for heat in flighted_heats if heat.status != 'pending']
    if nonpending_heats:
        blocked = ', '.join(
            f'{heat.id} ({heat.status})' for heat in nonpending_heats
        )
        raise FlightRebuildSafetyError(
            'Cannot integrate Pro-Am Relay because all flighted heats must be '
            f'pending; blocked heat(s): {blocked}.'
        )

    # Ignore existing relay rows while validating the underlying show order.
    # This permits a pending relay left after Chokerman by an older build to be
    # repaired, but still rejects any non-relay heat that breaks the closer.
    projected_without_relay = [
        heat for heat in flighted_heats if heat.event_id != relay_event.id
    ]
    validate_chokerman_closer_invariant(
        tournament,
        flights=flights,
        projected_order=projected_without_relay,
    )

    # Idempotency: wipe any existing pending relay Heat rows and assignments.
    existing_heat_ids = [heat.id for heat in existing_heats]
    if existing_heat_ids:
        HeatAssignment.query.filter(
            HeatAssignment.heat_id.in_(existing_heat_ids),
        ).delete(synchronize_session='fetch')
        Heat.query.filter(Heat.id.in_(existing_heat_ids)).delete(
            synchronize_session='fetch',
        )
        db.session.flush()

    last_flight = flights[-1]
    final_flight_heats = [
        heat for heat in projected_without_relay
        if heat.flight_id == last_flight.id
    ]
    insert_index = next(
        (
            index for index, existing_heat in enumerate(final_flight_heats)
            if _is_chokerman_run2_heat(existing_heat)
        ),
        len(final_flight_heats),
    )

    # Clear occupied slots before shifting the suffix. The database enforces
    # unique (flight_id, flight_position), so an in-place 3 -> 4 update can
    # collide with the row still occupying position 4. NULL is intentionally
    # allowed as a transaction-local staging value on both supported engines.
    for placed_heat in final_flight_heats:
        placed_heat.flight_position = None
    db.session.flush()

    heat = Heat(
        event_id=relay_event.id,
        heat_number=1,
        run_number=1,
        flight_id=last_flight.id,
        status='pending',
    )
    # An empty roster, written the same way every other roster in this module
    # is written. Nothing to resolve, so the type argument never reaches a
    # query; it is passed because set_roster's signature asks for it and
    # guessing would be worse than reading it off the event.
    heat.set_roster(relay_event.event_type, [], {})
    db.session.add(heat)

    resequenced_final_flight = list(final_flight_heats)
    resequenced_final_flight.insert(insert_index, heat)
    for position, placed_heat in enumerate(resequenced_final_flight, start=1):
        placed_heat.flight_position = position

    projected_order = [
        placed_heat for placed_heat in projected_without_relay
        if placed_heat.flight_id != last_flight.id
    ]
    projected_order.extend(resequenced_final_flight)
    validate_chokerman_closer_invariant(
        tournament,
        flights=flights,
        projected_order=projected_order,
    )

    if commit:
        db.session.commit()
    else:
        db.session.flush()

    return {
        'placed': True,
        'heat_id': heat.id,
        'flight_id': last_flight.id,
        'team_count': len(teams),
    }


# ---------------------------------------------------------------------------
# FlightBuilder class — thin, testable wrapper around the module functions (#12)
# ---------------------------------------------------------------------------

class FlightBuilder:
    """Object-oriented façade for flight building operations.

    Wraps the module-level functions so callers can:
    - Inject a tournament once and call individual steps cleanly.
    - Subclass or mock for unit testing without touching the DB.

    Example::

        fb = FlightBuilder(tournament)
        fb.build(num_flights=5)
        result = fb.integrate_spillover([101, 102])
    """

    def __init__(self, tournament: Tournament):
        self.tournament = tournament

    def build(self, num_flights: int = None) -> int:
        """Build pro flights. Returns number of flights created."""
        logger.info('FlightBuilder.build tournament_id=%s num_flights=%s',
                    self.tournament.id, num_flights)
        return build_pro_flights(self.tournament, num_flights=num_flights)

    def integrate_spillover(self, saturday_college_event_ids: list[int]) -> dict:
        """Integrate college spillover heats into existing Saturday flights."""
        logger.info('FlightBuilder.integrate_spillover tournament_id=%s events=%s',
                    self.tournament.id, saturday_college_event_ids)
        return integrate_college_spillover_into_flights(
            self.tournament, saturday_college_event_ids
        )

    def spacing(self, event) -> tuple[int, int]:
        """Return (min_spacing, target_spacing) for the given event's stand type."""
        return _get_spacing(event)
