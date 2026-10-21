#!/usr/bin/env python3
"""Recover the existing cwa-bdl/listenarr captured-SQLite contract together."""
import contextlib
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import stat


CONTRACTS = {
    'cwa-bdl': ('users.db', ['settings.json', '.flask_secret']),
    'listenarr': ('database/listenarr.db', ['config.json']),
}


def require(value):
    if not value:
        raise ValueError('captured SQLite original acceptance failed')


def inventory(root):
    result = {}
    for base, dirs, files in os.walk(root, followlinks=False,
                                     onerror=lambda error: (_ for _ in ()).throw(error)):
        for name in sorted(dirs + files):
            path = Path(base) / name
            info = path.lstat()
            require(stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode))
            digest = None
            if path.is_file():
                with path.open('rb') as stream:
                    digest = hashlib.file_digest(stream, 'sha256').hexdigest()
            result[str(path.relative_to(root))] = [info.st_mode, info.st_uid, info.st_gid,
                                                 info.st_mtime_ns if path.is_file() else None,
                                                 info.st_size if path.is_file() else None, digest]
    return result


def database(path):
    with contextlib.closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as db:
        require(db.execute('PRAGMA integrity_check').fetchall() == [('ok',)])
        tables = [row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        require(bool(tables))
        rows = sum(db.execute('SELECT count(*) FROM "' + name.replace('"', '""') + '"').fetchone()[0]
                   for name in tables)
        digest = hashlib.sha256()
        for line in db.iterdump():
            digest.update(line.encode() + b'\n')
        return {'tables': len(tables), 'rows': rows, 'logical_sha256': digest.hexdigest()}


def restore(original, destination, app):
    database_name, required_files = CONTRACTS[app]
    manifest = json.loads((original / '.kopiur-consistent/manifest.json').read_text())
    require(manifest['database'] == database_name)
    captured = original / '.kopiur-consistent/database.sqlite3'
    with captured.open('rb') as stream:
        require(hashlib.file_digest(stream, 'sha256').hexdigest() == manifest['sha256'])
    expected = database(captured)
    for name in required_files:
        path = original / name
        require(path.is_file() and not path.is_symlink() and path.stat().st_size > 0)
        if path.suffix == '.json':
            json.loads(path.read_text())
    require(not destination.exists())
    shutil.copytree(original, destination)
    # A root restore replays actual recorded ownership on the isolated copy.
    for base, dirs, files in os.walk(original):
        for name in dirs + files:
            source = Path(base) / name
            info = source.stat()
            os.chown(destination / source.relative_to(original), info.st_uid, info.st_gid)
    root_info = original.stat()
    os.chown(destination, root_info.st_uid, root_info.st_gid)
    restored = destination / database_name
    for suffix in ('-wal', '-shm', '-journal'):
        Path(str(restored) + suffix).unlink(missing_ok=True)
    shutil.copyfile(captured, restored)
    require(database(restored) == expected)
    for name in required_files:
        require((destination / name).read_bytes() == (original / name).read_bytes())
    return dict(expected, captured_database_replayed=True, integrity_check='ok',
                original_required_config_and_keys_equal=True)


def main():
    require(os.environ.get('RECOVERY_TARGET') == 'isolated')
    points = json.loads(os.environ['ORIGINAL_POINTS'])
    receipts = {}
    for app in CONTRACTS:
        roots = {backend: Path('/original') / app / backend for backend in ('nas', 'r2')}
        inventories = {backend: inventory(root) for backend, root in roots.items()}
        require(inventories['nas'] and inventories['nas'] == inventories['r2'])
        results = {}
        for backend, original in roots.items():
            destination = Path('/work') / (app + '-' + backend)
            result = restore(original, destination, app)
            require(inventory(original) == inventories[backend])
            result.update(snapshot_id=points[app][backend], original_unchanged=True)
            results[backend] = result
        require(results['nas']['logical_sha256'] == results['r2']['logical_sha256'])
        receipts[app] = {'independent_original_state_restored': True,
                         'native_sqlite_original_data_equal': True,
                         'full_original_tree_equal': True,
                         'original_files': sum(row[-1] is not None for row in inventories['nas'].values()),
                         'results': results}
    receipt = {'contract': 'existing-kopiur-consistent-sqlite', 'applications': receipts}
    Path('/work/original-receipt.json').write_text(json.dumps(receipt, sort_keys=True))
    print(json.dumps(receipt, sort_keys=True), flush=True)


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        import traceback
        frames = [frame.lineno for frame in traceback.extract_tb(error.__traceback__)
                  if frame.filename == __file__]
        print(json.dumps({'failed': True, 'class': type(error).__name__,
                          'source_line': frames[-1] if frames else 0}), flush=True)
        raise SystemExit(1) from None
