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


def redis_client():
    # redis is shipped by the pinned application. Never inspect another DB.
    import redis
    client = redis.Redis.from_url(os.environ['REDIS_CON'], decode_responses=False,
                                  socket_timeout=30, socket_connect_timeout=30)
    check(client.connection_pool.connection_kwargs.get('db') == 15, 'Redis DB must be 15')
    return client


def redis_inventory(client):
    """Preserve opaque native values and absolute expiries, including queues.

    A script reads value and expiry together for each key. A second pass rejects
    concurrent mutations. This is NOT an all-writer or cross-store fence.
    """
    script = """local value = redis.call('DUMP', KEYS[1])
    if not value then return {} end
    local ttl = redis.call('PTTL', KEYS[1])
    local now = redis.call('TIME')
    local expiry = -1
    if ttl >= 0 then expiry = now[1] * 1000 + math.floor(now[2] / 1000) + ttl end
    return {value, expiry}
    """
    result = {}
    for key in client.scan_iter(count=500):
        record = client.eval(script, 1, key)
        check(len(record) == 2, 'Redis key changed during capture')
        encoded = base64.b64encode(key).decode('ascii')
        result[encoded] = {'dump': base64.b64encode(record[0]).decode('ascii'),
                           'expires_at_ms': int(record[1])}
    return result


def check_redis_inventory(records):
    check(isinstance(records, dict), 'invalid Redis inventory')
    for key, record in records.items():
        base64.b64decode(key, validate=True)
        check(set(record) == {'dump', 'expires_at_ms'}, 'invalid Redis record')
        check(bool(base64.b64decode(record['dump'], validate=True)), 'empty Redis dump')
        expiry = record['expires_at_ms']
        check(type(expiry) is int and (expiry == -1 or expiry >= 0), 'invalid Redis expiry')
    return records


def redis_instance_id(client):
    # Dragonfly v2 omits Redis INFO server.run_id, but exposes master_replid.
    identity = client.info('server').get('run_id') or client.info('replication').get('master_replid')
    check(bool(identity), 'Redis server has no supported instance identity')
    return identity


def compare_redis_inventory(before, after):
    check(set(before) == set(after), 'Redis key set changed during capture')
    for key, record in before.items():
        current = after[key]
        check(record['dump'] == current['dump'], 'Redis value changed during capture')
        left, right = record['expires_at_ms'], current['expires_at_ms']
        # TIME/PTTL resolution can differ by one millisecond across commands.
        check(left == right or (left >= 0 and right >= 0 and abs(left - right) <= 1),
              'Redis expiry changed during capture')


RECOVERY_KEY = b'k8s92:recovery:hold:v1'


def recovery_owner(bundle, manifest, client):
    return json.dumps({'format': 'ta-redis-recovery-hold-v1', 'db': 15,
                       'bundle_sha256': digest(bundle / 'manifest.json'),
                       'source_instance_id': manifest['redis']['source_instance_id'],
                       'target_instance_id': redis_instance_id(client)}, sort_keys=True).encode()


def restored_inventory(client):
    records = redis_inventory(client)
    records.pop(base64.b64encode(RECOVERY_KEY).decode(), None)
    return records


def unexpired_records(client, records):
    seconds, micros = client.time()
    now = seconds * 1000 + micros // 1000
    return {key: value for key, value in records.items()
            if value['expires_at_ms'] == -1 or value['expires_at_ms'] > now}


def verify_restored_redis(client, records, owner):
    check(client.get(RECOVERY_KEY) == owner, 'Redis recovery ownership changed')
    # Expiry may cross during SCAN. Use Redis time on both sides of the scan,
    # retaining expired source bytes only in the immutable archive.
    before = unexpired_records(client, records)
    actual = restored_inventory(client)
    after = unexpired_records(client, records)
    check(set(after) <= set(actual) <= set(before), 'Redis recovery target key set differs')
    compare_redis_inventory({key: before[key] for key in actual}, actual)


def restore_redis(bundle):
    manifest = verify(bundle)
    check(os.environ.get('K8S92_ISOLATED_RESTORE') == 'YES', 'isolated Redis restore required')
    client = redis_client()
    check(redis_instance_id(client) != manifest['redis']['source_instance_id'],
          'refuse source Redis server')
    records = check_redis_inventory(json.loads((bundle / 'redis.json').read_text()))
    check(base64.b64encode(RECOVERY_KEY).decode() not in records, 'reserved Redis recovery key in source')
    owner = recovery_owner(bundle, manifest, client)
    # Atomically claim a NEW empty disposable target or resume the same owned
    # bundle. A nonempty production/unowned DB can never be merged or reset.
    claim = """local owner = redis.call('GET', KEYS[1])
    if owner then return owner == ARGV[1] and 1 or 0 end
    if redis.call('DBSIZE') ~= 0 then return 0 end
    redis.call('SET', KEYS[1], ARGV[1]); return 1
    """
    check(client.eval(claim, 1, RECOVERY_KEY, owner) == 1, 'unowned or conflicting Redis target')
    script = """if redis.call('GET', KEYS[1]) ~= ARGV[1] then return -1 end
    local expiry = tonumber(ARGV[3])
    local now = redis.call('TIME')
    local ms = now[1] * 1000 + math.floor(now[2] / 1000)
    local current = redis.call('DUMP', KEYS[2])
    if expiry >= 0 and expiry <= ms then
        if current then return -2 end
        return 0
    end
    if current then
        if current ~= ARGV[2] then return -2 end
        local ttl = redis.call('PTTL', KEYS[2])
        if expiry == -1 then return ttl == -1 and 2 or -2 end
        return ttl >= 0 and math.abs(ms + ttl - expiry) <= 1 and 2 or -2
    end
    if expiry == -1 then redis.call('RESTORE', KEYS[2], 0, ARGV[2])
    else redis.call('RESTORE', KEYS[2], expiry, ARGV[2], 'ABSTTL') end
    return 1
    """
    for encoded, record in records.items():
        key = base64.b64decode(encoded, validate=True)
        value = base64.b64decode(record['dump'], validate=True)
        check(client.eval(script, 2, RECOVERY_KEY, key, owner, value,
                          record['expires_at_ms']) >= 0, 'Redis recovery record conflict')
    verify_restored_redis(client, records, owner)
    print('Redis DB15 restored resumably under execution hold, no replacement or enqueue')


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
            redis = redis_client()
            redis_source_id = redis_instance_id(redis)
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
            redis_state = redis_inventory(redis)
            compare_redis_inventory(redis_state, redis_inventory(redis))
            (stage / 'redis.json').write_text(json.dumps(redis_state, sort_keys=True))
            manifest = {'format': 'ta-native-json-sqlite-redis-v2', 'app_version': '0.5.12', 'es_version': root['version']['number'],
                        'started_utc': started, 'finished_utc': datetime.datetime.now(datetime.timezone.utc).isoformat(),
                        'consistency': 'per-index PIT and SQLite online backup; NOT cross-store atomic',
                        'redis': {'db': 15, 'source_instance_id': redis_source_id, 'keys': len(redis_state),
                                  'encoding': 'native-DUMP-with-absolute-expiry'},
                        'documents': documents, 'sqlite_tables': sqlite_counts,
                        'files': {name: digest(stage / name) for name in ('db.sqlite3', 'elasticsearch.zip', 'indices.json', 'redis.json')}}
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
    print('TubeArchivist native ZIP, SQLite and Redis capture validated')


def verify(bundle):
    manifest = json.loads((bundle / 'manifest.json').read_text())
    check(manifest['format'] == 'ta-native-json-sqlite-redis-v2', 'wrong format')
    check(set(manifest['files']) == {'db.sqlite3', 'elasticsearch.zip', 'indices.json', 'redis.json'}, 'unexpected artifacts')
    for name, expected in manifest['files'].items():
        check(digest(bundle / name) == expected, 'artifact checksum mismatch')
    check(sqlite_inventory(bundle / 'db.sqlite3') == manifest['sqlite_tables'], 'SQLite counts differ')
    check(inventory(bundle / 'elasticsearch.zip') == manifest['documents'], 'ES documents differ')
    check(set(json.loads((bundle / 'indices.json').read_text())) == set(INDICES), 'index set differs')
    records = check_redis_inventory(json.loads((bundle / 'redis.json').read_text()))
    check(manifest['redis']['db'] == 15 and len(records) == manifest['redis']['keys'], 'Redis coverage differs')
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
    parser.add_argument('operation', choices=('capture', 'verify', 'restore-es', 'restore-redis'))
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
    elif args.operation == 'restore-redis':
        restore_redis(directory)
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
