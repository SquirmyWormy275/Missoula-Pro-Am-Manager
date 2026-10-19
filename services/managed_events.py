"""Opt-in process adapter and durable projection for delegated synthetic events.

No STRATHMARK import here: Missoula's retained 2.x dependency stays intact.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import uuid
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import quote, urlsplit

from flask import current_app
from sqlalchemy.exc import IntegrityError

from database import db
from models import Event, EventResult, Heat, User
from models.managed_event import (
    ManagedAcceptance,
    ManagedAttemptRevision,
    ManagedCareerDelivery,
    ManagedCommand,
    ManagedEventLink,
    ManagedFieldLink,
)

CONTRACT = 'strathmark.managed-event.v1'


class ManagedEventError(ValueError):
    pass


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def assert_host_owned(event):
    if getattr(event, 'operation_mode', 'host') == 'managed':
        raise ManagedEventError('This managed event must be changed through STRATHMARK operations.')


def assert_host_scratch_allowed(competitor, tournament):
    from models import CollegeCompetitor

    kind = 'college' if isinstance(competitor, CollegeCompetitor) else 'pro'
    for link, event in (db.session.query(ManagedEventLink, Event)
                        .join(Event, Event.id == ManagedEventLink.event_id)
                        .filter(Event.tournament_id == tournament.id, Event.event_type == kind).all()):
        if competitor.id in json.loads(link.mapping_json).values():
            raise ManagedEventError('Use STRATHMARK managed outcomes or roster revision before scratching this competitor.')


def authorize(user, event):
    if not user or not user.is_active or not user.is_judge or (user.tournament_id is not None and user.tournament_id != event.tournament_id):
        raise ManagedEventError('Managed operation forbidden for this operator/tournament.')


def workspace_url(event):
    """Optional navigation to the same private authority; no token in the URL."""
    origin = current_app.config.get('MANAGED_WORKSPACE_ORIGIN', '').rstrip('/')
    if not origin or not event.managed_operation:
        return None
    parsed = urlsplit(origin)
    if parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment or not parsed.hostname or (parsed.scheme != 'https' and not (parsed.scheme == 'http' and parsed.hostname in {'127.0.0.1', 'localhost', '::1'})):
        raise ManagedEventError('Shared workspace requires an explicit HTTPS or loopback origin without credentials or path.')
    return origin + '/events/' + quote(event.managed_operation.module_event_id, safe='')


def _configured_runtime():
    if not current_app.config.get('MANAGED_EVENTS_ENABLED', False):
        raise ManagedEventError('Managed events are disabled. Existing local show operations remain available.')
    executable = Path(current_app.config.get('MANAGED_EVENTS_PYTHON', ''))
    database = Path(current_app.config.get('MANAGED_EVENTS_DATABASE', ''))
    if not executable.is_absolute() or not executable.is_file() or not database.is_absolute():
        raise ManagedEventError('Configure an explicit installed STRATHMARK interpreter and private authority database.')
    return executable, database


def _runtime_environment(database):
    # The local OS is this transport's trust boundary. No provider credentials,
    # Python path, proxies or live database URL enter the numerical process.
    environment = {key: os.environ[key] for key in ('PATH', 'SYSTEMROOT', 'TEMP', 'TMP', 'LANG') if key in os.environ}
    isolated = database.parent / (database.name + '.numeric-isolation')
    environment.update(STRATHMARK_TEST_DB='1', STRATHMARK_DB_PATH=str(isolated.with_suffix('.v2.sqlite3')),
                       STRATHMARK_V3_DB_PATH=str(isolated.with_suffix('.v3.sqlite3')))
    return environment


def _call(user, link, request):
    executable, database = _configured_runtime()
    envelope = {'principal': {'actor_id': user.shadow_actor_id, 'shows': [link.show_id],
                              'role': 'director' if user.is_admin else 'judge'}, 'request': request}
    environment = _runtime_environment(database)
    try:
        completed = subprocess.run([str(executable), '-I', '-m', 'strathmark.events.rpc', '--database', str(database)],
                                   input=canonical(envelope), text=True, capture_output=True, timeout=45, env=environment)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ManagedEventError('STRATHMARK unavailable or response uncertain; retry the retained request identity.') from exc
    if len(completed.stdout) > 16 * 1024 * 1024:
        raise ManagedEventError('STRATHMARK response exceeded the contract bound.')
    try:
        response = json.loads(completed.stdout)
    except (ValueError, TypeError) as exc:
        raise ManagedEventError('Invalid STRATHMARK response; retained request requires recovery.') from exc
    if not isinstance(response, dict):
        raise ManagedEventError('Invalid STRATHMARK response object; retained request requires recovery.')
    if completed.returncode or not response.get('ok'):
        raise ManagedEventError(str(response.get('error', 'STRATHMARK command refused.')))
    result = response.get('result')
    if not isinstance(result, dict):
        raise ManagedEventError('Invalid STRATHMARK result response; retained request requires recovery.')
    if result.get('schema_version') != CONTRACT:
        raise ManagedEventError('Incompatible managed-event component; no fallback is permitted.')
    return result


def guard_managed_awards(events, user):
    """Fence all selected source events before taking host write locks."""
    from services.managed_source_lock import HeldSourceExport, SourceLockError

    events = [event for event in events if event.managed_operation is not None]
    if not events:
        return None
    for event in events:
        authorize(user, event)
    event_ids = sorted({event.managed_operation.module_event_id for event in events})
    session = db.session()
    existing = session.info.get('managed_source_fence')
    if existing is not None:
        existing.check_alive()
        if set(event_ids) <= set(existing.exports):
            return existing
        raise ManagedEventError('Select all managed award events before beginning the host transaction.')
    executable, database = _configured_runtime()
    principal = {'actor_id': user.shadow_actor_id,
                 'shows': sorted({event.managed_operation.show_id for event in events}),
                 'role': 'director' if user.is_admin else 'judge'}
    try:
        guard = HeldSourceExport(executable, database, _runtime_environment(database), principal,
                                 event_ids, failure_type=ManagedEventError)
    except SourceLockError as exc:
        raise ManagedEventError(str(exc)) from exc
    try:
        for event in events:
            link = event.managed_operation
            exported = guard.exports[link.module_event_id]
            if exported.get('schema_version') != CONTRACT or exported.get('event_id') != link.module_event_id or exported.get('show_id') != link.show_id:
                raise ManagedEventError('Source fence returned a different event identity.')
            _validate_host_profile(event, guard.states[link.module_event_id]['config'])
        guard.bind_transaction(session)
        session.info['managed_source_fence'] = guard
        return guard
    except Exception:
        guard.close(False)
        raise


def bind_event(event, user, config):
    """Serialize freshness and delegation with the host's schedule writers."""
    from services.flight_builder import lock_tournament_schedule, sqlite_schedule_writer_guard

    authorize(user, event)
    _configured_runtime()
    with sqlite_schedule_writer_guard(event.tournament_id):
        lock_tournament_schedule(event.tournament_id)
        db.session.query(Event).filter_by(id=event.id).populate_existing().with_for_update().one()
        _lock_host_context(event)
        db.session.expire(event, ['managed_operation'])
        return _bind_event_locked(event, user, config)


def _bind_event_locked(event, user, config):
    """Bind an existing show's active entries; never trust a name-based mapping."""
    authorize(user, event)
    _configured_runtime()
    _validate_host_profile(event, config)
    link = event.managed_operation
    pending_creation = link is not None and link.state_json is None and link.accepted_revision == 0
    if (event.operation_mode != 'host' and not pending_creation) or event.handicap_authority_mode == 'shadow' or not event.is_handicap or event.stand_type not in {'underhand', 'standing_block'}:
        raise ManagedEventError('Only an unlinked SB/UH handicap event may opt into managed mode.')
    if event.is_partnered or event.requires_dual_runs or event.requires_triple_runs or event.is_finalized or event.heats.count() or event.results.filter(EventResult.status != 'pending').first():
        raise ManagedEventError('Managed v1 requires a fresh, single-run individual event.')
    _validate_show_wood(event, config)
    mapping = _host_roster(event, config, assign_gear=True)
    _reconcile_initial_results(event, mapping)
    expected_event = event.shadow_event_occurrence_id + ':managed'
    if config['event_id'] != expected_event or config['tournament_id'] != event.tournament.shadow_tournament_id or config['discipline'] != {'underhand': 'UH', 'standing_block': 'SB'}[event.stand_type] or config['publisher'] != 'host':
        raise ManagedEventError('Source event/tournament/discipline/publisher binding differs from this show.')
    if config.get('synthetic') is not True:
        raise ManagedEventError('Live managed-event qualification is not enabled.')
    if len(config['stands']) > (event.max_stands or 5):
        raise ManagedEventError('Managed capacity exceeds the configured show resource.')
    if config.get('timer_policy') != 'dual_mean':
        raise ManagedEventError('Missoula requires its documented dual timer policy.')
    if link is None:
        link = ManagedEventLink(event=event, module_event_id=expected_event, show_id=config['show_id'], mapping_json=canonical(mapping))
        db.session.add(link)
        db.session.commit()
    elif link.module_event_id != expected_event or link.show_id != config['show_id']:
        raise ManagedEventError('A pending creation must retain its original source identity.')
    # A corrected initial configuration gets a new durable request. Source
    # create is insert-only: an earlier ambiguous success cannot be overwritten.
    # Keep the original mapping until a source receipt identifies the winner.
    return submit(event, user, {'operation': 'create', 'request_id': str(uuid.uuid4()), 'editor': 'missoula', 'payload': config}, _initial_binding=True)


def _host_roster(event, config, *, assign_gear=False, create_results=True):
    from services.heat_generator import (
        HeatGenerationSafetyError,
        _competitors_share_gear_for_event,
        _get_event_competitors,
        _get_tournament_events,
        _validate_event_gear_declarations,
    )

    roster = config.get('roster')
    if not isinstance(roster, list) or any(not isinstance(row, dict) for row in roster):
        raise ManagedEventError('Managed roster must be an array of entry objects.')
    mapping = {}
    for row in roster:
        entry_id, host_ref = row.get('entry_id'), row.get('host_ref')
        if (not isinstance(entry_id, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:/-]{0,223}', entry_id)
                or entry_id in mapping or not isinstance(host_ref, str) or len(host_ref) > 224
                or not re.fullmatch(re.escape(event.event_type) + r':[1-9][0-9]*', host_ref)):
            raise ManagedEventError('Use unique stable entry IDs and canonical pro:<id>/college:<id> host references.')
        mapping[entry_id] = int(host_ref.split(':', 1)[1])
    entrants = _get_event_competitors(event, refresh=True, create_results=create_results)
    if len(set(mapping.values())) != len(mapping) or set(mapping.values()) != {r['id'] for r in entrants}:
        raise ManagedEventError('Managed roster must account for every active entered competitor exactly once.')
    gender_by_id = {r['id']: r['gender'] for r in entrants}
    for row in config['roster']:
        if row['host_ref'] != f"{event.event_type}:{mapping[row['entry_id']]}" or row['gender'] != gender_by_id[mapping[row['entry_id']]]:
            raise ManagedEventError('Mismatched host identity or division mapping.')
    # The retained helper's cache is scoped to one generation operation, not a
    # reusable ORM snapshot across these separate host requests/databases.
    _get_tournament_events._cache = {}
    try:
        try:
            _validate_event_gear_declarations(event, entrants)
        except HeatGenerationSafetyError as exc:
            raise ManagedEventError(str(exc)) from exc
        groups = {r['id']: r['id'] for r in entrants}
        def root(value):
            while groups[value] != value:
                value = groups[value]
            return value
        for i, first in enumerate(entrants):
            for second in entrants[i+1:]:
                if _competitors_share_gear_for_event(first, second, event):
                    groups[root(second['id'])] = root(first['id'])
        for row in config['roster']:
            comp_id = mapping[row['entry_id']]
            family = [other for other in groups if root(other) == root(comp_id)]
            gear = f'gear:{min(family)}' if len(family) > 1 else None
            if assign_gear:
                row['gear_group'] = gear
            elif row['gear_group'] != gear:
                raise ManagedEventError('Host gear declarations changed; revise unissued source preparation.')
    finally:
        _get_tournament_events._cache = {}
    return mapping


def _validate_show_wood(event, config):
    from models.wood_config import WoodConfig
    from services.strathmark_sync import (
        _STAND_TYPE_TO_WOOD_FRAGMENT,
        _wood_config_key,
    )

    fragment = _STAND_TYPE_TO_WOOD_FRAGMENT.get(event.stand_type)
    if fragment is None:
        raise ManagedEventError('Unsupported show wood category for this managed event.')
    key = f'block_{fragment}_{event.event_type}'
    gender_key = _wood_config_key(event.stand_type, event.event_type, event.gender) or key
    wood = WoodConfig.query.filter_by(tournament_id=event.tournament_id, config_key=gender_key).populate_existing().first()
    if wood is None:
        wood = WoodConfig.query.filter_by(tournament_id=event.tournament_id, config_key=key).populate_existing().first()
    if wood is None:
        raise ManagedEventError('Configure the show wood before linking or preparing a managed event.')
    try:
        if wood.size_value is None or wood.size_unit not in {'in', 'mm'}:
            raise ValueError('missing size or unsupported unit')
        diameter = Decimal(str(wood.size_value)) * (Decimal('25.4') if wood.size_unit == 'in' else Decimal(1))
        supplied = Decimal(str(config['wood']['diameter_mm']))
        if not diameter.is_finite() or diameter <= 0 or not supplied.is_finite() or supplied <= 0:
            raise ValueError('nonpositive or nonfinite size')
    except (InvalidOperation, KeyError, TypeError, ValueError) as exc:
        raise ManagedEventError('Configure a finite positive show wood size and supported unit before managed operations.') from exc
    if str(config['wood']['species']).casefold() != str(wood.species).casefold() or supplied != diameter:
        raise ManagedEventError('Managed wood differs from the configured show wood; prepare a reviewed revision.')
    _validate_physical_stands(event, config)


def _validate_physical_stands(event, config):
    try:
        stands = [int(s) for s in config['stands']]
    except (TypeError, ValueError) as exc:
        raise ManagedEventError('Physical integer stands required.') from exc
    if len(set(stands)) != len(stands) or any(str(n) != str(raw) for n, raw in zip(stands, config['stands'])):
        raise ManagedEventError('Physical stands require distinct canonical integer identifiers.')
    if len(stands) > (event.max_stands or 5) or any(s < 1 or s > (event.max_stands or 5) for s in stands):
        raise ManagedEventError('Stand outside configured show resources.')


def _validate_host_profile(event, config):
    # The show scope is persisted before source create can validate the whole
    # config. Enforce the existing component identity grammar at that boundary.
    if not isinstance(config, dict) or not isinstance(config.get('show_id'), str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:/-]{0,223}', config['show_id']):
        raise ManagedEventError('A valid explicit managed show identity is required before binding.')
    if (config['event_id'] != event.shadow_event_occurrence_id + ':managed' or
        config['tournament_id'] != event.tournament.shadow_tournament_id or
        config['discipline'] != {'underhand': 'UH', 'standing_block': 'SB'}.get(event.stand_type) or
        config['format'] != 'handicap' or config['timer_policy'] != 'dual_mean' or
        config['publisher'] != 'host' or config.get('synthetic') is not True):
        raise ManagedEventError('Source configuration differs from this host event or its dual timer policy.')
    _validate_physical_stands(event, config)


def submit(event, user, request, *, _initial_binding=False):
    """Commit exact local intent before crossing the process/response boundary."""
    authorize(user, event)
    link = event.managed_operation
    if not link:
        raise ManagedEventError('Managed event is not linked.')
    request = dict(request)
    if request.get('operation') == 'create' and db.session().info.get('managed_source_fence') is not None:
        raise ManagedEventError('Finish the source-fenced host transaction before creating another event.')
    request['event_id'] = link.module_event_id
    if request['operation'] in {'read', 'export'}:
        if request['operation'] == 'export':
            observed = _call(user, link, {'operation': 'read', 'event_id': link.module_event_id})
            _project_latest(event, observed)
            db.session.commit()
        result = _call(user, link, request)
        if request['operation'] == 'read':
            _project_latest(event, result)
            db.session.commit()
        elif result['event_revision'] != observed['revision']:
            raise ManagedEventError('Source changed during export; refresh and retry the current revision.')
        return result
    request_id = request.get('request_id')
    if not isinstance(request_id, str) or not 1 <= len(request_id) <= 224:
        raise ManagedEventError('Stable request identity required.')
    saved = ManagedCommand.query.filter_by(event_id=event.id, actor_id=user.id, request_id=request_id).first()
    if request['operation'] == 'create' and saved is None and not _initial_binding:
        raise ManagedEventError('Create through the validated event binding form.')
    binding = digest(request)
    if saved:
        if saved.payload_digest != binding:
            raise ManagedEventError('Managed request identity conflicts with a different payload.')
        if saved.status == 'accepted':
            return json.loads(saved.response_json)
        if saved.status == 'conflicting':
            raise ManagedEventError('This initial request does not own the accepted source creation; its conflict evidence is retained.')
    already_committed = False
    if saved:
        # A later host edit must not prevent recovery of a source command that
        # already committed. Read verified source audit before allowing its
        # exact retry to bypass present-day preparation checks.
        try:
            observed = _call(user, link, {'operation': 'read', 'event_id': link.module_event_id})
        except ManagedEventError as exc:
            if request['operation'] != 'create' or str(exc) != 'event_not_found':
                raise
        else:
            already_committed = any(item['actor_id'] == user.shadow_actor_id and
                                    item['request_id'] == request_id and item['action'] == request['operation']
                                    for item in observed['audit'])
            if not already_committed and request['operation'] != 'create' and request.get('expected_revision') != observed['revision']:
                raise ManagedEventError('Stale uncommitted request; refresh and prepare a new command identity.')
            if already_committed:
                _project_latest(event, observed)
                db.session.commit()
    if not already_committed:
        if request['operation'] == 'configure':
            candidate = request['payload']['config']
            _validate_host_profile(event, candidate)
            _validate_show_wood(event, candidate)
            _host_roster(event, candidate, create_results=False)
        if request['operation'] in {'fields', 'forecast', 'calculate', 'review', 'issue'} and link.state_json:
            config = json.loads(link.state_json)['config']
            _validate_host_profile(event, config)
            _validate_show_wood(event, config)
            _host_roster(event, config, create_results=False)
        if request['operation'] in {'fields', 'calculate', 'review', 'issue', 'outcomes'}:
            validate_host_fields(event)
    if saved is None:
        saved = ManagedCommand(event_id=event.id, actor_id=user.id, request_id=request_id,
                               payload_json=canonical(request), payload_digest=binding)
        db.session.add(saved)
        try:
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            winner = ManagedCommand.query.filter_by(event_id=event.id, actor_id=user.id, request_id=request_id).first()
            if winner is None:
                raise
            # A concurrent request won the unique insert. Re-enter the normal
            # digest/status/audit path; exact retries recover, changes conflict.
            return submit(event, user, request, _initial_binding=_initial_binding)
    if request['operation'] == 'create' and not already_committed:
        # Durable link/intent commits intentionally release earlier locks.
        # Reacquire and revalidate after the last commit, then hold the real
        # host facts across source creation and host projection/commit.
        from services.flight_builder import lock_tournament_schedule
        from services.managed_source_lock import HostSourceOrder, SourceLockError

        _, database = _configured_runtime()
        try:
            with HostSourceOrder(database):
                try:
                    lock_tournament_schedule(event.tournament_id)
                    _locked_link(event)
                    candidate = json.loads(saved.payload_json)['payload']
                    _validate_host_profile(event, candidate)
                    _validate_show_wood(event, candidate)
                    mapping = _host_roster(event, candidate)
                    _reconcile_initial_results(event, mapping)
                    return _finish_command(event, user, link, saved)
                except BaseException:
                    # Release host locks before releasing the outer order gate.
                    db.session.rollback()
                    raise
        except SourceLockError as exc:
            raise ManagedEventError(str(exc)) from exc
    return _finish_command(event, user, link, saved)


def _finish_command(event, user, link, saved):
    result = _call(user, link, json.loads(saved.payload_json))
    _project_latest(event, result)
    saved.status, saved.response_json = 'accepted', canonical(result)
    db.session.commit()
    return result


def _locked_link(event):
    """Reload under the parent lock; an earlier ORM read is not current state."""
    db.session.query(Event).filter_by(id=event.id).populate_existing().with_for_update().one()
    _lock_host_context(event)
    return (ManagedEventLink.query.filter_by(event_id=event.id)
            .populate_existing().with_for_update().one())


def _lock_host_context(event):
    """Fence even wood/gear writers that do not take the tournament lock.

    PostgreSQL holds the underlying rows through commit; the parent lock also
    protects new foreign-key insertions. SQLite needs a real database writer
    reservation, since SELECT FOR UPDATE and the in-process schedule guard
    cannot serialize an unrelated connection's Woodboss/gear transaction.
    """
    from models import CollegeCompetitor, ProCompetitor, Tournament, WoodConfig

    if db.session.get_bind().dialect.name == 'sqlite':
        db.session.execute(Tournament.__table__.update().where(
            Tournament.id == event.tournament_id).values(id=Tournament.id))
    for model in (WoodConfig, ProCompetitor, CollegeCompetitor):
        (model.query.filter_by(tournament_id=event.tournament_id).order_by(model.id)
         .populate_existing().with_for_update().all())


def _validate_host_context(event, state, mapping):
    """Check fresh registration and show facts while holding the host lock."""
    config = state['config']
    _validate_host_profile(event, config)
    _validate_show_wood(event, config)
    if _host_roster(event, config) != mapping:
        raise ManagedEventError('Refresh the managed entry mapping before accepting this revision.')


def _has_result_history(row):
    recorded = ('result_value', 'run1_value', 'run2_value', 'run3_value', 'best_run',
                't1_run1', 't2_run1', 't1_run2', 't2_run2', 'tiebreak_value',
                'predicted_time', 'mark_assigned_at', 'final_position', 'status_reason', 'partner_name')
    return (row.status != 'pending' or any(getattr(row, name) is not None for name in recorded)
            or row.handicap_factor or row.points_awarded or row.payout_amount
            or row.payout_settled or row.throwoff_pending or row.is_flagged)


def _reconcile_initial_results(event, mapping):
    rows = event.results.populate_existing().with_for_update().all()
    if any(row.competitor_type != event.event_type or _has_result_history(row) for row in rows):
        raise ManagedEventError('Managed binding requires fresh pending results without recorded history.')
    _reconcile_pending_results(event, {str(row.id): row.competitor_id for row in rows}, mapping)


def _reconcile_pending_results(event, old_mapping, mapping):
    """An unissued roster replacement may remove only unused placeholder rows."""
    removed = set(old_mapping.values()) - set(mapping.values())
    rows = (EventResult.query.filter_by(event_id=event.id, competitor_type=event.event_type)
            .filter(EventResult.competitor_id.in_(removed)).populate_existing().with_for_update().all())
    for row in rows:
        if _has_result_history(row):
            raise ManagedEventError('Cannot discard host result history while replacing the managed roster.')
    for row in rows:
        db.session.delete(row)
    db.session.flush()


def _project_latest(event, result):
    from services.flight_builder import lock_tournament_schedule

    # Keep the whole-show schedule lock order, then serialize this event's
    # projection with acceptance. Delayed responses retain their own receipts
    # while the cache and physical field projection move only forwards.
    lock_tournament_schedule(event.tournament_id)
    link = _locked_link(event)
    if result.get('event_id') != link.module_event_id or result.get('show_id') != link.show_id:
        raise ManagedEventError('Managed response identity mismatch.')
    if 'config' in result:
        _validate_host_profile(event, result['config'])
        _validate_show_wood(event, result['config'])
    current = json.loads(link.state_json) if link.state_json else None
    if current and result['revision'] == current['revision'] and canonical(result) != canonical(current):
        raise ManagedEventError('Changed managed state for an existing revision.')
    if not current or result['revision'] > current['revision']:
        # A shared editor can prepare newer fields without changing its config.
        # Those fields must still describe current host enrollment/division/gear.
        # Receipt-only forecast recovery with no fields must remain possible
        # after registration edits; it publishes no physical heat to schedule.
        has_fields = any(round_['fields'] for round_ in result['rounds'])
        roster_changed = ('config' in result and current and
                          result['config']['roster'] != current['config']['roster'])
        mapping = (_host_roster(event, result['config']) if 'config' in result and
                   (not current or has_fields or roster_changed) else None)
        if not current:
            # A recovered original request can differ from a subsequently
            # refused candidate. Bind the source's creating actor/request and
            # exact supplied config before adopting its accepted mapping.
            creation = result['audit'][0]
            owned = (ManagedCommand.query.join(User, User.id == ManagedCommand.actor_id)
                     .filter(ManagedCommand.event_id == event.id,
                             ManagedCommand.request_id == creation['request_id'],
                             User.shadow_actor_id == creation['actor_id']).one_or_none())
            original = json.loads(owned.payload_json) if owned else {}
            if creation['action'] != 'create' or original.get('operation') != 'create' or canonical(original.get('payload')) != canonical(result['config']):
                raise ManagedEventError('Initial source receipt does not match an owned creation request.')
            link.mapping_json = canonical({row['entry_id']: int(row['host_ref'].split(':')[-1])
                                           for row in result['config']['roster']})
            for candidate in ManagedCommand.query.filter_by(event_id=event.id, status='pending').all():
                if candidate.id != owned.id and json.loads(candidate.payload_json).get('operation') == 'create':
                    candidate.status = 'conflicting'
                    candidate.response_json = canonical({'error': 'initial_creation_superseded',
                                                          'source_creation_request_id': creation['request_id']})
        elif 'config' in result and result['config']['roster'] != current['config']['roster']:
            _reconcile_pending_results(event, json.loads(link.mapping_json), mapping)
            link.mapping_json = canonical(mapping)
        elif mapping is not None and mapping != json.loads(link.mapping_json):
            raise ManagedEventError('Source roster differs from the retained host entry mapping.')
        project_fields(event, result)
        link.state_json = canonical(result)


def validate_host_fields(event):
    mapping = json.loads(event.managed_operation.mapping_json)
    state = json.loads(event.managed_operation.state_json) if event.managed_operation.state_json else None
    if not state:
        return
    for round_ in state['rounds']:
        for field in round_['fields']:
            link = ManagedFieldLink.query.filter_by(event_id=event.id, module_heat_id=field['heat_id']).first()
            if not link:
                raise ManagedEventError('Missing host field mapping; recover the original command first.')
            heat = db.session.get(Heat, link.heat_id)
            expected = [mapping[e] for e in field['entries']]
            if heat.get_competitors() != expected or [str(heat.get_stand_for_competitor(c)) for c in expected] != field['stands']:
                raise ManagedEventError('Physical host field changed; prepare a revised unissued module field.')


def project_fields(event, state):
    """Publish module readiness as normal host heats; the host schedules them."""
    from services.flight_builder import lock_tournament_schedule

    lock_tournament_schedule(event.tournament_id)
    mapping = json.loads(event.managed_operation.mapping_json)
    active_ids = {field['heat_id'] for round_ in state['rounds'] for field in round_['fields']}
    for obsolete in ManagedFieldLink.query.filter_by(event_id=event.id).all():
        if obsolete.module_heat_id not in active_ids:
            heat = db.session.get(Heat, obsolete.heat_id)
            if obsolete.issue_id or heat.status == 'completed' or heat.flight_id is not None:
                raise ManagedEventError('Cannot discard an issued, completed or host-scheduled dependent field; reconcile its placement first.')
            db.session.delete(obsolete)
            db.session.flush()
            db.session.delete(heat)
            db.session.flush()
    for round_ in state['rounds']:
        for field in round_['fields']:
            link = ManagedFieldLink.query.filter_by(event_id=event.id, module_heat_id=field['heat_id']).first()
            if link is None:
                number = max((h.heat_number for h in event.heats.all()), default=0) + 1
                heat = Heat(event_id=event.id, heat_number=number, run_number=1, status='pending')
                db.session.add(heat)
                db.session.flush()
                link = ManagedFieldLink(event_id=event.id, module_heat_id=field['heat_id'], heat_id=heat.id, field_digest=field['field_digest'])
                db.session.add(link)
            else:
                heat = db.session.get(Heat, link.heat_id)
                if link.issue_id and link.field_digest != field['field_digest']:
                    raise ManagedEventError('Issued host field cannot change.')
                if heat.flight_id is not None and link.field_digest != field['field_digest']:
                    raise ManagedEventError('Cannot replace a host-scheduled physical field; release its pending placement first.')
            ids = [mapping[e] for e in field['entries']]
            try:
                stands = {str(c): int(s) for c, s in zip(ids, field['stands'])}
            except (TypeError, ValueError) as exc:
                raise ManagedEventError('Missoula requires physical integer stand identifiers.') from exc
            if not link.issue_id:
                heat.set_roster(event.event_type, ids, stands)
            elif heat.get_competitors() != ids or heat.get_stand_assignments() != stands:
                raise ManagedEventError('Issued host roster/stands differ from the module authority.')
            link.field_digest, link.issue_id = field['field_digest'], field.get('issue_id')
            if field['state'] == 'accepted':
                heat.status = 'completed'
    db.session.flush()


def recover_command(command_id, user):
    command = db.get_or_404(ManagedCommand, command_id)
    event = db.session.get(Event, command.event_id)
    authorize(user, event)
    if command.actor_id != user.id:
        raise ManagedEventError('Only the original authenticated operator may retry this command.')
    return submit(event, user, json.loads(command.payload_json))


def placement_digest(event):
    rows = []
    for link in ManagedFieldLink.query.filter_by(event_id=event.id).order_by(ManagedFieldLink.id).all():
        heat = db.session.get(Heat, link.heat_id)
        rows.append([link.module_heat_id, heat.id, heat.flight_id, heat.flight_position, link.field_digest])
    return digest(rows)


def release_unissued_placement(event, user, heat_id, expected_placement, *, confirm=False):
    """Explicitly unschedule one pending field before source replacement."""
    from services.flight_builder import lock_tournament_schedule

    authorize(user, event)
    guard = guard_managed_awards([event], user)
    lock_tournament_schedule(event.tournament_id)
    if confirm is not True or placement_digest(event) != expected_placement:
        raise ManagedEventError('Refresh and confirm the current field placement before changing it.')
    link = ManagedFieldLink.query.filter_by(event_id=event.id, heat_id=heat_id).one_or_none()
    if link is None:
        raise ManagedEventError('Managed field placement not found.')
    source_fields = {field['heat_id']: field for round_ in guard.states[event.managed_operation.module_event_id]['rounds']
                     for field in round_['fields']}
    field = source_fields.get(link.module_heat_id)
    heat = db.session.get(Heat, heat_id)
    if link.issue_id or (field and field.get('issue_id')) or heat.status != 'pending' or (heat.flight and heat.flight.status != 'pending'):
        raise ManagedEventError('Issued, completed or started-flight fields cannot be unscheduled here.')
    heat.flight_id, heat.flight_position = None, None
    db.session.flush()


def accept_export(event, user, exported):
    """Atomically retain all attempts, source deliveries, summary and receipt."""
    authorize(user, event)
    link = event.managed_operation
    if not link or exported.get('event_id') != link.module_event_id or exported.get('show_id') != link.show_id or exported.get('schema_version') != CONTRACT:
        raise ManagedEventError('Managed result identity mismatch.')
    if exported.get('synthetic') is not True or exported.get('official_use') is not False:
        raise ManagedEventError('This installation has no live managed-event qualification.')
    revision = exported['event_revision']
    payload_digest = digest(exported)
    prior = ManagedAcceptance.query.filter_by(event_id=event.id, module_revision=revision).first()
    if prior:
        if prior.payload_digest != payload_digest:
            raise ManagedEventError('Changed payload for an accepted module revision.')
        return prior
    # Reject known stale input without opening a source writer. A fresh locked
    # comparison repeats below, after the source fence, before any result write.
    link = ManagedEventLink.query.filter_by(event_id=event.id).populate_existing().one()
    if revision <= link.accepted_revision:
        raise ManagedEventError('Stale managed export cannot replace a later correction.')
    if link.state_json and revision < json.loads(link.state_json)['revision']:
        raise ManagedEventError('Stale managed export cannot replace a later correction.')
    guard = guard_managed_awards([event], user)
    from services.flight_builder import lock_tournament_schedule

    lock_tournament_schedule(event.tournament_id)
    link = _locked_link(event)
    prior = ManagedAcceptance.query.filter_by(event_id=event.id, module_revision=revision).first()
    if prior:
        if prior.payload_digest != payload_digest:
            raise ManagedEventError('Changed payload for an accepted module revision.')
        return prior
    if digest(guard.exports[link.module_event_id]) != payload_digest:
        raise ManagedEventError('Stale source export; the authoritative revision changed before acceptance.')
    if revision <= link.accepted_revision:
        raise ManagedEventError('Stale managed export cannot replace a later correction.')
    mapping = json.loads(link.mapping_json)
    # Source roster equality with an old projection says nothing about current
    # registration. Recheck host facts under the same parent lock used by
    # registration writers, even when the source config has not changed.
    _validate_host_context(event, guard.states[link.module_event_id], mapping)
    for item in exported['deliveries']:
        payload = item['payload']
        if digest(payload) != item['payload_digest'] or payload['outcome']['entry_id'] not in mapping or payload['source_event_id'] != link.module_event_id:
            raise ManagedEventError('Invalid source attempt or delivery digest.')
        existing = ManagedAttemptRevision.query.filter_by(event_id=event.id, source_result_id=payload['source_result_id'], source_revision=payload['source_revision']).first()
        if existing and existing.payload_digest != item['payload_digest']:
            raise ManagedEventError('Attempt source revision conflict.')
        if not existing:
            db.session.add(ManagedAttemptRevision(event_id=event.id, source_result_id=payload['source_result_id'], source_revision=payload['source_revision'], payload_json=canonical(payload), payload_digest=item['payload_digest']))
        delivery = db.session.get(ManagedCareerDelivery, item['delivery_id'])
        if not delivery:
            db.session.add(ManagedCareerDelivery(delivery_id=item['delivery_id'], event_id=event.id, payload_json=canonical(payload), payload_digest=item['payload_digest']))
    if exported['status'] == 'finalized':
        summary = exported.get('summary')
        if not summary or len(summary) != len(mapping) or {r['entry_id'] for r in summary} != set(mapping):
            raise ManagedEventError('Complete source summary required.')
        for item in summary:
            row = EventResult.query.filter_by(event_id=event.id, competitor_type=event.event_type, competitor_id=mapping[item['entry_id']]).one()
            row.result_value = float(item['raw_duration_seconds']) if item['raw_duration_seconds'] is not None else None
            row.handicap_factor = item['start_mark_seconds']
            row.final_position = item['official_position']
            row.status = {'finished': 'completed', 'dns': 'scratched', 'void': 'dq'}.get(item['status'], item['status'])
            row.result_unit = 'seconds'
        event.status = 'completed'
        # Host awards and settlement remain explicit operations after accepting
        # source results. A correction cannot silently pay or repay anyone.
        if link.summary_revision != exported['summary_revision']:
            event.is_finalized = False
        link.summary_revision = exported['summary_revision']
    else:
        # Keep earlier awards/settlement facts for explicit reconciliation, but
        # they cannot remain final after accepting a still-active correction.
        event.is_finalized = False
        event.status = 'in_progress' if exported['rounds'] else 'pending'
        link.summary_revision = 0
    link.accepted_revision = revision
    receipt = ManagedAcceptance(event_id=event.id, module_revision=revision, payload_digest=payload_digest, actor_id=user.id)
    db.session.add(receipt)
    db.session.flush()
    return receipt


def apply_host_awards(event, user):
    """Use accepted source positions, without recalculating delegated placings."""
    authorize(user, event)
    if not event.managed_operation:
        raise ManagedEventError('Accepted finalized module summary required before host awards.')
    guard = guard_managed_awards([event], user)
    from services.flight_builder import lock_tournament_schedule

    lock_tournament_schedule(event.tournament_id)
    link = _locked_link(event)
    if not link.summary_revision:
        raise ManagedEventError('Accepted finalized module summary required before host awards.')
    source = guard.states[link.module_event_id]
    if source['status'] != 'finalized' or source['summary_revision'] != link.summary_revision:
        raise ManagedEventError('Accept the current finalized source revision before applying host awards.')
    _validate_host_context(event, source, json.loads(link.mapping_json))
    _project_latest(event, source)
    from services.scoring_engine import (
        _clear_changed_payout_settlements,
        _rebuild_individual_points,
        split_tie_points,
    )

    results = event.results.all()
    prior_payouts = {row.id: float(row.payout_amount or 0) for row in results}
    groups = {}
    for row in results:
        if row.final_position is not None:
            groups.setdefault(row.final_position, []).append(row)
    for row in results:
        row.points_awarded = Decimal(0)
        row.payout_amount = 0
    for position, rows in groups.items():
        for row in rows:
            if event.event_type == 'college':
                row.points_awarded = split_tie_points(position, len(rows))
            else:
                row.payout_amount = event.get_payout_for_position(position)
    db.session.flush()
    if event.event_type == 'college':
        from models import CollegeCompetitor, Team

        touched_ids = [r.competitor_id for r in results]
        _rebuild_individual_points(touched_ids)
        touched = CollegeCompetitor.query.filter(CollegeCompetitor.id.in_(touched_ids)).all()
        for team_id in {comp.team_id for comp in touched if comp.team_id}:
            team = db.session.get(Team, team_id)
            if team:
                team.recalculate_points()
    else:
        from models.competitor import ProCompetitor

        for row in results:
            comp = db.session.get(ProCompetitor, row.competitor_id)
            comp.total_earnings = sum(float(r.payout_amount or 0) for r in EventResult.query.filter_by(competitor_type='pro', competitor_id=comp.id).all())
    event.is_finalized = True
    _clear_changed_payout_settlements(results, prior_payouts)


def deliver_career_results(event, user, *, limit=25, transport=None):
    """Bounded supervised delivery; never called during scoring/finalization."""
    authorize(user, event)
    url = current_app.config.get('MNEMEX_SHOW_RESULTS_URL', '')
    token = current_app.config.get('MNEMEX_SHOW_RESULTS_TOKEN', '')
    parsed = urlsplit(url)
    if not token or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path != '/partners/results/ingest' or (parsed.scheme != 'https' and not (parsed.scheme == 'http' and parsed.hostname in {'127.0.0.1', 'localhost', '::1'})):
        raise ManagedEventError('Configure an explicit HTTPS or loopback MNEMEX inbox and scoped service credential.')
    if not 1 <= limit <= 100:
        raise ManagedEventError('Delivery limit must be 1..100.')
    import requests

    session = requests.Session()
    session.trust_env = False
    send = transport or session.post
    delivered, pending, conflicting = 0, 0, 0
    mapping = json.loads(event.managed_operation.mapping_json)
    try:
        for item in ManagedCareerDelivery.query.filter_by(event_id=event.id, status='pending').order_by(ManagedCareerDelivery.delivery_id).with_for_update(skip_locked=True).limit(limit).all():
            payload = json.loads(item.payload_json)
            if digest(payload) != item.payload_digest:
                raise ManagedEventError('Stored career delivery failed integrity verification.')
            entry_id = payload['outcome']['entry_id']
            envelope = {'schema_version': 'mnemex.show-result.v1', 'message_id': item.delivery_id,
                        'source': payload, 'source_digest': item.payload_digest,
                        'participant': {'show_person_ref': None, 'unresolved_ref': f'{event.event_type}:{mapping[entry_id]}', 'authorization_basis_ref': None}}
            try:
                response = send(url, data=canonical(envelope), headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'}, timeout=(3, 8), allow_redirects=False)
                if response.status_code not in {200, 409} or len(response.content) > 65536:
                    raise ManagedEventError('receiver_unavailable_or_refused')
                receipt = response.json()
                if not isinstance(receipt, dict):
                    raise ManagedEventError('receiver_receipt_must_be_an_object')
                if receipt.get('schema_version') != 'mnemex.show-result-receipt.v1' or receipt.get('source_result_id') != payload['source_result_id'] or receipt.get('source_revision') != payload['source_revision'] or receipt.get('state') not in {'unlinked', 'pending_predecessor', 'accepted', 'conflicting'}:
                    raise ManagedEventError('receiver_receipt_mismatch')
                if receipt.get('payload_digest') != digest({k: v for k, v in envelope.items() if k != 'message_id'}):
                    raise ManagedEventError('receiver_payload_binding_mismatch')
                item.receipt_json, item.receipt_digest = canonical(receipt), digest(receipt)
                item.status = 'conflicting' if receipt['state'] == 'conflicting' else 'accepted'
                item.last_error_code = None
                conflicting += int(item.status == 'conflicting')
                delivered += int(item.status == 'accepted')
            except (requests.RequestException, ValueError, KeyError, ManagedEventError):
                item.last_error_code = 'receiver_unavailable_or_invalid_receipt'
                pending += 1
            db.session.commit()
    finally:
        session.close()
    return {'accepted': delivered, 'pending_attempts': pending, 'conflicting': conflicting,
            'remaining_pending': ManagedCareerDelivery.query.filter_by(event_id=event.id, status='pending').count()}
