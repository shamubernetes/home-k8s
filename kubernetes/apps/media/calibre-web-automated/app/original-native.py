#!/usr/bin/env python3
"""Replay the actual five-database CWA generation in its original image."""
import contextlib
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat

DATABASES = ('app.db', 'cwa.db', 'gdrive.db', 'calibre-library/metadata.db',
             'calibre-library/.calnotes/notes.db')


def require(value):
    if not value:
        raise ValueError('CWA original replay contract failed')


def digest(path):
    result = hashlib.sha256()
    with path.open('rb') as handle:
        for data in iter(lambda: handle.read(1024 * 1024), b''):
            result.update(data)
    return result.hexdigest()


def tree(root):
    result = {}
    def fail(error):
        raise error
    for base, dirs, files in os.walk(root, followlinks=False, onerror=fail):
        for name in sorted(dirs + files):
            file = Path(base) / name
            info = file.lstat()
            require(stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode))
            result[str(file.relative_to(root))] = [info.st_mode, info.st_uid, info.st_gid,
                info.st_mtime_ns if file.is_file() else None, digest(file) if file.is_file() else None]
    return result


def database(path):
    with contextlib.closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as db:
        require(db.execute('PRAGMA integrity_check').fetchall() == [('ok',)])
        tables = [row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        require(tables)
        rows = {t: db.execute('SELECT count(*) FROM "' + t.replace('"', '""') + '"').fetchone()[0]
                for t in tables}
        logical = hashlib.sha256()
        for line in db.iterdump():
            logical.update(line.encode() + b'\n')
        return {'tables': len(tables), 'rows': sum(rows.values()), 'logical_sha256': logical.hexdigest(),
                'integrity_check': 'ok'}


def replay(source, target):
    candidates = [source, source / 'config', source / 'data']
    print(json.dumps({'backend': source.name, 'known_layout': [
        {'root': str(p.relative_to(source)), 'database_presence': {
            n: (p / n).is_file() for n in DATABASES},
         'manifest': (p / '.kopiur-cwa/current.json').is_file(),
         'native_directories': [c.name for c in p.iterdir() if 'kopiur' in c.name]
         if p.is_dir() else []} for p in candidates]}), flush=True)
    native_roots = [p for p in candidates if (p / '.kopiur-cwa/current.json').is_file()]
    require(len(native_roots) == 1)
    source = native_roots[0]
    manifest = json.loads((source / '.kopiur-cwa/current.json').read_text())
    require(manifest['version'] == 1 and manifest['consistency'] == 'per-database-online')
    require(re.fullmatch('[0-9a-f]{32}', manifest['generation']))
    records = manifest['databases']
    require(len(records) == len(DATABASES) and {r['path'] for r in records} == set(DATABASES))
    generation = source / '.kopiur-cwa' / manifest['generation']
    require(not target.exists())
    shutil.copytree(source, target)
    results = {}
    for record in records:
        relative = record['path']
        original = generation / relative
        require(original.is_file() and not original.is_symlink())
        require(original.stat().st_size == record['bytes'] and digest(original) == record['sha256'])
        restored = target / relative
        for suffix in ('-wal', '-shm', '-journal'):
            Path(str(restored) + suffix).unlink(missing_ok=True)
        shutil.copyfile(original, restored)
        results[relative] = database(restored)
        require(digest(restored) == record['sha256'])
    # app.db is CWA's actual persistent application settings and user store.
    with contextlib.closing(sqlite3.connect(str(target / 'app.db'))) as db:
        names = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        require({'user', 'settings'}.issubset(names))
    return results


def main():
    require(os.environ.get('RECOVERY_TARGET') == 'isolated')
    originals = {b: Path('/original') / b for b in ('nas', 'r2')}
    before = {b: tree(p) for b, p in originals.items()}
    require(before['nas'] and before['nas'] == before['r2'])
    results = {b: replay(p, Path('/work') / b) for b, p in originals.items()}
    require(results['nas'] == results['r2'])
    require(all(tree(p) == before[b] for b, p in originals.items()))
    receipt = {'app': 'calibre-web-automated', 'points': json.loads(os.environ['ORIGINAL_POINTS']),
               'independent_original_state_restored': True, 'all_five_native_databases_equal': True,
               'full_original_tree_equal': True, 'originals_unchanged': True,
               'original_tree_sha256': hashlib.sha256(json.dumps(before['nas'], sort_keys=True).encode()).hexdigest(),
               'results': results}
    print(json.dumps(receipt, sort_keys=True), flush=True)


if __name__ == '__main__':
    os.umask(0o077)
    try:
        main()
    except Exception as error:
        import traceback
        lines = [f.lineno for f in traceback.extract_tb(error.__traceback__) if f.filename == __file__]
        print(json.dumps({'failed': True, 'class': type(error).__name__, 'source_lines': lines}), flush=True)
        raise SystemExit(1) from None
