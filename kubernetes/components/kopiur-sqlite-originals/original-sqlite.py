#!/usr/bin/env python3
"""Reuse the existing captured-SQLite replay for four concrete source contracts."""
import configparser
import contextlib
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat

CONTRACTS = {
    'seerr': ('db/db.sqlite3', 'kopiur/seerr-online.db', ['settings.json']),
    'tautulli': ('tautulli.db', 'kopiur/tautulli-online.db', ['config.ini']),
    'audiobookshelf': ('absdatabase.sqlite', None, ['metadata']),
    'kometa': ('config.cache', None, ['config.yml', 'UUID']),
}


def require(value):
    if not value:
        raise ValueError('original captured SQLite acceptance failed')


def inventory(root):
    result = {}
    for base, dirs, files in os.walk(root, followlinks=False,
                                     onerror=lambda error: (_ for _ in ()).throw(error)):
        for name in sorted(dirs + files):
            file = Path(base) / name
            info = file.lstat()
            require(stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode))
            digest = None
            if file.is_file():
                with file.open('rb') as stream:
                    digest = hashlib.file_digest(stream, 'sha256').hexdigest()
            result[str(file.relative_to(root))] = [info.st_mode, info.st_uid, info.st_gid,
                info.st_mtime_ns if file.is_file() else None, digest]
    return result


def database(file):
    with contextlib.closing(sqlite3.connect(file.as_uri() + '?mode=ro', uri=True)) as db:
        require(db.execute('PRAGMA integrity_check').fetchall() == [('ok',)])
        names = [row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        require(names)
        rows = sum(db.execute('SELECT count(*) FROM "' + name.replace('"', '""') + '"').fetchone()[0] for name in names)
        digest = hashlib.sha256()
        for line in db.iterdump():
            digest.update(line.encode() + b'\n')
        return {'tables': len(names), 'rows': rows, 'logical_sha256': digest.hexdigest(), 'integrity_check': 'ok'}


def artifact(original, db_name, fixed):
    if fixed is not None:
        file = original / fixed
    else:
        directory = original / '.kopiur-consistent'
        manifest = json.loads((directory / 'manifest.json').read_text())
        require(manifest['database'] == db_name)
        if manifest.get('version') == 2:
            require(re.fullmatch('[0-9a-f]{32}', manifest['generation']))
            require(manifest['artifact'] == 'database-' + manifest['generation'] + '.sqlite3')
            attempt = json.loads((directory / 'attempt.json').read_text())
            require(attempt == {'generation': manifest['generation'], 'state': 'complete'})
            file = directory / manifest['artifact']
            require(file.stat().st_size == manifest['size_bytes'] and manifest['integrity_check'] == 'ok')
        else:
            require(manifest.get('version', 1) == 1)
            file = directory / 'database.sqlite3'
        with file.open('rb') as stream:
            require(hashlib.file_digest(stream, 'sha256').hexdigest() == manifest['sha256'])
    require(file.is_file() and not file.is_symlink() and file.stat().st_size > 0)
    return file


def restore(original, target, app):
    db_name, fixed, required = CONTRACTS[app]
    for name in required:
        file = original / name
        require(not file.is_symlink() and file.exists())
        require(file.is_dir() or file.stat().st_size > 0)
    copied = artifact(original, db_name, fixed)
    require(not target.exists())
    shutil.copytree(original, target)
    for base, dirs, files in os.walk(original):
        for name in dirs + files:
            file = Path(base) / name
            info = file.stat()
            os.chown(target / file.relative_to(original), info.st_uid, info.st_gid)
    info = original.stat()
    os.chown(target, info.st_uid, info.st_gid)
    restored = target / db_name
    for suffix in ('-wal', '-shm', '-journal'):
        Path(str(restored) + suffix).unlink(missing_ok=True)
    shutil.copyfile(copied, restored)
    # Open only the writable isolated replay, never a read-only original that
    # may need SQLite WAL/SHM bookkeeping despite a complete native export.
    result = database(restored)
    if app == 'seerr':
        require(isinstance(json.loads((target / 'settings.json').read_text()), dict))
    elif app == 'tautulli':
        config = configparser.RawConfigParser(strict=False)
        with (target / 'config.ini').open() as stream:
            config.read_file(stream)
        require(config.sections())
    elif app == 'kometa':
        from ruamel.yaml import YAML
        require(isinstance(YAML(typ='safe').load((target / 'config.yml').read_text()), dict))
        require((target / 'UUID').read_bytes())
    elif app == 'audiobookshelf':
        require((target / 'metadata').is_dir())
    for name in required:
        if (original / name).is_file():
            require((target / name).read_bytes() == (original / name).read_bytes())
    result.update(captured_database_replayed=True, original_required_state_and_keys_equal=True)
    return result


def main():
    require(os.environ.get('RECOVERY_TARGET') == 'isolated')
    app = os.environ['TARGET_APP']
    require(app in CONTRACTS)
    points = json.loads(os.environ['ORIGINAL_POINTS'])
    sources = {b: Path('/original') / b for b in ('nas', 'r2')}
    trees = {b: inventory(p) for b, p in sources.items()}
    require(trees['nas'] and trees['nas'] == trees['r2'])
    results = {}
    for backend, source in sources.items():
        result = restore(source, Path('/work') / backend, app)
        require(inventory(source) == trees[backend])
        result.update(snapshot_id=points[backend], original_unchanged=True)
        results[backend] = result
    require(results['nas']['logical_sha256'] == results['r2']['logical_sha256'])
    receipt = {'app': app, 'independent_original_state_restored': True,
               'native_sqlite_original_data_equal': True, 'full_original_tree_equal': True, 'results': results}
    Path('/work/original-receipt.json').write_text(json.dumps(receipt, sort_keys=True))
    print(json.dumps(receipt, sort_keys=True), flush=True)


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        import traceback
        lines = [f.lineno for f in traceback.extract_tb(error.__traceback__) if f.filename == __file__]
        print(json.dumps({'failed': True, 'class': type(error).__name__, 'source_lines': lines}), flush=True)
        raise SystemExit(1) from None
