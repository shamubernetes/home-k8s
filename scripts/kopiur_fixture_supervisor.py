#!/usr/bin/env python3
"""Root-owned cgroup-v2 command boundary for disposable ARC fixtures only.

This is not a production adapter. Workers have no cgroup write authority,
receive no inherited controller descriptors, and cannot dispatch before the
root supervisor has durably registered them. Revocation survives restart.
"""

import contextlib
import ctypes
import fcntl
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid


class BoundaryError(RuntimeError):
    """The supervisor cannot prove safe dispatch or complete cessation."""


# The launcher drops privilege before waiting, including on an abandoned gate.
_LAUNCHER = """
import ctypes, os, sys
fd = int(sys.argv[1])
libc = ctypes.CDLL(None, use_errno=True)
if libc.prctl(38, 1, 0, 0, 0) != 0:
    raise OSError(ctypes.get_errno(), 'no_new_privs')
os.setgroups([])
os.setgid(65534)
os.setuid(65534)
try:
    admitted = os.read(fd, 1) == b'G'
finally:
    os.close(fd)
if not admitted:
    sys.exit(125)
os.execv(sys.argv[2], sys.argv[2:])
"""


def _boot():
    return Path('/proc/sys/kernel/random/boot_id').read_text().strip()


def _identity(path):
    _require_cgroup2(path)
    st = path.stat()
    return {'device': st.st_dev, 'inode': st.st_ino, 'boot': _boot()}


def _require_cgroup2(path):
    # A directory of lookalike files must never become affirmative kernel
    # evidence. Linux statfs places f_type first; reserve ample native storage.
    libc = ctypes.CDLL(None, use_errno=True)
    buffer = ctypes.create_string_buffer(512)
    if libc.statfs(os.fsencode(path), ctypes.byref(buffer)) != 0:
        raise OSError(ctypes.get_errno(), 'statfs')
    if ctypes.c_long.from_buffer(buffer).value != 0x63677270:
        raise BoundaryError('real kernel cgroup-v2 filesystem required')


def _persist(path, state):
    temporary = path.with_name('.' + uuid.uuid4().hex)
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as stream:
            json.dump(state, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        temporary.unlink(missing_ok=True)


class FixtureSupervisor:
    """One UUID-owned cgroup and durable dispatch/revoke journal.

    A trusted isolated privileged supervisor owns this object. Native fixture
    commands run as nobody with no_new_privs. The journal directory is private
    and must never be mounted in worker containers or shared with production.
    """

    directory: Path
    path: Path
    lock: Path
    root: Path

    def __init__(self, directory, cgroup_root: str | Path = '/sys/fs/cgroup'):
        if sys.platform != 'linux' or os.geteuid() != 0:
            raise BoundaryError('isolated Linux root supervisor required')
        self.directory = Path(directory).resolve(strict=True)
        st = self.directory.stat()
        if st.st_uid != 0 or st.st_mode & 0o077:
            raise BoundaryError('journal directory must be root-owned private')
        self.path = self.directory / 'supervisor.json'
        self.lock = self.directory / 'supervisor.lock'
        self.root = Path(cgroup_root).resolve(strict=True)
        _require_cgroup2(self.root)
        if not (self.root / 'cgroup.controllers').is_file():
            raise BoundaryError('cgroup v2 required')

    @contextlib.contextmanager
    def _locked(self):
        fd = os.open(self.lock, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            deadline = time.monotonic() + 2
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise BoundaryError('supervisor state lock timeout')
                    time.sleep(0.01)
            yield
        finally:
            os.close(fd)

    def _read(self):
        state = json.loads(self.path.read_text())
        if state['version'] != 1 or state['fixture_only'] is not True:
            raise BoundaryError('unsupported supervisor journal')
        if str(uuid.UUID(state['generation'])) != state['generation']:
            raise BoundaryError('invalid fixture generation')
        if state['root'] != str(self.root):
            raise BoundaryError('cgroup root changed')
        boundary = self.root / ('kopiur-fixture-' + state['generation'])
        if boundary.is_symlink() or _identity(boundary) != state['boundary']:
            raise BoundaryError('cgroup identity changed, cessation unresolved')
        return state, boundary

    def begin(self):
        with self._locked():
            if self.path.exists():
                raise BoundaryError('existing generation must not be overwritten')
            generation = str(uuid.uuid4())
            boundary = self.root / ('kopiur-fixture-' + generation)
            boundary.mkdir(mode=0o755)
            state = {'version': 1, 'fixture_only': True,
                     'generation': generation, 'root': str(self.root),
                     'boundary': _identity(boundary), 'revoked': False,
                     'commands': [], 'revision': 0}
            _persist(self.path, state)
            return generation

    def dispatch(self, argv):
        if not argv or not Path(argv[0]).is_absolute():
            raise BoundaryError('absolute executable required')
        with self._locked():
            state, boundary = self._read()
            if state['revoked']:
                raise BoundaryError('generation revoked, queued start denied')
            read_fd, write_fd = os.pipe()
            child = None
            admitted = False
            try:
                child = subprocess.Popen(
                    [sys.executable, '-c', _LAUNCHER, str(read_fd), *argv],
                    pass_fds=(read_fd,), close_fds=True, start_new_session=True,
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL, cwd='/',
                    env={'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8'})
                os.close(read_fd)
                read_fd = -1
                (boundary / 'cgroup.procs').write_text(str(child.pid))
                if str(child.pid) not in (boundary / 'cgroup.procs').read_text().split():
                    raise BoundaryError('gated child not in owned cgroup')
                stat = Path(f'/proc/{child.pid}/stat').read_text().rsplit(')', 1)[1].split()
                command = {'operation': str(uuid.uuid4()), 'pid': child.pid,
                           'start_ticks': stat[19], 'session': stat[3],
                           'group': stat[2], 'boundary': state['boundary'],
                           'stage': 'registered'}
                state['commands'].append(command)
                state['revision'] += 1
                _persist(self.path, state)
                # The permanent state lock serializes gate opening and revoke.
                os.write(write_fd, b'G')
                admitted = True
                return child, command
            finally:
                if read_fd >= 0:
                    os.close(read_fd)
                os.close(write_fd)
                if child is not None and not admitted:
                    child.wait(timeout=2)
                # EOF refuses dispatch if registration failed. A bounded wait
                # reaps the non-admitted launcher, never a native command.

    def revoke(self):
        with self._locked():
            state, _ = self._read()
            if not state['revoked']:
                state['revoked'] = True
                state['revision'] += 1
                _persist(self.path, state)
            return state['generation']

    def cease(self, timeout=5):
        if not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
            raise BoundaryError('finite positive cessation timeout required')
        self.revoke()
        # cgroup.kill includes all descendants, including setsid double forks.
        # A changed boot or inode is NEVER interpreted as cessation.
        with self._locked():
            state, boundary = self._read()
        (boundary / 'cgroup.kill').write_text('1')
        deadline = time.monotonic() + timeout
        while True:
            with self._locked():
                current, boundary = self._read()
                events = dict(line.split() for line in
                              (boundary / 'cgroup.events').read_text().splitlines())
                if events.get('populated') == '0':
                    proof = {'generation': current['generation'],
                             'boundary': current['boundary'],
                             'populated': 0, 'revoked': True,
                             'registered_operations': [c['operation'] for c in current['commands']]}
                    current['cessation'] = proof
                    current['revision'] += 1
                    _persist(self.path, current)
                    return proof
            if time.monotonic() >= deadline:
                raise BoundaryError('complete boundary remains populated')
            time.sleep(0.01)

    def verify_cessation(self):
        with self._locked():
            state, boundary = self._read()
            events = dict(line.split() for line in
                          (boundary / 'cgroup.events').read_text().splitlines())
            proof = state.get('cessation')
            if not state['revoked'] or not proof or events.get('populated') != '0':
                raise BoundaryError('no authoritative complete-boundary cessation')
            if proof['boundary'] != state['boundary'] or proof['generation'] != state['generation']:
                raise BoundaryError('cessation belongs to another boundary')
            return proof
