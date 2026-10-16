"""Private adapter worker in the explicitly selected STRATHMARK interpreter.

The exact rc11 application supplies exports, authorization and its verified
transaction primitive. No numerical, result or progression logic lives here.
Keep its SQLite writer fence until the host acknowledges commit or closes stdin.
"""

import argparse
import json
import sys
from importlib.metadata import version


def main():
    from strathmark.events.client import local_request
    from strathmark.events.contracts import closed, encode
    from strathmark.events.operations import EventOperations

    parser = argparse.ArgumentParser()
    parser.add_argument('--database', required=True)
    args = parser.parse_args()
    announced = False
    try:
        if version('strathmark') != '3.0.0rc11':
            raise ValueError('unsupported_source_fence_runtime')
        raw = sys.stdin.readline(4 * 1024 * 1024 + 1)
        if len(raw) > 4 * 1024 * 1024:
            raise ValueError('source_fence_request_capacity')
        envelope = json.loads(raw)
        closed(envelope, {'principal', 'event_ids'})
        event_ids = envelope['event_ids']
        if not isinstance(event_ids, list) or not 1 <= len(event_ids) <= 64 or len(set(event_ids)) != len(event_ids):
            raise ValueError('source_fence_event_capacity')
        ops = EventOperations(args.database)
        exports = {event_id: local_request(ops, {'principal': envelope['principal'],
                   'request': {'operation': 'export', 'event_id': event_id}}) for event_id in event_ids}
        # All CLI/GUI writes use this same BEGIN IMMEDIATE transaction. Verify
        # the earlier application exports after obtaining it, then retain it
        # through the independent host commit. EOF rolls back and releases it.
        with ops.store.transaction() as source:
            states = {event_id: ops.store.read(source, event_id) for event_id in event_ids}
            if any(states[key]['revision'] != exports[key]['event_revision'] for key in event_ids):
                raise ValueError('source_changed_before_fence')
            print(encode({'ok': True, 'schema_version': 'strathmark.held-export.v1',
                          'exports': exports, 'states': states}), flush=True)
            announced = True
            if sys.stdin.readline(16) != 'commit\n':
                raise EOFError('host_transaction_aborted')
    except EOFError:
        return 0
    except Exception as exc:
        if not announced:
            print(encode({'ok': False, 'error': str(exc)}), flush=True)
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
