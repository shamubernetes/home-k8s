"""Durable container create/start intents, not daemon-side revocation proof.

Trusted controller storage only. Short state locks never enclose Docker I/O.
Revocation denies subsequent controller dispatch, but does not cancel an
already accepted daemon request. Native admission must remain closed until
independent server/client cessation is qualified.
"""
import fcntl
import json
import os
from pathlib import Path
import re
import tempfile
import uuid
import subprocess
from contextlib import contextmanager


def validate_endpoint(endpoint):
    if (not isinstance(endpoint, str) or not endpoint.startswith('unix:///')
            or endpoint.startswith('unix:////') or endpoint.endswith('/')
            or not endpoint.isprintable() or any(c.isspace() for c in endpoint)
            or any(c in endpoint for c in ('?', '#', '%'))
            or str(Path(endpoint[7:])) != endpoint[7:]
            or '..' in Path(endpoint[7:]).parts):
        raise ValueError('canonical Unix fixture endpoint required')


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise RuntimeError('duplicate fixture manifest field')
        result[key] = value
    return result


class GenerationManifest:
    def __init__(self, path, generation, endpoint):
        if not isinstance(generation, str) or re.fullmatch(r'k8s92-nonrel-[0-9a-f]{32}', generation) is None:
            raise ValueError('owned fixture generation required')
        validate_endpoint(endpoint)
        self.path = Path(path)
        self.generation = generation
        self.endpoint = endpoint

    @contextmanager
    def transaction(self):
        with self.path.with_suffix(self.path.suffix + '.lock').open('a+b') as lock:
            os.chmod(lock.name, 0o600)
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            yield

    def read(self):
        if self.path.stat().st_size > 65536:
            raise RuntimeError('fixture manifest exceeds bound')
        state = json.loads(self.path.read_text(), object_pairs_hook=unique_object)
        if (not isinstance(state, dict)
                or type(state.get('version')) is not int or state['version'] not in (1, 2)
                or set(state) != ({'version', 'generation', 'endpoint', 'revoked', 'containers'}
                                 | ({'admissions'} if state['version'] == 2 else set()))
                or state['generation'] != self.generation or state['endpoint'] != self.endpoint
                or type(state['revoked']) is not bool or not isinstance(state['containers'], dict)
                or not isinstance(state.get('admissions', {}), dict)):
            raise RuntimeError('fixture manifest binding malformed')
        for name, item in state['containers'].items():
            if (not isinstance(name, str) or not name.startswith(self.generation + '-')
                    or not isinstance(item, dict) or set(item) != {'operation', 'phase', 'id'}
                    or not isinstance(item['operation'], str)
                    or re.fullmatch(r'[0-9a-f]{32}', item['operation']) is None
                    or item['phase'] not in ('create-intent', 'registered', 'start-intent', 'running')
                    or (item['phase'] == 'create-intent' and item['id'] is not None)
                    or (item['phase'] != 'create-intent' and (
                        not isinstance(item['id'], str)
                        or re.fullmatch(r'[0-9a-f]{64}', item['id']) is None))):
                raise RuntimeError('fixture manifest container malformed')
        for name, binding in state.get('admissions', {}).items():
            item = state['containers'].get(name)
            if (item is None or item['phase'] not in ('running', 'start-intent')
                    or not isinstance(binding, dict)
                    or set(binding) != {'container_id', 'index_name', 'index_uuid', 'write_block'}
                    or binding['container_id'] != item['id'] or binding['index_name'] != 'fixture'
                    or not isinstance(binding['index_uuid'], str)
                    or re.fullmatch(r'[A-Za-z0-9_-]{22}', binding['index_uuid']) is None
                    or binding['write_block'] not in (None, 'true', 'false')):
                raise RuntimeError('fixture manifest admission malformed')
        return state

    def write(self, state):
        fd, name = tempfile.mkstemp(dir=self.path.parent, prefix=self.path.name + '.')
        try:
            with os.fdopen(fd, 'w') as stream:
                json.dump(state, stream, sort_keys=True)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, self.path)
            directory = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if os.path.exists(name):
                os.unlink(name)

    def initialize(self):
        with self.transaction():
            if self.path.exists():
                raise RuntimeError('fixture manifest already exists, reconcile before retry')
            self.write({'version': 2, 'generation': self.generation, 'endpoint': self.endpoint,
                        'revoked': False, 'containers': {}, 'admissions': {}})

    def bind_admission(self, name, container_id, admission):
        """Persist source identity and prior admission before a writer mutation.

        This immutable binding is not authority to reopen a revoked source.
        Old manifests remain retirement-readable but cannot admit new work.
        """
        if (not isinstance(admission, dict) or set(admission) != {'index_uuid', 'write_block'}
                or not isinstance(admission['index_uuid'], str)
                or re.fullmatch(r'[A-Za-z0-9_-]{22}', admission['index_uuid']) is None
                or admission['write_block'] not in (None, 'true', 'false')):
            raise ValueError('immutable fixture admission required')
        binding = dict(admission, container_id=container_id, index_name='fixture')
        with self.transaction():
            state = self.read()
            item = state['containers'].get(name)
            if (state['version'] != 2 or state['revoked'] or item is None or item['phase'] != 'running'
                    or item['id'] != container_id):
                raise RuntimeError('fixture admission binding denied')
            if name in state['admissions'] and state['admissions'][name] != binding:
                raise RuntimeError('fixture prior admission is immutable')
            state['admissions'][name] = binding
            self.write(state)

    def require_admission(self, name, container_id, admission):
        with self.transaction():
            state = self.read()
            expected = dict(admission, container_id=container_id, index_name='fixture')
            if state['version'] != 2 or state['revoked'] or state['admissions'].get(name) != expected:
                raise RuntimeError('fixture admission authority denied')

    def create_intent(self, name):
        if not isinstance(name, str) or re.fullmatch(
                re.escape(self.generation) + r'-[a-z][a-z0-9-]{0,31}', name) is None:
            raise ValueError('owned fixture container name required')
        with self.transaction():
            state = self.read()
            if state['version'] != 2 or state['revoked'] or name in state['containers']:
                raise RuntimeError('fixture create denied, reconcile existing intent')
            operation = uuid.uuid4().hex
            state['containers'][name] = {'operation': operation, 'phase': 'create-intent', 'id': None}
            self.write(state)
            return operation

    def transition(self, name, operation, before, after, container_id):
        if (before, after) not in (('create-intent', 'registered'),
                                   ('registered', 'start-intent'), ('start-intent', 'running')):
            raise ValueError('invalid fixture manifest transition')
        if not isinstance(container_id, str) or re.fullmatch(r'[0-9a-f]{64}', container_id) is None:
            raise ValueError('immutable fixture container ID required')
        with self.transaction():
            state = self.read()
            item = state['containers'].get(name)
            if (state['version'] != 2 or state['revoked'] or item is None or item['operation'] != operation
                    or item['phase'] != before or item['id'] not in (None, container_id)):
                raise RuntimeError('fixture manifest transition denied')
            item.update(phase=after, id=container_id)
            self.write(state)

    def register(self, name, operation, container_id):
        self.transition(name, operation, 'create-intent', 'registered', container_id)

    def start_intent(self, name, operation, container_id):
        self.transition(name, operation, 'registered', 'start-intent', container_id)

    def started(self, name, operation, container_id):
        self.transition(name, operation, 'start-intent', 'running', container_id)

    def revoke(self):
        with self.transaction():
            state = self.read()
            state['revoked'] = True
            self.write(state)

    def reconcile(self, *, timeout=30):
        """Recover immutable IDs for retirement, never authorize restarted work.

        Revoke before I/O so lost acknowledgments cannot allow a new dispatch.
        Query the exact endpoint, including stopped containers, then inspect
        immutable IDs. Absence is not proof that delayed creates have ceased.
        """
        self.revoke()
        with self.transaction():
            expected = self.read()

        def docker(*args):
            result = subprocess.run(['docker', '--host', self.endpoint, *args],
                                    capture_output=True, timeout=timeout)
            if result.returncode:
                raise RuntimeError('fixture reconciliation Docker query failed')
            return result.stdout

        ids = docker('ps', '--all', '--no-trunc', '--quiet', '--filter',
                     'label=kopiur.fixture-generation=' + self.generation).decode().splitlines()
        if (len(ids) > len(expected['containers']) or len(ids) != len(set(ids))
                or any(re.fullmatch(r'[0-9a-f]{64}', value) is None for value in ids)):
            raise RuntimeError('fixture reconciliation inventory ambiguous')
        observations = {}
        for container_id in ids:
            rows = json.loads(docker('inspect', container_id), object_pairs_hook=unique_object)
            if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
                raise RuntimeError('fixture reconciliation inspection malformed')
            row = rows[0]
            name = row.get('Name')
            config = row.get('Config')
            labels = config.get('Labels') if isinstance(config, dict) else None
            if not isinstance(name, str) or not name.startswith('/') or not isinstance(labels, dict):
                raise RuntimeError('fixture reconciliation ownership malformed')
            name = name[1:]
            item = expected['containers'].get(name)
            if (item is None or name in observations or row.get('Id') != container_id
                    or labels.get('kopiur.fixture-generation') != self.generation
                    or labels.get('kopiur.fixture-operation') != item['operation']
                    or item['id'] not in (None, container_id)):
                raise RuntimeError('fixture reconciliation ownership mismatch')
            observations[name] = container_id
        with self.transaction():
            state = self.read()
            if state != expected:
                raise RuntimeError('fixture manifest changed during reconciliation')
            for name, container_id in observations.items():
                item = state['containers'][name]
                if item['phase'] == 'create-intent':
                    item.update(phase='registered', id=container_id)
            self.write(state)
        return {'observed': observations,
                'unobserved': sorted(set(expected['containers']) - set(observations)),
                'startup_allowed': False, 'cessation_proved': False}

    def retire(self, *, timeout=30):
        """Retire reconciled exact IDs; retain durable evidence and revocation.

        Removal acknowledgments and an inventory sample do not prove that an
        already accepted create/start request cannot execute later. Never
        release native admission or reset an intent from this result.
        """
        receipt = self.reconcile(timeout=timeout)
        failures = []
        acknowledged = []
        for name, container_id in sorted(receipt['observed'].items()):
            try:
                result = subprocess.run(
                    ['docker', '--host', self.endpoint, 'rm', '--force', container_id],
                    capture_output=True, timeout=timeout)
                if result.returncode:
                    failures.append(name)
                else:
                    acknowledged.append(container_id)
            except (OSError, subprocess.SubprocessError):
                failures.append(name)
        try:
            result = subprocess.run(
                ['docker', '--host', self.endpoint, 'ps', '--all', '--no-trunc', '--quiet'],
                capture_output=True, timeout=timeout)
            if result.returncode:
                raise RuntimeError('fixture retirement inventory failed')
            inventory = result.stdout.decode().splitlines()
            if (len(inventory) != len(set(inventory))
                    or any(re.fullmatch(r'[0-9a-f]{64}', value) is None for value in inventory)):
                raise RuntimeError('fixture retirement inventory ambiguous')
        except (OSError, subprocess.SubprocessError, UnicodeError):
            raise RuntimeError('fixture retirement inventory unavailable') from None
        if failures or set(receipt['observed'].values()).intersection(inventory):
            raise RuntimeError('fixture retirement unresolved')
        receipt.update(removal_acknowledged=sorted(acknowledged),
                       observed_ids_absent=True, admission_release_allowed=False)
        return receipt


def main(argv=None):
    """Explicit persisted-manifest retirement on approved ARC only."""
    import argparse
    import sys

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('reconcile', 'retire'))
    parser.add_argument('--manifest', required=True)
    parser.add_argument('--generation', required=True)
    parser.add_argument('--endpoint', required=True)
    args = parser.parse_args(argv)
    if sys.platform != 'linux' or not os.environ.get('RUNNER_NAME', '').startswith('ghar-set-zoo-'):
        raise RuntimeError('fixture reconciliation requires approved Linux ARC')
    manifest = GenerationManifest(args.manifest, args.generation, args.endpoint)
    manifest.read()
    result = getattr(manifest, args.action)()
    print(json.dumps(result, sort_keys=True))


if __name__ == '__main__':
    main()
