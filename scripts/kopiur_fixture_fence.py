"""Durable recovery of the disposable Elasticsearch fixture's write fence.

Not a production watchdog. Only the UUID-owned ARC source and fixture index
are admitted. Persist intent before mutation, then recover the same boundary
when a capture process stops, without accepting an incomplete generation.
"""
import fcntl
import json
import os
from pathlib import Path
import re
import tempfile


class FixtureFence:
    def __init__(self, path, source, admission=None):
        if re.fullmatch(r'k8s92-nonrel-[0-9a-f]{32}-source', source) is None:
            raise ValueError('only an owned nonrel fixture source is admitted')
        if admission is not None and (
                not isinstance(admission, dict) or set(admission) != {'index_uuid', 'write_block'}
                or not isinstance(admission['index_uuid'], str) or not admission['index_uuid']
                or admission['write_block'] not in (None, 'true', 'false')):
            raise ValueError('exact observed native admission required')
        self.path = Path(path)
        self.source = source
        # Capture immutable bytes, not a mutable caller dictionary. Legacy
        # unbound journals never qualify this native admission protocol.
        self._admission = json.dumps(admission, sort_keys=True)
        self.lock = None

    def __enter__(self):
        self.lock = self.path.with_suffix('.lock').open('a+b')
        os.chmod(self.lock.name, 0o600)
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.lock.close()
            self.lock = None
            raise RuntimeError('fixture capture already has an owner') from None
        return self

    def __exit__(self, *_):
        if self.lock is not None:
            self.lock.close()
            self.lock = None

    def read(self):
        if self.lock is None:
            raise RuntimeError('fixture journal requires exclusive ownership')
        if not self.path.exists():
            return None
        state = json.loads(self.path.read_text())
        admission = json.loads(self._admission)
        required = {'source', 'phase'} if admission is None else {'source', 'phase', 'admission'}
        if (set(state) != required or state['source'] != self.source
                or (admission is not None and state['admission'] != admission)
                or state['phase'] not in ('intent', 'fenced', 'released')):
            raise RuntimeError('fixture journal does not match the owned boundary')
        return state['phase']

    def write(self, phase):
        if phase not in ('intent', 'fenced', 'released'):
            raise ValueError('invalid fixture fence phase')
        self.read()
        fd, name = tempfile.mkstemp(dir=self.path.parent, prefix=self.path.name + '.')
        try:
            with os.fdopen(fd, 'w') as stream:
                state = {'source': self.source, 'phase': phase}
                admission = json.loads(self._admission)
                if admission is not None:
                    state['admission'] = admission
                json.dump(state, stream)
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

    def acquire(self, block):
        if self.read() not in (None, 'released'):
            raise RuntimeError('recover prior fixture capture before acquiring')
        self.write('intent')
        block()
        self.write('fenced')

    def recover(self, release):
        phase = self.read()
        if phase in ('intent', 'fenced'):
            # Intent can survive a crash after ES accepts the block but before
            # the acknowledgement is persisted. Release must be idempotent.
            release()
            self.write('released')
            return True
        return False
