#!/usr/bin/env python3
"""Tube Archivist online SQLite and Elasticsearch native PIT/bulk recovery bundle."""
import base64
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import sqlite3
import tarfile
import time
import urllib.request


def checked(response):
    if response.get('error') or response.get('timed_out') or response.get('errors'):
        raise ValueError('native Elasticsearch request failed')
    shards = response.get('_shards')
    if shards and (shards.get('failed') != 0 or shards.get('successful') != shards.get('total')):
        raise ValueError('incomplete Elasticsearch shards')
    return response


def export_es(request, pending):
    # One native PIT covers all application indices, including ta_config.
    catalog = request('GET', '/ta_*/_mapping')
    if not catalog or not all(name.startswith('ta_') for name in catalog):
        raise ValueError('empty or foreign index catalog')
    if not any(name.startswith('ta_config') for name in catalog):
        raise ValueError('missing application settings index')
    aliases = request('GET', '/ta_*/_alias')
    settings = request('GET', '/ta_*/_settings')
    pit = request('POST', '/ta_*/_pit?keep_alive=2m')['id']
    count = 0
    total = None
    seen = set()
    after = None
    try:
        with (pending / 'elasticsearch.ndjson').open('w') as stream:
            while True:
                usage = shutil.disk_usage(pending)
                if usage.free < max(usage.total // 10, 128 * 1024 * 1024):
                    raise ValueError('insufficient native bundle headroom')
                query = {'size': 500, 'track_total_hits': True,
                         'pit': {'id': pit, 'keep_alive': '2m'}, 'sort': ['_shard_doc']}
                if after is not None:
                    query['search_after'] = after
                response = checked(request('POST', '/_search', query))
                shards = response.get('_shards')
                if not isinstance(shards, dict) or any(type(shards.get(key)) is not int for key in ['failed', 'successful', 'total']):
                    raise ValueError('missing native shard proof')
                pit = response.get('pit_id', pit)
                hits = response['hits']
                if type(hits['total']['value']) is not int or hits['total']['value'] < 0:
                    raise ValueError('invalid Elasticsearch total')
                if hits['total']['relation'] != 'eq':
                    raise ValueError('inexact Elasticsearch total')
                if total is None:
                    total = hits['total']['value']
                if total != hits['total']['value']:
                    raise ValueError('PIT total changed')
                if not hits['hits']:
                    break
                for hit in hits['hits']:
                    identity = (hit['_index'], hit['_id'])
                    if identity in seen or hit['_index'] not in catalog or '_source' not in hit:
                        raise ValueError('duplicate, foreign or incomplete document')
                    seen.add(identity)
                    stream.write(json.dumps({'index': {'_index': hit['_index'], '_id': hit['_id']}}) + '\n')
                    stream.write(json.dumps(hit['_source']) + '\n')
                    count += 1
                next_after = hits['hits'][-1]['sort']
                if next_after == after:
                    raise ValueError('PIT did not advance')
                after = next_after
        if count != total:
            raise ValueError('incomplete native export')
        if catalog != request('GET', '/ta_*/_mapping'):
            raise ValueError('Elasticsearch mapping changed')
        if aliases != request('GET', '/ta_*/_alias') or settings != request('GET', '/ta_*/_settings'):
            raise ValueError('Elasticsearch aliases or settings changed')
        (pending / 'elasticsearch-mappings.json').write_text(json.dumps(catalog))
        (pending / 'elasticsearch-aliases.json').write_text(json.dumps(aliases))
        (pending / 'elasticsearch-settings.json').write_text(json.dumps(settings))
        return {'indices': sorted(catalog), 'documents': count, 'consistency': 'single-native-PIT'}
    finally:
        request('DELETE', '/_pit', {'id': pit})


def cache_identity(root):
    result = []
    for name in ['ytdlp', 'download', 'import']:
        directory = root / name
        if directory.is_symlink() or not directory.is_dir():
            raise ValueError('required cache state unavailable')
        for base, dirs, files in os.walk(directory, followlinks=False, onerror=lambda error: (_ for _ in ()).throw(error)):
            for leaf in [Path(base), *(Path(base) / p for p in sorted(dirs + files))]:
                if leaf.is_symlink() or not (leaf.is_file() or leaf.is_dir()):
                    raise ValueError('unsupported cache state')
                stat = leaf.stat()
                result.append((str(leaf.relative_to(root)), stat.st_ino, stat.st_size,
                               stat.st_mtime_ns, stat.st_ctime_ns, stat.st_mode))
    return sorted(result)


def capture(root=Path('/cache'), runtime=Path('/runtime/escrow.json')):
    os.umask(0o077)
    output = root / '.kopiur-native'
    if output.is_symlink():
        raise ValueError('unsafe output')
    output.mkdir(mode=0o700, exist_ok=True)
    if (output / 'lock').is_symlink():
        raise ValueError('unsafe lock')
    with (output / 'lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        (output / 'COMPLETE').unlink(missing_ok=True)
        pending = output / 'pending'
        if pending.exists():
            shutil.rmtree(pending)
        pending.mkdir(mode=0o700)
        database = root / 'db.sqlite3'
        def db_identity():
            result = []
            # SHM contains reader locks, not recovery data. Opening the online
            # reader may create empty WAL/SHM files even without a native writer.
            for suffix in ['', '-wal', '-journal']:
                p = Path(str(database) + suffix)
                if p.is_symlink():
                    raise ValueError('unsafe database')
                if p.exists():
                    s = p.stat()
                    if suffix and s.st_size == 0:
                        continue
                    result.append((suffix, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns))
            return result
        if not database.is_file() or not database.stat().st_size:
            raise ValueError('missing Django database')
        before = db_identity()
        files_before = cache_identity(root)
        required = sum(row[2] for row in files_before) + database.stat().st_size * 2
        usage = shutil.disk_usage(root)
        if usage.free - required < max(usage.total // 10, 128 * 1024 * 1024):
            raise ValueError('insufficient native bundle headroom')
        runtime_before = runtime.read_bytes()
        with contextlib.closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)) as source:
            with contextlib.closing(sqlite3.connect(pending / 'db.sqlite3')) as destination:
                source.backup(destination, pages=256, sleep=0.1)
                destination.execute('PRAGMA journal_mode=DELETE')
                if destination.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
                    raise ValueError('SQLite integrity failed')
        url = 'http://elasticsearch.database.svc.cluster.local:9200'
        auth = base64.b64encode((os.environ['ES_CAPTURE_USER'] + ':' + os.environ['ES_CAPTURE_PASSWORD']).encode()).decode()
        def request(method, path, body=None):
            req = urllib.request.Request(url + path, method=method,
                data=None if body is None else json.dumps(body).encode(),
                headers={'Authorization': 'Basic ' + auth, 'Content-Type': 'application/json'})
            # No redirect or proxy credential forwarding, even for a misconfigured Service.
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
            with opener.open(req, timeout=30) as response:
                raw = response.read(64 * 1024 * 1024 + 1)
            if len(raw) > 64 * 1024 * 1024:
                raise ValueError('oversized native response')
            return checked(json.loads(raw))
        metadata = export_es(request, pending)
        # Private runtime escrow is provisioned independently, never copied to logs.
        escrow = json.loads(runtime_before)
        if set(escrow) != {'TA_USERNAME', 'TA_PASSWORD'} or not all(isinstance(v, str) and v for v in escrow.values()):
            raise ValueError('missing effective authentication escrow')
        (pending / 'runtime.json').write_text(json.dumps(escrow))
        # Cookies and download state are required. Archive errors remain fatal.
        with tarfile.open(pending / 'cache-state.tar', 'w') as archive:
            for name in ['ytdlp', 'download', 'import']:
                path = root / name
                if path.is_symlink() or not path.is_dir():
                    raise ValueError('required cache state unavailable')
                archive.add(path, arcname=name)
        if before != db_identity() or files_before != cache_identity(root) or runtime_before != runtime.read_bytes():
            raise ValueError('application state changed during Elasticsearch capture')
        metadata.update({'format': 1, 'application': 'tubearchivist', 'completed_at': time.time()})
        (pending / 'metadata.json').write_text(json.dumps(metadata))
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


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


if __name__ == '__main__':
    def expired(*_):
        raise TimeoutError('native capture deadline')
    signal.signal(signal.SIGALRM, expired)
    signal.alarm(540)
    try:
        capture()
    except Exception:
        raise SystemExit('native Tube Archivist capture failed') from None
