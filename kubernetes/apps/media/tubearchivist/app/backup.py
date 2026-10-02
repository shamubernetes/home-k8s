#!/usr/bin/env python3
"""TubeArchivist 0.5.12 native JSON + SQLite recovery contract.

capture runs inside the existing app. restore-es runs only against a new ES
cluster named k8s92-ta-restore. Neither operation copies global/security state.
"""
import argparse
import base64
import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import sqlite3
import sys
import tempfile
import urllib.error
import urllib.request
import zipfile

INDICES = tuple('ta_' + name for name in (
    'channel', 'video', 'download', 'playlist', 'subtitle', 'comment', 'config'))


def check(condition, message):
    if not condition:
        raise RuntimeError(message)


def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def request(path, method='GET', data=None, ndjson=False):
    url = os.environ['ES_URL'].rstrip('/') + '/' + path
    password = os.environ.get('ELASTIC_PASSWORD')
    if password is None and os.environ.get('ELASTIC_PASSWORD_FILE'):
        password = Path(os.environ['ELASTIC_PASSWORD_FILE']).read_text().strip()
    auth = base64.b64encode((os.environ.get('ELASTIC_USER', 'elastic') + ':' + (password or '')).encode()).decode()
    headers = {'Authorization': 'Basic ' + auth, 'Content-Type': 'application/x-ndjson' if ndjson else 'application/json'}
    payload = data.encode() if ndjson else (json.dumps(data).encode() if data is not None else None)
    try:
        with urllib.request.urlopen(urllib.request.Request(url, data=payload, headers=headers, method=method), timeout=30) as response:
            result = json.load(response)
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f'Elasticsearch HTTP {exc.code}') from None
    if isinstance(result, dict):
        check(not result.get('timed_out') and not result.get('_shards', {}).get('failed', 0), 'incomplete Elasticsearch response')
    return result


def zip_records(path):
    with zipfile.ZipFile(path) as archive:
        check(archive.testzip() is None, 'ZIP CRC failed')
        check(len(archive.namelist()) == len(set(archive.namelist())), 'duplicate ZIP member')
        for member in archive.namelist():
            check('/' not in member and member.startswith('es_') and member.endswith('.json'), 'unexpected ZIP member')
            with archive.open(member) as stream:
                while True:
                    action = stream.readline()
                    if not action:
                        break
                    if not action.strip():
                        continue
                    action = json.loads(action)
                    check(set(action) == {'index'}, 'unexpected bulk action')
                    meta = action['index']
                    check(set(meta) == {'_index', '_id'} and meta['_index'] in INDICES, 'unexpected index or bulk metadata')
                    source = json.loads(stream.readline())
                    yield meta, source


def inventory(path):
    result = {index: {} for index in INDICES}
    for meta, source in zip_records(path):
        bucket = result[meta['_index']]
        check(meta['_id'] not in bucket, 'duplicate document ID')
        bucket[meta['_id']] = source
    return {index: {'count': len(docs), 'sha256': hashlib.sha256(json.dumps(docs, sort_keys=True, separators=(',', ':')).encode()).hexdigest()} for index, docs in result.items()}


def sqlite_inventory(path):
    with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True) as db:
        check(db.execute('PRAGMA integrity_check').fetchall() == [('ok',)], 'SQLite integrity failed')
        tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        # v0.5.12 config/settings.py swaps auth.User for user.Account.
        check({'user_account', 'django_migrations', 'task_customperiodictask'} <= set(tables), 'missing Django state tables')
        return {name: db.execute('SELECT count(*) FROM "' + name.replace('"', '""') + '"').fetchone()[0] for name in tables}


def capture(output):
    # These imports deliberately use the app's shipped exporter, not a lookalike.
    sys.path.insert(0, '/app')
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
    import django
    django.setup()
    from appsettings.src.backup import ElasticBackup
    from common.src.env_settings import EnvironmentSettings
    import requests
    original = requests.sessions.Session.request

    def strict(self, method, url, **kwargs):
        kwargs['timeout'] = min(kwargs.get('timeout') or 30, 30)
        response = original(self, method, url, **kwargs)
        check(response.ok, f'native exporter HTTP {response.status_code}')
        body = response.json()
        check(not body.get('timed_out') and not body.get('_shards', {}).get('failed', 0), 'native export partial search')
        return response

    requests.sessions.Session.request = strict
    output.parent.mkdir(parents=True, exist_ok=True)
    with (output.parent / '.capture.lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if output.exists():
            shutil.rmtree(output)
        stage = Path(tempfile.mkdtemp(prefix='.capture-', dir=output.parent))
        try:
            started = datetime.datetime.now(datetime.timezone.utc).isoformat()
            root = request('')
            check(root['version']['number'] == '8.19.22', 'ES version changed; requalify restore')
            settings = request('ta_*?expand_wildcards=all')
            check(set(settings) == set(INDICES), 'application index set changed; requalify coverage')
            counts = {}
            for index in INDICES:
                counts[index] = request(index + '/_count')['count']
            source_path = Path(os.environ.get('TA_CACHE_DIR', '/cache')) / 'db.sqlite3'
            with sqlite3.connect(source_path.as_uri() + '?mode=ro', uri=True) as source:
                with sqlite3.connect(stage / 'db.sqlite3') as destination:
                    source.backup(destination, pages=256, sleep=0.1)
            sqlite_counts = sqlite_inventory(stage / 'db.sqlite3')
            # Redirect both the native callback and zipper to a clean directory.
            (stage / 'backup').mkdir()
            EnvironmentSettings.CACHE_DIR = str(stage)
            ElasticBackup.CACHE_DIR = str(stage)
            ElasticBackup.BACKUP_DIR = str(stage / 'backup')
            ElasticBackup(reason='kopiur').backup_all_indexes()
            archives = list((stage / 'backup').glob('*.zip'))
            check(len(archives) == 1, 'native ZIP missing')
            archives[0].rename(stage / 'elasticsearch.zip')
            shutil.rmtree(stage / 'backup')
            documents = inventory(stage / 'elasticsearch.zip')
            for index in INDICES:
                check(documents[index]['count'] == counts[index] == request(index + '/_count')['count'], 'index count changed or incomplete export')
            check(request('ta_*?expand_wildcards=all') == settings, 'index metadata changed during capture')
            (stage / 'indices.json').write_text(json.dumps(settings, sort_keys=True))
            manifest = {'format': 'ta-native-json-sqlite-v1', 'app_version': '0.5.12', 'es_version': root['version']['number'],
                        'started_utc': started, 'finished_utc': datetime.datetime.now(datetime.timezone.utc).isoformat(),
                        'consistency': 'per-index PIT and SQLite online backup; NOT cross-store atomic',
                        'redis': 'fresh-empty; discard transient jobs, sessions and progress; requeue deliberately',
                        'documents': documents, 'sqlite_tables': sqlite_counts,
                        'files': {name: digest(stage / name) for name in ('db.sqlite3', 'elasticsearch.zip', 'indices.json')}}
            (stage / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
            for path in stage.iterdir():
                path.chmod(0o640)
                with path.open('rb') as stream:
                    os.fsync(stream.fileno())
            stage.chmod(0o750)
            stage.rename(output)
            fd = os.open(output.parent, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        finally:
            if stage.exists():
                shutil.rmtree(stage)
    print('TubeArchivist native ZIP and SQLite capture validated')


def verify(bundle):
    manifest = json.loads((bundle / 'manifest.json').read_text())
    check(manifest['format'] == 'ta-native-json-sqlite-v1', 'wrong format')
    check(set(manifest['files']) == {'db.sqlite3', 'elasticsearch.zip', 'indices.json'}, 'unexpected artifacts')
    for name, expected in manifest['files'].items():
        check(digest(bundle / name) == expected, 'artifact checksum mismatch')
    check(sqlite_inventory(bundle / 'db.sqlite3') == manifest['sqlite_tables'], 'SQLite counts differ')
    check(inventory(bundle / 'elasticsearch.zip') == manifest['documents'], 'ES documents differ')
    check(set(json.loads((bundle / 'indices.json').read_text())) == set(INDICES), 'index set differs')
    return manifest


def restore(bundle):
    manifest = verify(bundle)
    root = request('')
    check(root['cluster_name'] == 'k8s92-ta-restore', 'refuse non-disposable Elasticsearch cluster')
    check(root['version']['number'] == manifest['es_version'], 'ES version mismatch')
    existing = request('_cat/indices?format=json&expand_wildcards=all')
    check(not existing, 'restore requires empty Elasticsearch, including hidden indices')
    settings = json.loads((bundle / 'indices.json').read_text())
    for index in INDICES:
        original = settings[index]['settings']['index']
        # Runtime UUIDs, allocation rules, ILM and global/security state are never imported.
        keep = {key: original[key] for key in ('analysis', 'number_of_shards', 'max_result_window', 'max_ngram_diff') if key in original}
        keep['number_of_replicas'] = 0
        request(index, 'PUT', {'settings': keep, 'mappings': settings[index]['mappings']})
    batch = []

    def send():
        if batch:
            response = request('_bulk?refresh=true', 'POST', '\n'.join(batch) + '\n', ndjson=True)
            check(not response.get('errors', True), 'bulk item restore failure')
            batch.clear()

    for meta, source in zip_records(bundle / 'elasticsearch.zip'):
        batch.extend((json.dumps({'index': meta}), json.dumps(source)))
        if len(batch) >= 200:
            send()
    send()
    # Compare every ID and source, not only counts or a health endpoint.
    actual = {}
    for index in INDICES:
        documents = {}
        response = request(index + '/_search?scroll=1m', 'POST', {'size': 500, 'sort': ['_doc'], 'query': {'match_all': {}}})
        scroll = response.get('_scroll_id')
        try:
            while response['hits']['hits']:
                for hit in response['hits']['hits']:
                    documents[hit['_id']] = hit['_source']
                response = request('_search/scroll', 'POST', {'scroll': '1m', 'scroll_id': scroll})
                scroll = response.get('_scroll_id', scroll)
        finally:
            if scroll:
                request('_search/scroll', 'DELETE', {'scroll_id': [scroll]})
        actual[index] = {'count': len(documents), 'sha256': hashlib.sha256(json.dumps(documents, sort_keys=True, separators=(',', ':')).encode()).hexdigest()}
    check(actual == manifest['documents'], 'restored Elasticsearch content differs')
    check({r['index'] for r in request('_cat/indices?format=json&expand_wildcards=all')} == set(INDICES), 'unexpected system/security indices')
    print('Seven isolated indices restored; all IDs/sources match; no global/security state')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('operation', choices=('capture', 'verify', 'restore-es'))
    parser.add_argument('directory', type=Path)
    args = parser.parse_args()
    os.umask(0o027)
    signal.signal(signal.SIGALRM, lambda *_: (_ for _ in ()).throw(TimeoutError('capture/restore deadline exceeded')))
    signal.alarm(600)
    directory = args.directory.resolve()
    if args.operation == 'capture':
        capture(directory)
    elif args.operation == 'restore-es':
        restore(directory)
    else:
        verify(directory)
        print('Artifact integrity and inventories verified')


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        # Do not print response bodies, document contents or credential-bearing URLs.
        print(f'recovery contract failed: {type(exc).__name__}', file=sys.stderr)
        sys.exit(1)
