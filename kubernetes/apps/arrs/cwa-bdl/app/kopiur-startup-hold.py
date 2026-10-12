#!/usr/bin/env python3
"""Bounded CWA startup hold, before the pinned image's native /init.

This does not drain a running service. Publish a reviewed absolute expiry in a
separate rollout, prove the old container exited, and bind checks to the new
Pod/container identity before using it in a coordinated Books capture.
"""
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import re
import secrets
import signal
import stat
import time

STATE = Path('/run/kopiur-books-hold')
MAX_HOLD = 1800  # Existing bounded Kopiur maintenance hold, not a capture policy.
UUID = re.compile(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}')
FIELDS = {'pod_uid', 'boot_id', 'pid1_start', 'command_sha256', 'nonce',
          'deadline', 'monotonic_end'}


def identity():
    pod = os.environ.get('KOPIUR_POD_UID', '')
    if not UUID.fullmatch(pod):
        raise ValueError('missing Pod identity')
    # /proc stat's comm can include spaces and parentheses.
    fields = Path('/proc/1/stat').read_text().rsplit(')', 1)[1].split()
    return {'pod_uid': pod,
            'boot_id': Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
            'pid1_start': int(fields[19]),
            'command_sha256': hashlib.sha256(Path('/proc/1/cmdline').read_bytes()).hexdigest()}


def secure(path, directory=False):
    info = path.lstat()
    expected = stat.S_ISDIR if directory else stat.S_ISREG
    if not expected(info.st_mode) or info.st_uid != 0 or stat.S_IMODE(info.st_mode) != (0o700 if directory else 0o600):
        raise ValueError('unsafe hold evidence')


def read_marker(minimum):
    if minimum is not None and (type(minimum) is not int or not 0 < minimum <= MAX_HOLD):
        raise ValueError('invalid remaining time')
    secure(STATE, directory=True)
    secure(STATE / 'active.json')
    marker = json.loads((STATE / 'active.json').read_text())
    if not isinstance(marker, dict) or set(marker) != FIELDS:
        raise ValueError('invalid hold evidence')
    current = identity()
    if any(marker[k] != v for k, v in current.items()):
        raise ValueError('replaced native holder')
    if not isinstance(marker['nonce'], str) or not re.fullmatch(r'[0-9a-f]{64}', marker['nonce']):
        raise ValueError('invalid hold identity')
    if type(marker['deadline']) is not int or type(marker['monotonic_end']) not in (int, float) or not math.isfinite(marker['monotonic_end']):
        raise ValueError('invalid expiry')
    remaining = min(marker['deadline'] - time.time(), marker['monotonic_end'] - time.monotonic())
    if minimum is not None and not minimum <= remaining <= MAX_HOLD:
        raise ValueError('hold expired or too short')
    return marker


def remove_marker():
    # Never follow or remove an attacker-supplied directory or symlink.
    secure(STATE, directory=True)
    try:
        secure(STATE / 'active.json')
        (STATE / 'active.json').unlink()
    except FileNotFoundError:
        pass


def start(deadline):
    if os.getpid() != 1 or os.geteuid() != 0:
        raise ValueError('native startup identity required')
    # Invalid or expired input restores service, but creates no valid fence.
    # Capture must require status/check, never infer pause from deployment Ready.
    if STATE.exists() or STATE.is_symlink():
        remove_marker()
    now = time.time()
    duration = deadline - now
    if not 0 < duration <= MAX_HOLD:
        os.execv('/init', ['/init'])
    STATE.mkdir(mode=0o700, exist_ok=True)
    secure(STATE, directory=True)
    monotonic_end = time.monotonic() + duration
    marker = dict(identity(), nonce=secrets.token_hex(32), deadline=deadline,
                  monotonic_end=monotonic_end)
    pending = STATE / ('pending-' + marker['nonce'])
    fd = os.open(pending, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(marker, stream, sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(pending, STATE / 'active.json')

    def stop(signum, _frame):
        remove_marker()
        raise SystemExit(128 + signum)

    for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(signum, stop)
    try:
        while time.time() < deadline and time.monotonic() < monotonic_end:
            time.sleep(min(0.25, max(0.0, monotonic_end - time.monotonic())))
    finally:
        remove_marker()
    # Do not fork the s6 init. It must retain PID 1 and native signal handling.
    os.execv('/init', ['/init'])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='mode', required=True)
    hold = sub.add_parser('start')
    hold.add_argument('deadline', type=int)
    sub.add_parser('status')
    sub.add_parser('evidence', help='Identity-only evidence, not a capture guard')
    check = sub.add_parser('check')
    check.add_argument('expected', help='Exact nonsecret status JSON from this holder')
    check.add_argument('minimum', type=int)
    args = parser.parse_args()
    if args.mode == 'start':
        start(args.deadline)
    elif args.mode == 'status':
        print(json.dumps(read_marker(1), sort_keys=True))
    elif args.mode == 'evidence':
        try:
            print(json.dumps(read_marker(None), sort_keys=True))
        except FileNotFoundError:
            raise SystemExit(3) from None
    elif read_marker(args.minimum) != json.loads(args.expected):
        raise ValueError('holder identity changed')


if __name__ == '__main__':
    try:
        main()
    except Exception:
        # No environment, path contents, process argv or upstream logs on failure.
        raise SystemExit('CWA startup hold rejected') from None
