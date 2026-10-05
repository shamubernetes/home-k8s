#!/usr/bin/env python3
"""Validate a restored bundle and run pinned native startup under execution hold.

This is NOT run.sh and deliberately has no worker, beat, web API or release
operation. Use only on a new isolated restore with all writers excluded. A
coherent checkpoint and task-specific release decision belong to the caller.
"""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import signal
import sys
import tempfile

import backup as contract

SOURCES = {
    'config/management/commands/ta_startup.py': '9dead2d0aaab22f970ddef8eb93574b257246cef3beb3e2a1db54820e4230a1e',
    'task/src/task_manager.py': 'b30ee45bbf656ca5067d6c9adebd80e2ce989ec0854921546db24992f85fff93',
    'common/src/ta_redis.py': '2a581402292fa8149d483c95d0cdda0d0dfdf6770f67bf82d73b7969a9ebef2e',
}


def disposition(key, client):
    """Classify without rewriting native values, or guessing replay arguments."""
    if key.startswith(b'celery-task-meta-'):
        try:
            value = json.loads(client.get(key))
            status = value.get('status')
            if status in {'SUCCESS', 'FAILURE', 'FAILED', 'REVOKED'}:
                return 'terminal-no-replay'
            if status in {'PENDING', 'STARTED', 'RETRY'}:
                return 'interrupted-requires-reconciliation'
        except (TypeError, ValueError, AttributeError):
            pass
        return 'invalid-or-unknown-no-replay'
    if key.startswith(b'ta:message:'):
        return 'progress-preserved'
    if key.startswith(b'ta:'):
        return 'application-intent-preserved'
    return 'opaque-or-broker-no-replay'


def hold_startup(bundle, ledger):
    contract.check(not ledger.is_relative_to(bundle), 'reconciliation ledger must not overwrite bundle')
    manifest = contract.verify(bundle)
    contract.check(os.environ.get('K8S92_ISOLATED_RESTORE') == 'YES', 'isolated recovery required')
    client = contract.redis_client()
    contract.check(contract.redis_instance_id(client) != manifest['redis']['source_instance_id'],
                   'refuse source Redis server')
    owner = contract.recovery_owner(bundle, manifest, client)
    records = json.loads((bundle / 'redis.json').read_text())
    contract.verify_restored_redis(client, records, owner)
    for relative, expected in SOURCES.items():
        contract.check(contract.digest(Path('/app') / relative) == expected,
                       'unqualified native recovery startup source')
    sys.path.insert(0, '/app')
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
    import django
    django.setup()
    from config.management.commands.ta_startup import Command

    class HeldStartup(Command):
        # Native cleanup deletes queues/progress, fails PENDING records, and
        # removes partial media. Preserve them until cross-store reconciliation.
        def _clear_redis_keys(self):
            pass

        def _clear_tasks(self):
            pass

        def _clear_dl_cache(self):
            pass

        # Native version check can publish a new Celery job at startup.
        def _version_check(self):
            pass

        def _set_ta_startup_time(self):
            pass

    HeldStartup().handle()
    contract.verify_restored_redis(client, records, owner)
    live = contract.unexpired_records(client, records)
    report = {'format': 'ta-reconciliation-hold-v1', 'owner': json.loads(owner),
              'execution': 'HELD', 'automatic_replay': False, 'records': {}}
    for encoded in records:
        key = base64.b64decode(encoded, validate=True)
        identity = hashlib.sha256(key).hexdigest()
        report['records'][identity] = (disposition(key, client) if encoded in live
                                      else 'naturally-expired-original-archived')
    ledger.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode='w', prefix='.hold-', dir=ledger.parent,
                                     delete=False) as stream:
        temporary = Path(stream.name)
        try:
            json.dump(report, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
            temporary.replace(ledger)
        finally:
            temporary.unlink(missing_ok=True)
    fd = os.open(ledger.parent, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    print('Pinned native startup validated, all execution remains held')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('bundle', type=Path)
    parser.add_argument('ledger', type=Path)
    args = parser.parse_args()
    os.umask(0o077)
    signal.signal(signal.SIGALRM, lambda *_: (_ for _ in ()).throw(TimeoutError('held startup deadline')))
    signal.alarm(600)
    try:
        hold_startup(args.bundle.resolve(), args.ledger.resolve())
    except Exception as exc:
        print(f'held startup failed: {type(exc).__name__}', file=sys.stderr)
        sys.exit(1)
