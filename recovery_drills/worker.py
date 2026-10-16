"""Explicit lab launcher; ordinary app:create_app() never imports this module.

All instrumentation is installed by this separate executable, using a private
control file, never a request header, query parameter, or ambient crash flag.
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import time
from pathlib import Path

from recovery_drills.ownership import process_identity, publish


def restrict_network(ports):
    def audit(event, args):
        if event == 'socket.connect':
            address = args[1]
            if isinstance(address, tuple) and (address[0] != '127.0.0.1'
                                               or address[1] not in ports):
                raise PermissionError('Recovery lab prohibits non-owned network destinations.')
        if event == 'socket.getaddrinfo' and args[0] not in ('127.0.0.1', None):
            raise PermissionError('Recovery lab prohibits external DNS.')
    sys.addaudithook(audit)


def application(settings):
    from flask import Flask

    import app as application_module

    class LabFlask(Flask):
        def __init__(self, *args, **kwargs):
            kwargs['instance_path'] = settings['private'] + '/instance'
            super().__init__(*args, **kwargs)

    application_module.Flask = LabFlask
    app = application_module.create_app()
    assert not app.testing and not app.debug and app.config['WTF_CSRF_ENABLED']
    assert app.config['SQLALCHEMY_DATABASE_URI'] == settings['database_url']
    app.config['PROPAGATE_EXCEPTIONS'] = False
    if settings.get('managed_python'):
        app.config.update(MANAGED_EVENTS_ENABLED=True, MANAGED_EVENTS_PYTHON=settings['managed_python'],
                          MANAGED_EVENTS_DATABASE=str(Path(settings['private'])/'managed-authority.sqlite3'))
    return app


def barrier(settings, point, request_id=None, extra=None):
    private = Path(settings['private'])
    fault_path = private / 'fault.json'
    if not fault_path.exists():
        return
    fault = json.loads(fault_path.read_text())
    if fault['point'] != point or fault.get('request_id') != request_id:
        return
    publish(private / 'reached.json', json.dumps({
        'point': point, 'request_id': request_id, 'process': process_identity(os.getpid()),
        'monotonic': time.monotonic(), **(extra or {}),
    }))
    # A watchdog, not a claimed application recovery objective.
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        if (private / 'release').exists():
            return
        time.sleep(0.05)
    raise TimeoutError('Lab fault barrier was reached but never resolved.')


def instrument(settings):
    from flask import has_request_context, request
    from sqlalchemy import event as sa_event
    from sqlalchemy.orm import Session

    from models.managed_event import ManagedAcceptance

    @sa_event.listens_for(Session, 'before_flush')
    def note_managed_acceptance(session, _context, _instances):
        if any(isinstance(item, ManagedAcceptance) for item in session.new):
            session.info['lab_managed_acceptance_transaction'] = True

    @sa_event.listens_for(Session, 'after_rollback')
    def forget_managed_acceptance(session):
        session.info.pop('lab_managed_acceptance_transaction', None)

    real_commit = Session.commit

    def commit(session):
        target = (request.form.get('request_id') if has_request_context()
                  and request.endpoint in ('scoring.enter_heat_results',
                                           'scoring.replay_offline_score') else None)
        if target:
            # Include receipt/audit INSERTs, not only earlier result flushes.
            session.flush()
            barrier(settings, 'score-precommit', target)
        managed = (request.form.get('request_id') if has_request_context()
                   and request.endpoint == 'managed.event_workspace'
                   and request.form.get('action') == 'accept_export'
                   and session.info.pop('lab_managed_acceptance_transaction', False) else None)
        if managed:
            session.flush()
            barrier(settings, 'managed-accept-precommit', managed)
        result = real_commit(session)
        if target:
            barrier(settings, 'score-postcommit', target)
        if managed:
            barrier(settings, 'managed-accept-postcommit', managed)
        return result
    Session.commit = commit

    import services.reporting_export as exports
    real_export = exports.export_results_to_excel

    def export(tournament, path):
        control = Path(settings['private']) / 'fault.json'
        if control.exists() and json.loads(control.read_text())['point'] == 'job-running':
            Path(path).write_bytes(b'partial synthetic export; never downloadable')
            barrier(settings, 'job-running', extra={'partial_name': Path(path).name})
        return real_export(tournament, path)
    exports.export_results_to_excel = export


def seed(app):
    from database import db
    from models import Event, EventResult, Heat, ProCompetitor, Tournament, User
    with app.app_context():
        tournaments = [Tournament(name=f'[SYNTHETIC RECOVERY] {name}', year=2027,
                                  status='pro_active') for name in ('Primary', 'Untouched')]
        db.session.add_all(tournaments)
        db.session.flush()
        for name, role, scope in [('judge', 'judge', tournaments[0].id),
                                  ('otherjudge', 'judge', tournaments[0].id),
                                  ('viewer', 'viewer', tournaments[0].id),
                                  ('outsider', 'scorer', tournaments[1].id)]:
            user = User(username=name, role=role, tournament_id=scope)
            user.set_password('synthetic-lab-password')
            db.session.add(user)
        event = Event(tournament_id=tournaments[0].id, name='Single Buck',
                      event_type='pro', scoring_type='time', scoring_order='lowest_wins',
                      is_handicap=False, stand_type='saw_hand', max_stands=2)
        event.set_payouts({'1': 100, '2': 50, '3': 25, '4': 10})
        db.session.add(event)
        db.session.flush()
        competitors = []
        for name in ('Alpha', 'Beta', 'Gamma', 'Delta'):
            competitor = ProCompetitor(tournament_id=tournaments[0].id,
                                       name='Synthetic ' + name, gender='M')
            competitor.events_entered = json.dumps([event.name])
            db.session.add(competitor)
            db.session.flush()
            competitors.append(competitor)
            db.session.add(EventResult(event_id=event.id, competitor_id=competitor.id,
                                       competitor_type='pro', competitor_name=competitor.name))
        for number in (1, 2):
            heat = Heat(event_id=event.id, heat_number=number, run_number=1)
            group = competitors[(number - 1) * 2:number * 2]
            heat.set_roster('pro', [c.id for c in group],
                            {str(c.id): i for i, c in enumerate(group, 1)})
            db.session.add(heat)
        db.session.commit()
        return {'tournament': tournaments[0].id, 'other_tournament': tournaments[1].id,
                'event': event.id, 'heats': [h.id for h in event.heats.order_by(Heat.id)],
                'competitors': [c.id for c in competitors]}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('settings')
    parser.add_argument('action', choices=['migrate', 'seed', 'backup', 'serve', 'obsolete-job',
                                         'shadow-seed', 'shadow-deliver', 'managed-seed', 'managed-correct'])
    args = parser.parse_args()
    settings = json.loads(Path(args.settings).read_text())
    if settings.get('application_root'):
        sys.path.insert(0, settings['application_root'])
    os.environ['DATABASE_URL'] = settings['database_url']
    os.environ['SECRET_KEY'] = settings['secret']
    restrict_network(set(settings['ports']))
    app = application(settings) if args.action != 'serve' else None
    if args.action == 'migrate':
        from flask_migrate import upgrade
        with app.app_context():
            upgrade(directory=str(Path(app.root_path) / 'migrations'))
    elif args.action == 'seed':
        publish(Path(settings['private']) / 'fixture.json', json.dumps(seed(app)))
    elif args.action == 'backup':
        from services.backup import backup_database
        result = backup_database(settings['database_url'], 1, app.instance_path)
        if not result['ok']:
            raise RuntimeError('Actual application PostgreSQL backup failed.')
        publish(Path(settings['private']) / 'backup.json', json.dumps(result))
    elif args.action == 'obsolete-job':
        import services.background_jobs as jobs
        jobs._BOOT_ID = settings['obsolete_owner']
        result = jobs._persist_job(settings['job_id'], status='completed',
                                   result={'path': 'obsolete'}, require_active_owner=True)
        publish(Path(settings['private']) / 'obsolete.json', json.dumps({'accepted': result}))
    elif args.action in ('managed-seed', 'managed-correct'):
        from recovery_drills.managed import correct_managed, seed_managed
        if args.action == 'managed-seed':
            seed_managed(app, settings)
        else:
            correct_managed(app, settings)
    elif args.action in ('shadow-seed', 'shadow-deliver'):
        from recovery_drills.shadow import deliver_shadow, seed_shadow
        if args.action == 'shadow-seed':
            seed_shadow(app, settings)
        else:
            deliver_shadow(app, settings)
    else:
        from gunicorn.app.base import BaseApplication

        def worker_ready(worker):
            publish(Path(settings['private']) / f'worker-{os.getpid()}.json',
                    json.dumps({'identity': process_identity(os.getpid()),
                                'parent': os.getppid(), 'run_id': settings['run_id'],
                                'application_source': worker.wsgi.root_path,
                                'testing': worker.wsgi.testing,
                                'csrf': worker.wsgi.config['WTF_CSRF_ENABLED']}))

        class Server(BaseApplication):
            def load_config(self):
                for key, value in {'bind': f"127.0.0.1:{settings['app_port']}",
                                   'workers': 1, 'threads': 4, 'timeout': 120,
                                   'post_worker_init': worker_ready,
                                   'accesslog': None, 'errorlog': '-',
                                   'preload_app': False}.items():
                    self.cfg.set(key, value)

            def load(self):
                # Factory executes in the worker, so boot/job ownership is new.
                app = application(settings)
                instrument(settings)
                return app
        Server().run()


if __name__ == '__main__':
    main()
