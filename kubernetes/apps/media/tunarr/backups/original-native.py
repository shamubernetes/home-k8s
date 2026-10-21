#!/usr/bin/env python3
"""Consume real frozen Tunarr originals without touching the retained deployment."""
import contextlib
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import time
import urllib.request


def require(value):
    if not value:
        raise ValueError('actual frozen original recovery failed')


def original_tree(root):
    result = {}
    for base, dirs, files in os.walk(root, followlinks=False,
                                     onerror=lambda error: (_ for _ in ()).throw(error)):
        for name in sorted(dirs + files):
            file = Path(base) / name
            require(not file.is_symlink() and (file.is_file() or file.is_dir()))
            info = file.stat()
            if file.is_file():
                with file.open('rb') as stream:
                    digest = hashlib.file_digest(stream, 'sha256').hexdigest()
            else:
                digest = None
            result[str(file.relative_to(root))] = [info.st_mode, info.st_uid, info.st_gid,
                                                  info.st_mtime_ns if file.is_file() else None, digest]
    return result


def _inspect_active_database(file, target, databases):
    with contextlib.closing(sqlite3.connect(file.as_uri() + '?mode=ro', uri=True)) as db:
        require(db.execute('PRAGMA integrity_check').fetchall() == [('ok',)])
        tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        rows = sum(db.execute('SELECT count(*) FROM "'+t.replace('"','""')+'"').fetchone()[0] for t in tables)
        digest = hashlib.sha256()
        for line in db.iterdump():
            digest.update(line.encode() + b'\n')
        entry = {'tables': len(tables), 'rows': rows, 'logical_sha256': digest.hexdigest()}
        for table in ('channel', 'channels'):
            if table in tables:
                columns = [r[1] for r in db.execute('PRAGMA table_info("'+table+'")')]
                require('uuid' in columns)
                entry['channel_ids'] = sorted(r[0] for r in db.execute('SELECT uuid FROM "'+table+'"'))
        databases[str(file.relative_to(target))] = entry


def prepare():
    original, target = Path('/original'), Path('/work/data')
    os.chown('/work', 568, 568)
    os.chmod('/work', 0o700)
    os.chmod('/tmp', 0o1777)
    before = original_tree(original)
    require(before and not target.exists())
    shutil.copytree(original, target)
    native_root = target / 'stable-1.3'
    require(native_root.is_dir())
    databases = {}
    channel_ids = None
    for base, directories, files in os.walk(native_root):
        directories[:] = [name for name in directories if name not in {'backups', 'snapshots', 'dumps', 'streams'}]
        for name in files:
            file = Path(base) / name
            if not file.is_file():
                continue
            with file.open('rb') as stream:
                if stream.read(16) != b'SQLite format 3\x00':
                    continue
            # Active databases only; historical dumps remain fully restored but
            # are not incorrectly asserted to equal today's channel catalog.
            _inspect_active_database(file, target, databases)
    # Tunarr 1.3.15 getDefaultDatabaseName() opens db.db in this directory.
    # Historical SQLite copies are restored and checked, not active catalogs.
    active_database = str((native_root / 'db.db').relative_to(target))
    require(active_database in databases)
    channel_ids = databases[active_database].get('channel_ids')
    for entry in databases.values():
        entry.pop('channel_ids', None)
    require(databases and channel_ids is not None)
    require(original_tree(original) == before)
    for base, dirs, files in os.walk(target):
        for name in dirs + files:
            os.chown(Path(base)/name, 568, 568)
    os.chown(target, 568, 568)
    expected = {'databases': databases, 'channel_ids': channel_ids,
                'original_tree_sha256': hashlib.sha256(json.dumps(before, sort_keys=True).encode()).hexdigest()}
    Path('/work/original-data.json').write_text(json.dumps(expected, sort_keys=True))
    os.chown('/work/original-data.json', 568, 568)


def verify():
    expected = json.loads(Path('/work/original-data.json').read_text())
    deadline = time.monotonic() + 600
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen('http://127.0.0.1:8000/api/version', timeout=5) as response:
                require(response.status == 200)
                version = json.load(response)
            with urllib.request.urlopen('http://127.0.0.1:8000/api/channels', timeout=5) as response:
                require(response.status == 200)
                channels = json.load(response)
            require(isinstance(channels, list))
            # Native API exposes SQL channel.uuid as the response's id field.
            ids = sorted(c['id'] for c in channels)
            require(ids == expected['channel_ids'])
            receipt = {'app': 'tunarr-frozen-rollback', 'backend': os.environ['BACKEND'],
                       'snapshot_id': os.environ['ORIGINAL_POINT'], 'native_http_status': 200,
                       'native_version': version, 'native_channel_catalog_equal': True,
                       'channel_count': len(ids), 'all_original_sqlite': expected['databases'],
                       'original_tree_sha256': expected['original_tree_sha256'],
                       'retained_deployment_untouched': True}
            Path('/work/native-receipt.json').write_text(json.dumps(receipt, sort_keys=True))
            print(json.dumps(receipt, sort_keys=True), flush=True)
            Path('/work/native-complete').touch()
            return
        except Exception:
            time.sleep(2)
    raise TimeoutError('isolated actual native image failed to consume original catalog')


try:
    require(os.environ.get('RECOVERY_TARGET') == 'isolated')
    prepare() if sys.argv[1] == 'prepare' else verify()
except Exception as error:
    import traceback
    lines = [f.lineno for f in traceback.extract_tb(error.__traceback__) if f.filename == __file__]
    print(json.dumps({'failed': True, 'class': type(error).__name__, 'source_lines': lines}), flush=True)
    raise SystemExit(1) from None
