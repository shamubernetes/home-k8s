#!/usr/bin/env python3
"""Native Chaptarr database export. Credentials enter via libpq environment.

Use the existing app-specific Chaptarr account, not INIT_POSTGRES_SUPER_PASS.
Each pg_dump is a separate database transaction. No cross-database atomicity
is claimed. Never invoke the old startup-hold helper for this path.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time

DATABASES = ('chaptarr_main', 'chaptarr_log', 'chaptarr_cache')


def capture(destination, timeout=600):
    destination = Path(destination)
    if destination.exists() or destination.is_symlink():
        raise ValueError('destination must be a new generation')
    destination.mkdir(mode=0o700)
    deadline = time.monotonic() + timeout
    records = []
    for database in DATABASES:
        path = destination / (database + '.dump')
        started = time.time()
        # Never put passwords in argv or expose server error text in logs.
        result = subprocess.run(['pg_dump', '--format=custom', '--no-owner',
                                 '--no-acl', '--dbname', database, '--file', str(path)],
                                capture_output=True, timeout=max(1, deadline-time.monotonic()))
        if result.returncode:
            raise RuntimeError('native pg_dump failed for ' + database)
        result = subprocess.run(['pg_restore', '--list', str(path)],
                                capture_output=True, timeout=max(1, deadline-time.monotonic()))
        if result.returncode or not result.stdout or path.stat().st_size == 0:
            raise RuntimeError('native archive validation failed')
        os.chmod(path, 0o600)
        with path.open('rb') as handle:
            os.fsync(handle.fileno())
            digest = hashlib.file_digest(handle, 'sha256').hexdigest()
        records.append({'database': database, 'artifact': path.name,
                        'sha256': digest, 'bytes': path.stat().st_size,
                        'started_at': started, 'completed_at': time.time()})
    manifest = {'version': 1, 'consistency': 'per-database-pg-dump',
                'config_captured': False, 'media_captured': False,
                'restore_verified': False, 'databases': records}
    with (destination / 'manifest.json').open('x') as handle:
        os.chmod(handle.name, 0o600)
        json.dump(manifest, handle, sort_keys=True)
        handle.write('\n')
        handle.flush()
        os.fsync(handle.fileno())
    return manifest


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--destination', required=True)
    args = parser.parse_args()
    capture(args.destination)
