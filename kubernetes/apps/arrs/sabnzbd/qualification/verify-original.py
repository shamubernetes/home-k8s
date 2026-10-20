#!/usr/bin/env python3
"""Read-only SABnzbd original-pair validation. Does not boot apps or authorize cutover."""
import argparse
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import stat


def digest(path):
    with path.open('rb') as handle:
        return hashlib.file_digest(handle, 'sha256').hexdigest()


def inspect(root):
    """Fail closed on unsafe files, corrupt recovery DB or incomplete config."""
    root = Path(root)
    if root.is_symlink() or not root.is_dir():
        raise ValueError('invalid restore root')
    inventory = []
    for path in sorted(root.rglob('*')):
        mode = path.lstat().st_mode
        if stat.S_ISDIR(mode):
            inventory.append([str(path.relative_to(root)), 'directory'])
        elif stat.S_ISREG(mode):
            if path.stat().st_nlink != 1:
                raise ValueError('hardlinked restore member')
            inventory.append([str(path.relative_to(root)), digest(path)])
        else:
            raise ValueError('unsafe restore member')
    manifest = json.loads((root / '.kopiur-consistent/manifest.json').read_text())
    if (manifest.get('version') not in (1, 2) or manifest.get('database') != 'admin/history1.db'
            or manifest.get('integrity_check') != 'ok'):
        raise ValueError('unexpected recovery contract')
    db = root / '.kopiur-consistent/database.sqlite3'
    if any(Path(str(db) + suffix).exists() for suffix in ['-wal', '-shm', '-journal']):
        raise ValueError('recovery copy is not standalone')
    checksum = digest(db)
    if checksum != manifest.get('sha256'):
        raise ValueError('recovery checksum mismatch')
    config = root / 'sabnzbd.ini'
    if not config.is_file() or config.stat().st_size == 0:
        raise ValueError('missing application configuration')
    if manifest['version'] == 2:
        if digest(config) != manifest.get('configSha256'):
            raise ValueError('runtime generation mismatch')
        keys = manifest.get('runtimeKeySha256')
        if not isinstance(keys, dict) or set(keys) != {'api_key', 'nzb_key'}:
            raise ValueError('missing runtime key binding')
        hashes = [manifest.get(field) for field in ('sourceDatabasePathSha256', 'effectiveRuntimeSha256')]
        for value in [*hashes, *keys.values()]:
            if not isinstance(value, str) or len(value) != 64 or any(c not in '0123456789abcdef' for c in value):
                raise ValueError('missing effective source provenance')
    # immutable is safe only for the hook's standalone, completed online copy.
    # Never use it against the source DB or its live WAL.
    uri = db.resolve().as_uri() + '?mode=ro&immutable=1'
    with closing(sqlite3.connect(uri, uri=True)) as connection:
        if connection.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
            raise ValueError('recovery integrity failure')
        tables = [row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        if not tables:
            raise ValueError('empty application database')
        rows = sum(connection.execute('SELECT count(*) FROM "' +
                   table.replace('"', '""') + '"').fetchone()[0] for table in tables)
    return {'databaseSha256': checksum, 'configSha256': digest(config),
            'treeSha256': hashlib.sha256(json.dumps(inventory, separators=(',', ':')).encode()).hexdigest(),
            'members': len(inventory), 'tables': len(tables), 'rows': rows,
            'integrity': 'ok'}


def verify(nas, r2):
    if Path(nas).resolve() == Path(r2).resolve():
        raise ValueError('independent roots required')
    left, right = inspect(nas), inspect(r2)
    if left != right:
        raise ValueError('independent original bytes differ')
    return {'nativeFilesystemEquality': True, 'evidence': left,
            'applicationBootProved': False, 'runtimeKeyEscrowProved': False,
            'processContinuityProved': False, 'retirementAllowed': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('nas_root')
    parser.add_argument('r2_root')
    args = parser.parse_args()
    try:
        print(json.dumps(verify(args.nas_root, args.r2_root), sort_keys=True))
    except (OSError, ValueError, sqlite3.Error):
        # No record values, filenames, configuration contents or exception text.
        raise SystemExit('SABnzbd original qualification rejected') from None


if __name__ == '__main__':
    main()
