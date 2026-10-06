#!/usr/bin/env python3
"""Root-owned cgroup-v2 command boundary for disposable ARC fixtures only.

This is not a production adapter. Workers have no cgroup write authority,
receive no inherited controller descriptors, and cannot dispatch before the
root supervisor has durably registered them. Revocation survives restart.
"""

import contextlib
import copy
import ctypes
import fcntl
import json
import math
import os
from pathlib import Path
import select
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
    _children: dict

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
        self._children = {}
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

    def _revocation_path(self, state):
        return self.directory / ('revoked-' + state['generation'] + '.json')

    def _watchdog_revoked(self, state):
        path = self._revocation_path(state)
        if path.is_symlink():
            raise BoundaryError('independent revocation identity changed')
        if not path.exists():
            return False
        expected = {'version': 1, 'fixture_only': True,
                    'generation': state['generation'], 'root': state['root'],
                    'boundary': state['boundary'], 'revoked': True}
        if json.loads(path.read_text()) != expected:
            raise BoundaryError('independent revocation identity changed')
        return True

    def admit_watchdog(self, owner_pid, timeout):
        """Bind one immutable owner/deadline before any fixture dispatch."""
        if (type(owner_pid) is not int or owner_pid <= 0
                or type(timeout) not in (int, float) or not math.isfinite(timeout)
                or timeout <= 0):
            raise BoundaryError('explicit owner and finite watchdog deadline required')
        with self._locked():
            state, _ = self._read()
            path = self.directory / 'watchdog.json'
            if (path.exists() or state['commands'] or state['revoked']
                    or self._watchdog_revoked(state)):
                raise BoundaryError('watchdog admission requires unused generation')
            ticks = Path(f'/proc/{owner_pid}/stat').read_text().rsplit(')', 1)[1].split()[19]
            contract = {'generation': state['generation'], 'boundary': state['boundary'],
                        'owner_pid': owner_pid, 'owner_start_ticks': ticks,
                        'deadline_monotonic': time.monotonic() + timeout}
            _persist(path, contract)
            if self._watchdog_revoked(state):
                raise BoundaryError('watchdog generation revoked during admission')
            return copy.deepcopy(contract)

    def monitor_watchdog(self):
        """Independent process waits for admitted owner death or deadline.

        It never signals a numeric owner PID or needs its state lock. A reused
        PID is owner loss, not authority to terminate another process. The
        immutable boundary includes boot identity, so monotonic time survives
        watchdog restart only within that same proven boot.
        """
        state, _ = self._read()
        contract = json.loads((self.directory / 'watchdog.json').read_text())
        if (contract['generation'] != state['generation']
                or contract['boundary'] != state['boundary']
                or type(contract['owner_pid']) is not int or contract['owner_pid'] <= 0
                or type(contract['deadline_monotonic']) not in (int, float)
                or not math.isfinite(contract['deadline_monotonic'])
                or contract['deadline_monotonic'] <= 0
                or not isinstance(contract['owner_start_ticks'], str)
                or not contract['owner_start_ticks'].isdigit()):
            raise BoundaryError('watchdog contract identity changed')
        pidfd = None
        try:
            try:
                pidfd = getattr(os, 'pidfd_open')(contract['owner_pid'])
                ticks = Path(f'/proc/{contract["owner_pid"]}/stat').read_text().rsplit(')', 1)[1].split()[19]
            except ProcessLookupError:
                return self.watchdog_cease()
            except FileNotFoundError:
                return self.watchdog_cease()
            if ticks != contract['owner_start_ticks']:
                return self.watchdog_cease()
            poller = select.poll()
            poller.register(pidfd, select.POLLIN)
            while True:
                remaining = contract['deadline_monotonic'] - time.monotonic()
                if remaining <= 0:
                    break
                # poll has no FD_SETSIZE limitation. Round up to avoid an early
                # deadline, and cap each wait at its signed integer limit.
                if poller.poll(min(math.ceil(remaining * 1000), 2147483647)):
                    break
            return self.watchdog_cease()
        except BaseException:
            # A monitoring failure is never permission to abandon live workers.
            self.watchdog_cease()
            raise
        finally:
            if pidfd is not None:
                os.close(pidfd)

    def watchdog_cease(self, timeout=5):
        """Revoke and freeze the exact boundary without acquiring owner locks.

        The separate permanent tombstone cannot be overwritten by an owner's
        stale supervisor.json replacement. The frozen boundary is never thawed,
        so a dispatch paused between its last check and gate write cannot run
        after cessation. This proof does NOT restore consumer admission or
        reconcile the command cohort. Those still need owner-lock recovery.
        """
        if (type(timeout) not in (int, float) or not math.isfinite(timeout)
                or timeout <= 0):
            raise BoundaryError('finite positive watchdog timeout required')
        state, boundary = self._read()
        revoked = {'version': 1, 'fixture_only': True,
                   'generation': state['generation'], 'root': state['root'],
                   'boundary': state['boundary'], 'revoked': True}
        if self._revocation_path(state).exists():
            self._watchdog_revoked(state)
        # Every retry repeats both durability barriers before kernel mutation.
        _persist(self._revocation_path(state), revoked)
        (boundary / 'cgroup.freeze').write_text('1')
        deadline = time.monotonic() + timeout
        while True:
            current, checked = self._read()
            if checked != boundary or current['boundary'] != state['boundary']:
                raise BoundaryError('watchdog boundary changed')
            if not self._watchdog_revoked(current):
                raise BoundaryError('independent revocation disappeared')
            events = dict(line.split() for line in
                          (boundary / 'cgroup.events').read_text().splitlines())
            # Kill remains effective against frozen descendants. Repeat it for
            # a gated launcher added by a dispatch already in progress.
            (boundary / 'cgroup.kill').write_text('1')
            if events.get('frozen') == '1' and events.get('populated') == '0':
                return self.verify_watchdog_cessation()
            if time.monotonic() >= deadline:
                raise BoundaryError('watchdog boundary cessation unresolved')
            time.sleep(0.01)

    def verify_watchdog_cessation(self):
        """Fresh kernel proof, never an admission-restoration authorization."""
        state, boundary = self._read()
        if not self._watchdog_revoked(state):
            raise BoundaryError('no durable independent revocation')
        revoked = json.loads(self._revocation_path(state).read_text())
        _persist(self._revocation_path(state), revoked)
        events = dict(line.split() for line in
                      (boundary / 'cgroup.events').read_text().splitlines())
        if (events.get('frozen') != '1' or events.get('populated') != '0'
                or (boundary / 'cgroup.freeze').read_text().strip() != '1'):
            raise BoundaryError('independent frozen boundary remains unresolved')
        return {'generation': state['generation'], 'boundary': state['boundary'],
                'revoked': True, 'frozen': True, 'populated': 0,
                'consumer_admission_restored': False,
                'production_recovery_accepted': False}

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
            if state['revoked'] or state.get('sealed', False) or self._watchdog_revoked(state):
                raise BoundaryError('revoked generation: queued start denied')
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
                           'argv': list(argv),
                           'start_ticks': stat[19], 'session': stat[3],
                           'group': stat[2], 'boundary': state['boundary'],
                           'stage': 'registered'}
                state['commands'].append(command)
                state['revision'] += 1
                _persist(self.path, state)
                # Independent watchdog revocation is outside this owner lock.
                # Its permanent freeze also covers a race after this check.
                if self._watchdog_revoked(state):
                    raise BoundaryError('revoked generation: queued start denied')
                os.write(write_fd, b'G')
                admitted = True
                self._children[command['operation']] = (child, copy.deepcopy(command))
                return child, copy.deepcopy(command)
            finally:
                if read_fd >= 0:
                    os.close(read_fd)
                os.close(write_fd)
                if child is not None and not admitted:
                    # A delayed insertion can join an already frozen boundary.
                    # Pipe EOF cannot wake it there. Kill the exact owned Popen
                    # for cleanup; this is not complete-boundary release proof.
                    if self._revocation_path(state).exists():
                        child.kill()
                    child.wait(timeout=2)
                # EOF refuses dispatch if registration failed. A bounded wait
                # reaps the non-admitted launcher, never a native command.

    def complete(self, operation, timeout=5):
        """Persist success only from an owned child and an empty native boundary.

        Waiting never holds the state lock. A restarted supervisor cannot infer
        success from an absent PID, and must revoke/recover an unknown outcome.
        This receipt does not authorize consumer admission or production use.
        """
        if (type(timeout) not in (int, float) or not math.isfinite(timeout)
                or timeout <= 0):
            raise BoundaryError('finite positive completion timeout required')
        owned = self._children.get(operation)
        if owned is None:
            raise BoundaryError('native outcome unknown after supervisor restart')
        child, registration = owned
        # This Popen belongs to this supervisor, not a caller-supplied exit code.
        if child.wait(timeout=timeout) != 0:
            raise BoundaryError('native command did not exit successfully')
        with self._locked():
            state, boundary = self._read()
            if state['revoked'] or self._watchdog_revoked(state):
                raise BoundaryError('revoked generation cannot complete')
            commands = [item for item in state['commands'] if item['operation'] == operation]
            if len(commands) != 1:
                raise BoundaryError('completion requires exact registered operation')
            command = commands[0]
            if command['stage'] not in ('registered', 'completed'):
                raise BoundaryError('native registration stage changed')
            expected = copy.deepcopy(command)
            expected.pop('completion', None)
            expected['stage'] = 'registered'
            if expected != registration:
                raise BoundaryError('native registration changed')
            events = dict(line.split() for line in
                          (boundary / 'cgroup.events').read_text().splitlines())
            if events.get('populated') != '0':
                raise BoundaryError('native descendants remain populated')
            proof = {'generation': state['generation'], 'boundary': state['boundary'],
                     'operation': operation, 'populated': 0, 'returncode': 0,
                     'registered_operations': [item['operation'] for item in state['commands']]}
            if command.get('completion') is not None:
                if command['completion'] != proof:
                    raise BoundaryError('completion cohort changed')
                # Retry must repeat fsync, including after replace succeeded but
                # directory durability acknowledgement was lost.
            command['stage'] = 'completed'
            command['completion'] = proof
            state['revision'] += 1
            _persist(self.path, state)
            if self._watchdog_revoked(state):
                raise BoundaryError('revoked generation cannot complete')
            return copy.deepcopy(proof)

    def verify_completion(self, operation):
        """Re-read durable completion; revocation or later dispatch invalidates it."""
        with self._locked():
            state, boundary = self._read()
            commands = [item for item in state['commands'] if item['operation'] == operation]
            if (state['revoked'] or self._watchdog_revoked(state)
                    or len(commands) != 1 or commands[0]['stage'] != 'completed'):
                raise BoundaryError('no current successful native completion')
            proof = commands[0].get('completion')
            events = dict(line.split() for line in
                          (boundary / 'cgroup.events').read_text().splitlines())
            if (not proof or proof['generation'] != state['generation']
                    or proof['boundary'] != state['boundary'] or proof['operation'] != operation
                    or proof['returncode'] != 0 or proof['populated'] != 0
                    or events.get('populated') != '0'
                    or proof['registered_operations'] !=
                    [item['operation'] for item in state['commands']]):
                raise BoundaryError('completion boundary or cohort changed')
            # Visible bytes after a failed replace acknowledgement are not yet
            # durability proof. Re-establish both barriers before returning.
            _persist(self.path, state)
            if self._watchdog_revoked(state):
                raise BoundaryError('no current successful native completion')
            return copy.deepcopy(proof)

    def revoke(self):
        with self._locked():
            state, _ = self._read()
            if not state['revoked']:
                state['revoked'] = True
                state['revision'] += 1
            # Visible revoked bytes after a lost directory-fsync acknowledgement
            # are not durable revocation. Every retry repeats both barriers
            # before cessation can kill the owned boundary or restore admission.
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
