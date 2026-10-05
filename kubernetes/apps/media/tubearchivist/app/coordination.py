#!/usr/bin/env python3
"""Bounded admission/drain and generation journal, not a mid-write process freeze.

All lock inodes are permanent. A detached watchdog holds only the attempt lock,
never the writer lock. It revokes a generation before killing its collector and
reopening admission. Publication is serialized against revocation. Transports
run after resume and may only consume the closed generation, never source files.
"""
import argparse
import asyncio
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import uuid

ROOT = Path(os.environ.get('TA_COORDINATION_DIR', '/cache/kopiur-coordination'))
TERMINAL = {'RESUMED', 'REVOKED'}
MAX_HOLD = 60.0


def check(condition, message):
    if not condition:
        raise RuntimeError(message)


def identity(pid=None):
    pid = pid or os.getpid()
    try:
        # comm can contain spaces and parentheses. starttime is field 22.
        fields = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
        if fields[0] == 'Z':
            return None
        return {'pid': pid, 'start': fields[19],
                'boot': Path('/proc/sys/kernel/random/boot_id').read_text().strip()}
    except FileNotFoundError:
        return None


def alive(owner):
    return bool(owner) and identity(owner['pid']) == owner


def atomic(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        try:
            json.dump(value, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)
    fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def read(path, default=None):
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return default


@contextmanager
def lock(name, exclusive=True, blocking=True):
    ROOT.mkdir(parents=True, exist_ok=True)
    fd = os.open(ROOT / name, os.O_CREAT | os.O_RDWR | os.O_CLOEXEC, 0o600)
    mode = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
    try:
        fcntl.flock(fd, mode | (0 if blocking else fcntl.LOCK_NB))
        yield fd
    finally:
        os.close(fd)


def current():
    return read(ROOT / 'current.json')


def expired(state):
    return (not alive(state['owner']) or state['owner']['boot'] != identity()['boot']
            or time.monotonic() >= state['deadline'])


def terminate(owner):
    if alive(owner):
        # pidfd prevents killing a reused PID between identity check and signal.
        try:
            fd = os.pidfd_open(owner['pid'])
        except ProcessLookupError:
            return
        try:
            if identity(owner['pid']) == owner:
                signal.pidfd_send_signal(fd, signal.SIGKILL)
        finally:
            os.close(fd)


def revoke(generation):
    with lock('state.lock'):
        state = current()
        if (not state or state['generation'] != generation or state['phase'] == 'RESUMED'
                or (state['phase'] == 'REVOKED' and state.get('admission') == 'OPEN')):
            return
        state['phase'] = 'REVOKED'
        state['revoked_at'] = time.monotonic()
        atomic(ROOT / 'current.json', state)
        atomic(ROOT / 'history' / (generation + '.json'), state)
        # Marking REVOKED first makes any staged collector output inadmissible.
        collector = state.get('collector')
    if collector:
        terminate(collector)
    if state['owner'] != identity():
        terminate(state['owner'])
    with lock('state.lock'):
        state = current()
        if state and state['generation'] == generation:
            state['admission'] = 'OPEN'
            atomic(ROOT / 'current.json', state)
            atomic(ROOT / 'history' / (generation + '.json'), state)


def recover():
    state = current()
    if state and ((state['phase'] == 'REVOKED' and state.get('admission') != 'OPEN')
                  or (state['phase'] not in TERMINAL and expired(state))):
        revoke(state['generation'])


def writer_fd():
    """Acquire admission and a shared lease atomically against closure."""
    while True:
        recover()
        with lock('state.lock'):
            state = current()
            if not state or state.get('admission') == 'OPEN':
                fd = os.open(ROOT / 'writers.lock', os.O_CREAT | os.O_RDWR | os.O_CLOEXEC, 0o600)
                try:
                    fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
                    return fd
                except BlockingIOError:
                    os.close(fd)
        time.sleep(0.025)


@contextmanager
def writer():
    fd = writer_fd()
    try:
        yield
    finally:
        os.close(fd)


async def admitted(application, scope, receive, send):
    """Hold through Django ASGI disconnect cleanup, including sync threads.

    Cancellation during to_thread admission must still close the eventual FD.
    Django's qualified ASGI handler awaits its thread-sensitive context on exit.
    """
    admission = asyncio.create_task(asyncio.to_thread(writer_fd))
    fd = None
    try:
        try:
            fd = await asyncio.shield(admission)
        except asyncio.CancelledError:
            fd = await admission
            raise
        await application(scope, receive, send)
    finally:
        if fd is not None:
            os.close(fd)


def watchdog(generation, attempt_fd):
    # Keep the serial attempt lock until recovery is complete, including after
    # controller SIGKILL. CLOEXEC writer/state descriptors are never inherited.
    try:
        while True:
            state = current()
            if not state or state['generation'] != generation or state['phase'] == 'RESUMED':
                return
            if state['phase'] == 'REVOKED':
                if state.get('admission') != 'OPEN':
                    revoke(generation)
                return
            if expired(state):
                revoke(generation)
                return
            time.sleep(0.025)
    finally:
        os.close(attempt_fd)


def require_generation(generation, phase=None):
    state = current()
    check(state and state['generation'] == generation and state['phase'] not in TERMINAL,
          'capture generation revoked')
    check(not expired(state), 'capture deadline expired')
    if phase:
        check(state['phase'] == phase, 'wrong capture phase')
    return state


def resume(generation, success=False):
    with lock('state.lock'):
        state = current()
        check(state and state['generation'] == generation, 'wrong resume generation')
        if state['phase'] == 'REVOKED':
            return state
        state.update(phase='RESUMED' if success else 'REVOKED', admission='OPEN',
                     resumed_at=time.monotonic())
        atomic(ROOT / 'current.json', state)
        atomic(ROOT / 'history' / (generation + '.json'), state)
        return state


def coordinate(destination, collect, drain_seconds=20.0, hold_seconds=30.0):
    """Collect(argv) writes a new directory passed as its last argument.

    Admission closes for at most drain+hold, capped at MAX_HOLD. Every attempt
    has its own directory and manifest. No failed attempt can replace a prior
    completed generation. A collector cannot publish itself as coherent.
    """
    check(0 < drain_seconds and 0 < hold_seconds and drain_seconds + hold_seconds <= MAX_HOLD,
          'invalid bounded capture deadline')
    destination = destination.resolve()
    check(not destination.exists(), 'generation destination already exists')
    recover()
    with lock('attempt.lock', blocking=False) as attempt_fd:
        generation = uuid.uuid4().hex
        state = {'format': 'writer-generation-v1', 'generation': generation,
                 'owner': identity(), 'admission': 'CLOSED', 'phase': 'DRAINING',
                 'started_at': time.monotonic(), 'deadline': time.monotonic() + drain_seconds + hold_seconds,
                 'destination_sha256': hashlib.sha256(str(destination).encode()).hexdigest()}
        with lock('state.lock'):
            prior = current()
            check(not prior or prior['phase'] in TERMINAL, 'another capture is active')
            atomic(ROOT / 'current.json', state)
        watcher = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), 'watchdog',
                                    generation, str(attempt_fd)], pass_fds=(attempt_fd,),
                                   start_new_session=True, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        with lock('state.lock'):
            state = require_generation(generation, 'DRAINING')
            state['watchdog'] = identity(watcher.pid)
            atomic(ROOT / 'current.json', state)
        exclusive = os.open(ROOT / 'writers.lock', os.O_CREAT | os.O_RDWR | os.O_CLOEXEC, 0o600)
        stage = destination.parent / ('.generation-' + generation)
        collector = None
        success = False
        try:
            drain_deadline = time.monotonic() + drain_seconds
            while True:
                require_generation(generation)
                drained = read(ROOT / 'drained.json')
                if drained and drained.get('generation') == generation:
                    check(drained.get('beat_clean') is True, 'beat state was not durably drained')
                    check(alive(drained.get('supervisor')), 'writer supervisor is not alive')
                    try:
                        fcntl.flock(exclusive, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        pass
                check(time.monotonic() < drain_deadline, 'writer drain timeout')
                time.sleep(0.025)
            with lock('state.lock'):
                state = require_generation(generation, 'DRAINING')
                state.update(phase='CAPTURING', frozen_at=time.monotonic(),
                             deadline=min(state['deadline'], time.monotonic() + hold_seconds))
                atomic(ROOT / 'current.json', state)
                destination.parent.mkdir(parents=True, exist_ok=True)
                collector = subprocess.Popen([*collect, str(stage)], start_new_session=True)
                state['collector'] = identity(collector.pid)
                atomic(ROOT / 'current.json', state)
            while collector.poll() is None:
                require_generation(generation, 'CAPTURING')
                time.sleep(0.025)
            check(collector.returncode == 0, 'collector failed')
            with lock('state.lock'):
                state = require_generation(generation, 'CAPTURING')
                # Validity is an immutable commit record inside the generation,
                # not an early COMPLETE marker from one independently copied store.
                manifest = read(stage / 'manifest.json')
                check(manifest is not None, 'collector manifest missing')
                commit = {'format': 'coherent-generation-v1', 'generation': generation,
                          'manifest_sha256': hashlib.sha256((stage / 'manifest.json').read_bytes()).hexdigest(),
                          'boundary': 'all qualified native writers drained, exclusive admission',
                          'started_at': state['started_at'], 'frozen_at': state['frozen_at'],
                          'captured_at': time.monotonic()}
                atomic(stage / 'generation.json', commit)
                stage.rename(destination)
                fd = os.open(destination.parent, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
                state.update(phase='COMMITTED', committed_at=commit['captured_at'])
                atomic(ROOT / 'current.json', state)
                # Release before admission reopens. No upload runs under this lease.
                os.close(exclusive)
                exclusive = None
                state.update(phase='RESUMED', admission='OPEN', resumed_at=time.monotonic())
                atomic(ROOT / 'current.json', state)
                atomic(ROOT / 'history' / (generation + '.json'), state)
                success = True
            return state
        finally:
            if collector and collector.poll() is None:
                terminate(state.get('collector'))
                collector.wait(timeout=5)
            if exclusive is not None:
                os.close(exclusive)
            if not success:
                revoke(generation)
            watcher.wait(timeout=5)


def verify_generation(bundle):
    commit = read(bundle / 'generation.json')
    check(commit and commit['format'] == 'coherent-generation-v1', 'coherent generation missing')
    check(hashlib.sha256((bundle / 'manifest.json').read_bytes()).hexdigest() == commit['manifest_sha256'],
          'coherent manifest differs')
    check(commit['started_at'] <= commit['frozen_at'] <= commit['captured_at'], 'invalid capture timing')
    check(commit['captured_at'] - commit['started_at'] <= MAX_HOLD, 'unbounded capture generation')
    return commit


if __name__ == '__main__':
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='operation', required=True)
    w = sub.add_parser('watchdog')
    w.add_argument('generation')
    w.add_argument('fd', type=int)
    c = sub.add_parser('capture')
    c.add_argument('destination', type=Path)
    c.add_argument('--drain', type=float, default=20)
    c.add_argument('--hold', type=float, default=30)
    args = parser.parse_args()
    if args.operation == 'watchdog':
        watchdog(args.generation, args.fd)
    else:
        result = coordinate(args.destination, [sys.executable, str(Path(__file__).with_name('backup.py')),
                                              'collect-coherent'], args.drain, args.hold)
        print(json.dumps({key: result[key] for key in ('generation', 'phase', 'started_at', 'frozen_at', 'resumed_at')}))
