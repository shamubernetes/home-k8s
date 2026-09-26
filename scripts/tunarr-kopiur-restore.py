#!/usr/bin/env python3
"""Validate a historical Tunarr recovery point and promote into a NEW directory.

Never mounts, modifies, or contacts the production app. Backend retrieval is a
separate operator step. Requires capture identity/window from controller evidence.
"""
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import sys

VERSIONS = {'': 'retained-2026.9', 'stable-1.3': 'stable-1.3'}


def regular(path, directory=False):
    mode = path.lstat().st_mode
    if not (stat.S_ISDIR(mode) if directory else stat.S_ISREG(mode)):
        raise ValueError('non-regular recovery path')


def tree(path):
    regular(path, directory=True)
    result = []
    for child in sorted(path.iterdir()):
        if child.is_symlink():
            raise ValueError('symlink in recovery tree')
        if child.is_dir():
            result.extend(tree(child))
        else:
            regular(child)
            result.append(child)
    return result


def load(path):
    regular(path)
    return json.loads(path.read_text())


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def verify(source, generation, start, end):
    if not re.fullmatch('[a-f0-9]{32}', generation):
        raise ValueError('invalid generation')
    if not all(math.isfinite(n) for n in (start, end)) or not 0 <= start <= end:
        raise ValueError('invalid capture window')
    # Reject links in any ancestor, not only leaf files.
    for ancestor in [source, *source.parents]:
        regular(ancestor, directory=True)
    directory = source / '.kopiur-consistent'
    regular(directory, directory=True)
    if (directory / 'lock').exists():
        raise ValueError('capture lock present')
    manifest = load(directory / 'manifest.json')
    attempt = load(directory / 'attempt.json')
    if manifest.get('version') != 3 or manifest.get('generation') != generation:
        raise ValueError('wrong manifest')
    if attempt != {'generation': generation, 'state': 'complete'}:
        raise ValueError('incomplete capture')
    completed = manifest['completed_at']
    if not isinstance(completed, (int, float)) or not start <= completed <= end:
        raise ValueError('capture outside controller window')
    records = manifest['records']
    if len(records) != 2 or {r['source']: r['artifact'] for r in records} != VERSIONS:
        raise ValueError('both exact persistent versions required')
    capture = directory / ('capture-' + generation)
    regular(capture, directory=True)
    for record in records:
        base = capture / record['artifact']
        actual = {str(p.relative_to(base)): digest(p) for p in tree(base)}
        if actual != record['files']:
            raise ValueError('artifact file set or hash mismatch')
        if not {'db.db', 'settings.json'} <= actual.keys():
            raise ValueError('missing required files')
        if any(n not in ('db.db', 'settings.json') and not n.startswith(('channel-lineups/', 'images/')) for n in actual):
            raise ValueError('unexpected capture path')
        for name in ('channel-lineups', 'images'):
            regular(base / name, directory=True)
        settings = load(base / 'settings.json')
        if not settings.get('settings') or not settings.get('system'):
            raise ValueError('invalid settings')
        db = sqlite3.connect((base / 'db.db').as_uri() + '?mode=ro&immutable=1', uri=True)
        try:
            if db.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
                raise ValueError('SQLite integrity')
            if db.execute('PRAGMA foreign_key_check').fetchall():
                raise ValueError('SQLite foreign keys')
            names = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
            counts = {n: db.execute('SELECT COUNT(*) FROM "' + n.replace('"', '""') + '"').fetchone()[0] for n in names}
            if counts != record['counts']:
                raise ValueError('count mismatch')
            channels = dict(db.execute('SELECT uuid, duration FROM channel'))
            programs = {r[0] for r in db.execute('SELECT uuid FROM program')}
            lineup_paths = list((base / 'channel-lineups').iterdir())
            if {p.name for p in lineup_paths} != {c + '.json' for c in channels}:
                raise ValueError('lineup/channel mismatch')
            for p in lineup_paths:
                items = load(p)['items']
                if not isinstance(items, list):
                    raise ValueError('invalid lineup')
                content = set()
                duration = 0.0
                for item in items:
                    if not isinstance(item, dict) or item.get('type') not in ('content', 'offline', 'redirect'):
                        raise ValueError('invalid lineup item')
                    value = item.get('durationMs')
                    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                        raise ValueError('invalid lineup duration')
                    # Match upstream's ordered JS sum, including repeats, offline
                    # and redirects. Do not sum distinct SQL program durations.
                    duration += value
                    if item['type'] == 'content':
                        if item.get('id') not in programs:
                            raise ValueError('invalid lineup program')
                        content.add(item['id'])
                membership = {r[0] for r in db.execute(
                    'SELECT DISTINCT program_uuid FROM channel_programs WHERE channel_uuid = ?', (p.stem,))}
                if content != membership:
                    raise ValueError('lineup membership mismatch')
                if not math.isfinite(duration) or duration != channels[p.stem]:
                    raise ValueError('lineup duration mismatch')
        finally:
            db.close()
    return manifest, capture


def restore(source, destination, generation, start, end, backend):
    if backend not in ('nas', 'r2'):
        raise ValueError('backend identity required')
    # Do not resolve away source symlinks before verification.
    source = Path(os.path.abspath(source))
    destination = Path(os.path.abspath(destination))
    if destination.exists() or destination.is_symlink():
        raise ValueError('destination must not exist')
    for ancestor in [destination.parent, *destination.parent.parents]:
        regular(ancestor, directory=True)
    if source in destination.parents or destination in source.parents:
        raise ValueError('overlapping source and destination')
    manifest, capture = verify(source, generation, start, end)
    # Entire PVC preserved, including raw DBs, retained newer state and indexes.
    tree(source)
    os.umask(0o077)
    shutil.copytree(source, destination)
    archive = destination / '.kopiur-raw'
    archive.mkdir(mode=0o700)  # Collision is a hard failure, never overwrite.
    for record in manifest['records']:
        root = destination / record['source']
        original = archive / record['artifact']
        original.mkdir(mode=0o700)
        # Search state is derived. Archive both the index and its auto-importable
        # snapshots so a normal boot rebuilds from verified SQL, not raw bytes.
        for name in ('db.db', 'db.db-wal', 'db.db-shm', 'db.db-journal', 'settings.json',
                     'channel-lineups', 'images', 'data.ms', 'ms-snapshots', 'meilisearch.pid'):
            p = root / name
            if p.exists():
                p.rename(original / name)
        for p in (capture / record['artifact']).iterdir():
            target = root / p.name
            if p.is_dir():
                shutil.copytree(p, target)
            else:
                shutil.copyfile(p, target)
    verify(destination, generation, start, end)
    for record in manifest['records']:
        for name, expected in record['files'].items():
            if digest(destination / record['source'] / name) != expected:
                raise ValueError('promoted bytes mismatch')
    # A successful receipt is emitted only after every promoted file is durable.
    for p in tree(destination):
        os.chmod(p, 0o600)
        with p.open('rb') as stream:
            os.fsync(stream.fileno())
    receipt = {**manifest, 'backend': backend, 'capture_window': [start, end]}
    marker = destination / '.tunarr-restore.json'
    with marker.open('x') as stream:
        json.dump(receipt, stream)
        stream.flush()
        os.fsync(stream.fileno())
    for directory, _, _ in os.walk(destination, topdown=False):
        os.chmod(directory, 0o700)
        fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--destination', type=Path, required=True)
    parser.add_argument('--generation', required=True)
    parser.add_argument('--capture-start', type=float, required=True)
    parser.add_argument('--capture-end', type=float, required=True)
    parser.add_argument('--backend', choices=['nas', 'r2'], required=True)
    args = parser.parse_args()
    try:
        receipt = restore(args.source, args.destination, args.generation, args.capture_start, args.capture_end, args.backend)
        print(json.dumps({'backend': args.backend, 'generation': receipt['generation'],
                          'counts': {r['artifact']: r['counts'] for r in receipt['records']}}))
    except Exception:
        # Paths and database errors can contain credentials or private titles.
        print('Tunarr restore rejected; source untouched; discard any partial destination', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
