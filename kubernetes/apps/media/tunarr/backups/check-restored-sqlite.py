#!/usr/bin/env python3
"""Check SQLite recovery from an isolated restored rollback volume.

Never mount the original PVC here. Do not start Tunarr or Meilisearch. A pass
checks SQLite only, not Meilisearch or a successful application boot.
"""
import argparse
import json
import os
from pathlib import Path
import sqlite3


def strict_paths(root):
    def fail(error):
        raise error
    for directory, dirs, files in os.walk(root, onerror=fail, followlinks=False):
        for name in sorted(dirs + files):
            path = Path(directory) / name
            if path.is_symlink():
                raise ValueError('restore tree must not contain symlinks')
            yield path


def check(root):
    root = Path(root).resolve(strict=True)
    records = []
    for path in strict_paths(root):
        if not path.is_file():
            continue
        with path.open('rb') as handle:
            if handle.read(16) != b'SQLite format 3\x00':
                continue
        # mode=ro preserves and reads a restored WAL; immutable=1 would ignore it.
        db = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)
        try:
            db.execute('PRAGMA query_only=ON')
            if db.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
                raise ValueError('restored SQLite integrity failure')
            records.append({'path': str(path.relative_to(root)), 'integrity_check': 'ok'})
        finally:
            db.close()
    if not records:
        raise ValueError('no SQLite databases found in restored volume')
    return {'sqlite_databases': records, 'application_started': False,
            'application_recovery_verified': False, 'meilisearch_verified': False}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--restored-root', required=True)
    args = parser.parse_args()
    print(json.dumps(check(args.restored_root), indent=2))
