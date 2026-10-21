#!/usr/bin/env python3
"""Export individually consistent CWA SQLite databases, without writer fences.

Run against the config mount, never against the ingest NFS tree. This bundle
is NOT a transaction across databases or between metadata and library bytes.
The caller must retain the later immutable CSI point and qualify that skew.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time

DATABASES = ('app.db', 'cwa.db', 'gdrive.db', 'calibre-library/metadata.db',
             'calibre-library/.calnotes/notes.db')


def capture(root, destination, timeout=90):
    root = Path(root).resolve(strict=True)
    destination = Path(destination)
    if destination.exists() or destination.is_symlink():
        raise ValueError('destination must be a new private generation')
    destination.mkdir(mode=0o700)
    deadline = time.monotonic() + timeout
    records = []

    def progress(*_):
        if time.monotonic() >= deadline:
            raise TimeoutError('online SQLite capture deadline')

    for relative in DATABASES:
        source = root / relative
        if source.resolve(strict=True) != source or not source.is_file() or source.stat().st_size == 0:
            raise ValueError('unsafe database path')
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        started = time.time()
        origin = sqlite3.connect(source.as_uri() + '?mode=ro', uri=True)
        backup = sqlite3.connect(target)
        try:
            origin.backup(backup, pages=128, progress=progress, sleep=0.05)
            backup.execute('PRAGMA journal_mode=DELETE')
            if backup.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
                raise ValueError('SQLite integrity check failed')
        finally:
            backup.close()
            origin.close()
        os.chmod(target, 0o600)
        with target.open('rb') as handle:
            os.fsync(handle.fileno())
            digest = hashlib.file_digest(handle, 'sha256').hexdigest()
        progress()
        records.append({'path': relative, 'sha256': digest,
                        'bytes': target.stat().st_size, 'started_at': started,
                        'completed_at': time.time()})
    manifest = {'version': 1, 'consistency': 'per-database-online',
                'library_bytes_captured': False, 'ingest_captured': False,
                'databases': records}
    # A failed attempt leaves no manifest and is never a valid generation.
    with (destination / 'manifest.json').open('x') as handle:
        os.chmod(handle.name, 0o600)
        json.dump(manifest, handle, sort_keys=True)
        handle.write('\n')
        handle.flush()
        os.fsync(handle.fileno())
    fd = os.open(destination, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    return manifest


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', default='/config')
    parser.add_argument('--destination', required=True)
    args = parser.parse_args()
    capture(args.root, args.destination)
