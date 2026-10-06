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
                or set(state) != {'version', 'generation', 'endpoint', 'revoked', 'containers'}
                or type(state['version']) is not int or state['version'] != 1
                or state['generation'] != self.generation or state['endpoint'] != self.endpoint
                or type(state['revoked']) is not bool or not isinstance(state['containers'], dict)):
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
            self.write({'version': 1, 'generation': self.generation, 'endpoint': self.endpoint,
                        'revoked': False, 'containers': {}})

    def create_intent(self, name):
        if not isinstance(name, str) or re.fullmatch(
                re.escape(self.generation) + r'-[a-z][a-z0-9-]{0,31}', name) is None:
            raise ValueError('owned fixture container name required')
        with self.transaction():
            state = self.read()
            if state['revoked'] or name in state['containers']:
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
            if (state['revoked'] or item is None or item['operation'] != operation
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
