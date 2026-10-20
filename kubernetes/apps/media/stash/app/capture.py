#!/usr/bin/env python3
"""Online SQLite backup with stable file-backed blobs, metadata and extensions."""
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import sqlite3
import stat
import tarfile
import time


def inventory(roots):
    result = {}
    for label, root in roots.items():
        if not root.is_dir() or root.is_symlink():
            raise ValueError('required directory unavailable')
        for base, dirs, files in os.walk(root, followlinks=False, onerror=lambda e: (_ for _ in ()).throw(e)):
            for name in sorted(dirs + files):
                p = Path(base) / name
                s = p.lstat()
                if not (stat.S_ISREG(s.st_mode) or stat.S_ISDIR(s.st_mode)):
                    raise ValueError('unsupported state object')
                result[label + '/' + str(p.relative_to(root))] = (
                    s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns, s.st_mode)
    return result


def backup_sqlite(source, target):
    if source.is_symlink() or not source.is_file() or not source.stat().st_size:
        raise ValueError('required database unavailable')
    with contextlib.closing(sqlite3.connect(source.as_uri() + '?mode=ro', uri=True)) as origin:
        with contextlib.closing(sqlite3.connect(target)) as destination:
            origin.backup(destination, pages=256, sleep=0.1)
            destination.execute('PRAGMA journal_mode=DELETE')
            if destination.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
                raise ValueError('database integrity failed')


def capture(config=Path('/config'), data=Path('/data'), plugins=Path('/plugins'), scrapers=Path('/scrapers')):
    os.umask(0o077)
    output = data / '.kopiur-native'
    if output.is_symlink():
        raise ValueError('unsafe output')
    output.mkdir(mode=0o700, exist_ok=True)
    if (output / 'lock').is_symlink():
        raise ValueError('unsafe lock')
    with (output / 'lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        (output / 'COMPLETE').unlink(missing_ok=True)
        pending = output / 'pending'
        shutil.rmtree(pending, ignore_errors=False) if pending.exists() else None
        pending.mkdir(mode=0o700)
        # Exclude the DB and sidecars from file state. The online API copies it.
        roots = {'blobs': data / 'blobs', 'metadata': data / 'metadata',
                 'plugins': plugins, 'scrapers': scrapers}
        before = inventory(roots)
        required = sum(value[1] for value in before.values() if stat.S_ISREG(value[4]))
        required += (config / 'stash-go.sqlite').stat().st_size * 2
        usage = shutil.disk_usage(data)
        if usage.free - required < max(usage.total // 10, 128 * 1024 * 1024):
            raise ValueError('insufficient native bundle headroom')
        cfg = config / 'config.yml'
        if cfg.is_symlink() or not cfg.is_file():
            raise ValueError('configuration unavailable')
        config_before = cfg.read_bytes()
        backup_sqlite(config / 'stash-go.sqlite', pending / 'stash-go.sqlite')
        (pending / 'config.yml').write_bytes(config_before)
        with tarfile.open(pending / 'state.tar', 'w') as archive:
            for label, root in roots.items():
                archive.add(root, arcname=label, recursive=True)
        if before != inventory(roots) or config_before != cfg.read_bytes():
            raise ValueError('file state changed during native capture')
        (pending / 'metadata.json').write_text(json.dumps({
            'format': 1, 'application': 'stash', 'completed_at': time.time(),
            'consistency': 'online-sqlite-stable-filetree',
            'regeneratable_omitted': ['generated', 'cache'],
        }))
        checksums = {}
        for p in pending.iterdir():
            with p.open('rb') as handle:
                checksums[p.name] = hashlib.file_digest(handle, 'sha256').hexdigest()
                os.fsync(handle.fileno())
        (pending / 'checksums.json').write_text(json.dumps(checksums, sort_keys=True))
        with (pending / 'checksums.json').open('rb') as handle:
            os.fsync(handle.fileno())
        previous = output / 'previous'
        if previous.exists():
            shutil.rmtree(previous)
        fd = os.open(pending, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        if (output / 'current').exists():
            (output / 'current').rename(previous)
        pending.rename(output / 'current')
        (output / 'COMPLETE').write_text('complete\n')
        with (output / 'COMPLETE').open('rb') as handle:
            os.fsync(handle.fileno())
        fd = os.open(output, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


if __name__ == '__main__':
    def expired(*_):
        raise TimeoutError('native capture deadline')
    signal.signal(signal.SIGALRM, expired)
    signal.alarm(540)
    try:
        capture()
    except Exception:
        # Driver and filesystem exceptions may contain private application data.
        raise SystemExit('native Stash capture failed') from None
