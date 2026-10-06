"""Credential-free, disposable ARC fixtures for shared non-relational stores.

Only native engine recovery is qualified. No production, NAS/R2, retention,
stream-consumer, security-identity or coherent multi-application claim is made.
"""
import base64
import hashlib
import io
import inspect
import json
import multiprocessing
import os
import secrets
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
from typing import Any
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from kopiur_fixture_fence import FixtureFence
from kopiur_fixture_manifest import GenerationManifest, validate_endpoint

IMAGES = {
    'dragonfly': 'ghcr.io/dragonflydb/dragonfly:v2.0.0@sha256:7426fdb31ddcf7bd9499b4205f36ebaa83b26149ba1609a0d5f8f474b3631233',
    'elasticsearch': 'docker.elastic.co/elasticsearch/elasticsearch:8.19.23@sha256:4d0724bb8d78d7a2330693623e26ca4d621079d062ecc8a30b3e1fc180ec14c1',
    'rabbitmq-server': 'docker.io/library/rabbitmq:4.2.6-management@sha256:3ab808deef2f6552bc10ede59bdba1437b0d5c778e29d8927c5afbfa2586c22f',
}
MAX_ARCHIVE = 192 * 1024 * 1024
CLIENT_IMAGE = 'docker.io/library/python:3.14.7-slim@sha256:51dafde81dbdb6ebde285137a295cf18a47ca95234fe388a343719cb97305b3d'


def elasticsearch_admission(settings):
    """Observe exact fixture identity and preserve absent versus false blocks.

    Settings writes lack ownership CAS. This prerequisite does not qualify
    production fencing or replace a supervised generation boundary.
    """
    if not isinstance(settings, dict) or set(settings) != {'fixture'}:
        raise RuntimeError('exact Elasticsearch fixture index required')
    entry = settings['fixture']
    if not isinstance(entry, dict) or not isinstance(entry.get('settings'), dict):
        raise RuntimeError('Elasticsearch fixture settings absent')
    values = entry['settings']
    identity = values.get('index.uuid')
    if not isinstance(identity, str) or not identity:
        raise RuntimeError('Elasticsearch fixture index identity absent')
    block = values.get('index.blocks.write')
    if block not in (None, 'true', 'false') or (
            'index.blocks.write' in values and block is None):
        raise RuntimeError('Elasticsearch fixture admission malformed')
    return {'index_uuid': identity, 'write_block': block}


def validate_archive(data):
    if not data or len(data) > MAX_ARCHIVE:
        raise ValueError('native archive exceeds bound or is empty')
    names = set()
    total = 0
    with tarfile.open(fileobj=io.BytesIO(data), mode='r:') as archive:
        for member in archive:
            name = member.name
            parts = name.split('/')
            if (name.startswith('/') or '..' in parts or name in names
                    or not (member.isfile() or member.isdir()) or member.size < 0):
                raise ValueError('unsafe native archive')
            names.add(name)
            total += member.size
            if total > MAX_ARCHIVE:
                raise ValueError('native archive expanded size exceeds bound')
    if not names:
        raise ValueError('native archive has no members')
    return hashlib.sha256(data).hexdigest()


def resp_read(stream, depth=0) -> Any:
    if depth > 16:
        raise ValueError('RESP nesting exceeds bound')
    line = stream.readline(65537)
    if not line.endswith(b'\r\n') or len(line) > 65536:
        raise ValueError('invalid RESP frame')
    kind, value = line[:1], line[1:-2]
    if kind == b'+':
        return value
    if kind == b'-':
        raise RuntimeError('native RESP command rejected')
    if kind == b':':
        return int(value)
    if kind not in (b'$', b'*'):
        raise ValueError('unsupported RESP frame')
    size = int(value)
    if size == -1:
        return None
    if size < 0 or size > 1024 * 1024:
        raise ValueError('RESP size exceeds bound')
    if kind == b'*':
        if size > 4096:
            raise ValueError('RESP array exceeds bound')
        return [resp_read(stream, depth + 1) for _ in range(size)]
    data = stream.read(size)
    if len(data) != size or stream.read(2) != b'\r\n':
        raise ValueError('truncated RESP bulk frame')
    return data


def repository_envelope(data):
    """Create the missing ES snapshot directory without changing native bytes."""
    validate_archive(data)
    result = io.BytesIO()
    with tarfile.open(fileobj=io.BytesIO(data), mode='r:') as source:
        with tarfile.open(fileobj=result, mode='w') as target:
            for member in source:
                original = member.name
                member.name = 'snapshot/' + original.removeprefix('./').lstrip('/')
                if original in ('.', './'):
                    member.name = 'snapshot'
                target.addfile(member, source.extractfile(member) if member.isfile() else None)
    validate_archive(result.getvalue())
    return result.getvalue()


def validate_elasticsearch_restore(result):
    """A completed HTTP request does not prove all native shards recovered."""
    snapshot = result.get('snapshot', {})
    shards = snapshot.get('shards', {})
    total = shards.get('total')
    if (type(total) is not int or total <= 0
            or type(shards.get('successful')) is not int
            or shards['successful'] != total
            or type(shards.get('failed')) is not int or shards['failed'] != 0
            or not isinstance(snapshot.get('indices'), list)
            or 'fixture' not in snapshot['indices']
            or any(not isinstance(index, str) or (index != 'fixture' and not index.startswith('.security-'))
                   for index in snapshot['indices'])):
        raise RuntimeError('Elasticsearch native restore is incomplete')
    return total


class Fixture:
    def __init__(self, service, *, docker_endpoint=None, require_explicit_endpoint=False,
                 generation_manifest=None):
        if service not in IMAGES:
            raise ValueError('unknown shared-store fixture')
        if not os.environ.get('RUNNER_NAME', '').startswith('ghar-set-zoo-') or sys.platform != 'linux':
            raise RuntimeError('native fixtures require the owned Linux ARC runner')
        if require_explicit_endpoint and docker_endpoint is None:
            raise ValueError('explicit fixture Docker endpoint required')
        if docker_endpoint is not None:
            validate_endpoint(docker_endpoint)
        # Explicit routing is a prerequisite, not proof of a private daemon or
        # server containment. Legacy fixtures retain their approved ARC endpoint.
        self.docker_endpoint = docker_endpoint
        self.service = service
        self.prefix = 'k8s92-nonrel-' + uuid.uuid4().hex
        self.generation_manifest = generation_manifest
        if generation_manifest is not None:
            if (not isinstance(generation_manifest, GenerationManifest)
                    or docker_endpoint is None or generation_manifest.endpoint != docker_endpoint):
                raise ValueError('manifest requires its exact explicit Docker endpoint')
            state = generation_manifest.read()
            if state['revoked'] or state['containers']:
                raise RuntimeError('manifest generation requires reconciliation')
            self.prefix = generation_manifest.generation
        self.network = self.prefix + '-net'
        self.containers = []
        self.container_ids = {}
        self.deadline = time.monotonic() + 1200
        self.auth = None
        self.client = None
        self.client_started = False
        self.host = None

    def docker_command(self, *args):
        if self.docker_endpoint is None:
            return ['docker', *args]
        return ['docker', '--host', self.docker_endpoint, *args]

    def check_dispatch(self):
        if self.generation_manifest is not None and self.generation_manifest.read()['revoked']:
            raise RuntimeError('fixture generation dispatch revoked')

    def run(self, *args, data=None, timeout=180):
        self.check_dispatch()
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeError('native fixture deadline exceeded')
        result = subprocess.run(self.docker_command(*args), input=data, capture_output=True,
                                timeout=min(timeout, remaining))
        if result.returncode:
            # Docker output can contain the fixture password or message bodies.
            raise RuntimeError('native Docker operation failed: ' + args[0])
        return result.stdout

    def create(self, label, port, variables=None, args=(), extra=(), archive=None, path=None):
        name = self.prefix + '-' + label
        env = os.environ.copy()
        options = []
        for key, value in (variables or {}).items():
            env[key] = value
            options.extend(('-e', key))
        operation = None
        labels = []
        if self.generation_manifest is not None:
            operation = self.generation_manifest.create_intent(name)
            labels = ['--label', 'kopiur.fixture-generation=' + self.prefix,
                      '--label', 'kopiur.fixture-operation=' + operation]
        command = self.docker_command('create', '--name', name, '--network', self.network,
                   *extra, *options, *labels,
                   IMAGES[self.service], *args)
        # Track exact names even if create partly succeeds, cleanup never prunes.
        self.containers.append(name)
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeError('native fixture deadline exceeded before create')
        self.check_dispatch()
        result = subprocess.run(command, env=env, capture_output=True, timeout=min(60, remaining))
        if result.returncode:
            raise RuntimeError('native fixture create failed')
        container_id = result.stdout.decode().strip()
        if len(container_id) != 64 or any(c not in '0123456789abcdef' for c in container_id):
            raise RuntimeError('native fixture container identity was not established')
        self.container_ids[name] = container_id
        if self.run('inspect', '--format', '{{.Id}}', name).decode().strip() != container_id:
            raise RuntimeError('native fixture container identity changed during create')
        if self.generation_manifest is not None:
            self.generation_manifest.register(name, operation, container_id)
        if archive is not None:
            validate_archive(archive)
            self.run('cp', '-a', '-', container_id + ':' + path, data=archive)
        if self.generation_manifest is not None:
            self.generation_manifest.start_intent(name, operation, container_id)
        self.run('start', container_id)
        if self.generation_manifest is not None:
            self.generation_manifest.started(name, operation, container_id)
        self.host = name
        return name, port

    def remove(self, name):
        container_id = self.container_ids.get(name)
        if (name not in self.containers or container_id is None
                or self.run('inspect', '--format', '{{.Id}}', name).decode().strip() != container_id):
            raise RuntimeError('native fixture container identity changed before removal')
        # Docker daemon/server work is not contained by the invoking client's
        # cgroup. Retire the exact server lifetime and read daemon state back
        # before any fresh restore can proceed. A name-only rm is not proof.
        self.run('rm', '-fv', container_id)
        remaining = self.run('ps', '-a', '--no-trunc', '--format', '{{.ID}}').decode().splitlines()
        if container_id in remaining:
            raise RuntimeError('native fixture server lifetime remains after removal')
        self.containers.remove(name)
        del self.container_ids[name]
        return {'container_id': container_id, 'daemon_inventory_absent': True,
                'production_mutation_cessation_qualified': False}

    def cleanup(self):
        failures = []
        for name in list(self.containers):
            container_id = self.container_ids.get(name)
            if container_id is None:
                failures.append('lacks immutable container identity')
                continue
            # Cleanup has its own bounded deadline but the same identity fence.
            # Never fall back to deleting a potentially replaced container name.
            try:
                subprocess.run(self.docker_command('rm', '-fv', container_id), capture_output=True, timeout=60, check=True)
                inventory = subprocess.run(
                    self.docker_command('ps', '-a', '--no-trunc', '--format', '{{.ID}}'),
                    capture_output=True, timeout=60, check=True)
                if container_id in inventory.stdout.decode().splitlines():
                    raise RuntimeError('server lifetime remains after removal')
            except (OSError, subprocess.SubprocessError, RuntimeError, UnicodeError):
                failures.append('exact server retirement unresolved')
                continue
            self.containers.remove(name)
            del self.container_ids[name]
        try:
            subprocess.run(self.docker_command('network', 'rm', self.network), capture_output=True, timeout=60, check=True)
        except (OSError, subprocess.SubprocessError):
            failures.append('network retirement unresolved')
        if failures:
            # Preserve unresolved identities for reconciliation, but do not let
            # one failure prevent retiring other established server lifetimes.
            # Never expose subprocess output, which can contain fixture secrets.
            raise RuntimeError('native cleanup unresolved: ' + '; '.join(failures))

    def registered_id(self, name):
        container_id = self.container_ids.get(name)
        if name not in self.containers or container_id is None:
            raise RuntimeError('native container lacks registered immutable identity')
        return container_id

    def ready(self, name, check):
        deadline = min(self.deadline, time.monotonic() + 240)
        while time.monotonic() < deadline:
            state = json.loads(self.run('inspect', '--format', '{{json .State}}', self.registered_id(name)))
            if state['Status'] in ('exited', 'dead'):
                raise RuntimeError('native fixture exited before readiness')
            try:
                if check():
                    return
            except (OSError, urllib.error.URLError, RuntimeError):
                pass
            time.sleep(2)
        raise RuntimeError('native fixture readiness deadline exceeded')

    def client_request(self, payload):
        # Docker internal networks intentionally do not publish ports. The
        # credential-free client joins only this UUID-owned isolated network.
        if self.client is None:
            self.client = self.prefix + '-client'
            self.containers.append(self.client)
            operation = None
            labels = []
            if self.generation_manifest is not None:
                operation = self.generation_manifest.create_intent(self.client)
                labels = ['--label', 'kopiur.fixture-generation=' + self.prefix,
                          '--label', 'kopiur.fixture-operation=' + operation]
            container_id = self.run('create', '--name', self.client, '--network', self.network,
                                    '--memory=128m', *labels, CLIENT_IMAGE, 'python', '-c',
                                    'import time;time.sleep(1200)').decode().strip()
            if len(container_id) != 64 or any(c not in '0123456789abcdef' for c in container_id):
                raise RuntimeError('native client container identity was not established')
            self.container_ids[self.client] = container_id
            if self.run('inspect', '--format', '{{.Id}}', self.client).decode().strip() != container_id:
                raise RuntimeError('native client container identity changed during create')
            if self.generation_manifest is not None:
                self.generation_manifest.register(self.client, operation, container_id)
                self.generation_manifest.start_intent(self.client, operation, container_id)
            self.run('start', container_id)
            if self.generation_manifest is not None:
                self.generation_manifest.started(self.client, operation, container_id)
            self.client_started = True
        if not self.client_started:
            raise RuntimeError('native client startup remains unresolved')
        code = ('import sys,json,base64,socket,urllib.request,urllib.error\nfrom typing import Any\n'
                + inspect.getsource(resp_read) + '''
p=json.load(sys.stdin)
if p['operation']=='redis':
    items=[base64.b64decode(x) for x in p['args']]
    wire=b'*'+str(len(items)).encode()+b'\\r\\n'
    wire+=b''.join(b'$'+str(len(x)).encode()+b'\\r\\n'+x+b'\\r\\n' for x in items)
    with socket.create_connection((p['host'],p['port']),timeout=20) as connection:
        connection.sendall(wire)
        with connection.makefile('rb') as stream: result=resp_read(stream)
else:
    data=None if p['body'] is None else json.dumps(p['body']).encode()
    headers={'Content-Type':'application/json'}
    if p['auth']: headers['Authorization']='Basic '+base64.b64encode(p['auth'].encode()).decode()
    request=urllib.request.Request('http://'+p['host']+':'+str(p['port'])+p['path'],data=data,headers=headers,method=p['method'])
    try:
        response=urllib.request.urlopen(request,timeout=60)
    except urllib.error.HTTPError as error:
        if not p.get('expect_write_block'): raise
        raw=error.read(65537)
        if len(raw)>65536: raise ValueError('error response bound')
        failure=json.loads(raw)
        result={'status':error.code,'error_type':failure.get('error',{}).get('type')}
        response=None
    if response is not None and p.get('expect_write_block'):
        response.close()
        raise ValueError('write unexpectedly allowed')
    if response is not None:
      with response:
        raw=response.read(8*1024*1024+1)
        if len(raw)>8*1024*1024: raise ValueError('response bound')
        result=json.loads(raw) if raw else {}
print(json.dumps(result,default=lambda x:{'__binary__':base64.b64encode(x).decode()}))
''')
        wire = json.dumps(payload | {'host': self.host}).encode()
        result = self.run('exec', '-i', self.registered_id(self.client), 'python', '-c', code, data=wire, timeout=90)
        return json.loads(result, object_hook=lambda item: base64.b64decode(item['__binary__'])
                          if set(item) == {'__binary__'} else item)

    def redis(self, port, *args):
        items = [x if isinstance(x, bytes) else str(x).encode() for x in args]
        return self.client_request({'operation': 'redis', 'port': port,
                                    'args': [base64.b64encode(x).decode() for x in items]})

    def http(self, port, path, method='GET', body=None, *, expect_write_block=False) -> Any:
        return self.client_request({'operation': 'http', 'port': port, 'path': path,
                                    'method': method, 'body': body, 'auth': self.auth,
                                    'expect_write_block': expect_write_block})

    def dragonfly(self):
        args = ('--force_epoll', '--proactor_threads=2', '--maxmemory=512Mi',
                '--dir=/data', '--dbfilename=fixture', '--df_snapshot_format=true')
        source, port = self.create('source', 6379, args=args, extra=('--memory=768m',))
        self.ready(source, lambda: self.redis(port, 'PING') == b'PONG')
        commands = [('SET', 'binary', b'\x00\xfffixture'), ('HSET', 'hash', 'field', 'value'),
                    ('RPUSH', 'list', 'first', 'second'), ('SADD', 'set', 'first', 'second'),
                    ('ZADD', 'zset', '1.25', 'first', '2.5', 'second'),
                    ('XADD', 'stream', '1-0', 'field', 'first'),
                    ('XADD', 'stream', '2-0', 'field', 'second'),
                    ('XGROUP', 'CREATE', 'stream', 'group', '0'),
                    ('XREADGROUP', 'GROUP', 'group', 'consumer', 'COUNT', '1', 'STREAMS', 'stream', '>')]
        for command in commands:
            self.redis(port, *command)
        reads = [('GET', 'binary'), ('HGETALL', 'hash'), ('LRANGE', 'list', '0', '-1'),
                 ('SMEMBERS', 'set'), ('ZRANGE', 'zset', '0', '-1', 'WITHSCORES'),
                 ('XRANGE', 'stream', '-', '+'), ('XPENDING', 'stream', 'group'),
                 ('XINFO', 'GROUPS', 'stream'), ('DBSIZE',)]
        def inventory(listener):
            values = [self.redis(listener, *command) for command in reads]
            values[3] = sorted(values[3])
            return values
        expected = inventory(port)
        if self.redis(port, 'SAVE') != b'OK':
            raise RuntimeError('Dragonfly snapshot did not complete')
        archive = self.run('cp', source + ':/data/.', '-')
        digest = validate_archive(archive)
        self.remove(source)
        restored, port = self.create('restore', 6379, args=args, extra=('--memory=768m',),
                                     archive=archive, path='/data')
        self.ready(restored, lambda: self.redis(port, 'PING') == b'PONG')
        if inventory(port) != expected:
            raise RuntimeError('Dragonfly native keys or stream state differ')
        return {'snapshot_sha256': digest, 'keys_and_stream_pending_equal': True,
                'ttl_and_acl_recovery_qualified': False}

    def elasticsearch(self, transport=None):
        password = secrets.token_hex(24)
        self.auth = 'elastic:' + password
        variables = {'discovery.type': 'single-node', 'xpack.security.enabled': 'true',
                     'xpack.security.http.ssl.enabled': 'false', 'ELASTIC_PASSWORD': password,
                     'xpack.ml.enabled': 'false', 'ingest.geoip.downloader.enabled': 'false',
                     'ES_JAVA_OPTS': '-Xms256m -Xmx256m -XX:ActiveProcessorCount=2',
                     'path.repo': '/usr/share/elasticsearch/data/snapshot'}
        # Docker cp -a misresolves this image's numeric USER 1000:0. Its
        # named elasticsearch account preserves the same non-root UID/GID.
        extra = ('--memory=1600m', '--user=elasticsearch')
        source, port = self.create('source', 9200, variables, extra=extra)
        self.ready(source, lambda: self.http(port, '/_cluster/health')['status'] in ('yellow', 'green'))
        role = {'cluster': [], 'indices': [{'names': ['fixture'], 'privileges': ['read']}]}
        self.http(port, '/_security/role/fixture-reader', 'PUT', role)
        user_password = secrets.token_hex(24)
        self.http(port, '/_security/user/fixture-reader', 'PUT',
                  {'password': user_password, 'roles': ['fixture-reader'], 'enabled': True})
        mappings = {'properties': {'title': {'type': 'keyword'}, 'number': {'type': 'integer'},
                                   '@timestamp': {'type': 'date'}, 'tags': {'type': 'keyword'}}}
        self.http(port, '/fixture', 'PUT', {'settings': {'number_of_shards': 1, 'number_of_replicas': 0},
                                          'mappings': mappings, 'aliases': {'fixture-alias': {}}})
        for number in (1, 2):
            self.http(port, '/fixture/_doc/' + str(number) + '?refresh=true', 'PUT',
                      {'title': 'fixture-' + str(number), 'number': number,
                       '@timestamp': '2026-01-0' + str(number) + 'T00:00:00Z',
                       'tags': ['fixture', 'sample-' + str(number)]})
        # Synthetic metadata only. Never interpret this fixture policy as an
        # approved production retention or recovery objective.
        policy = {'policy': {'phases': {'delete': {'min_age': '30d', 'actions': {'delete': {}}}}}}
        self.http(port, '/_ilm/policy/fixture-retention', 'PUT', policy)
        self.http(port, '/fixture/_settings', 'PUT',
                  {'index.lifecycle.name': 'fixture-retention'})
        self.http(port, '/_index_template/fixture-template', 'PUT',
                  {'index_patterns': ['fixture-*'], 'template': {'mappings': mappings}})
        self.http(port, '/_ingest/pipeline/fixture-pipeline', 'PUT',
                  {'processors': [{'set': {'field': 'fixture', 'value': True}}]})
        reads = ('/fixture/_search?sort=number&size=10', '/fixture/_mapping',
                 '/fixture/_alias', '/_index_template/fixture-template', '/_ingest/pipeline/fixture-pipeline',
                 '/fixture/_settings?flat_settings=true', '/_ilm/policy/fixture-retention',
                 '/fixture-alias/_search?q=tags:sample-2&sort=number&size=10')
        def inventory(listener):
            values = [self.http(listener, path) for path in reads]
            values[0] = [{'_id': hit['_id'], '_source': hit['_source']} for hit in values[0]['hits']['hits']]
            # Native restore assigns a fresh internal index UUID. Document IDs,
            # creation timestamp and every other persisted setting must match.
            values[5]['fixture']['settings'].pop('index.uuid')
            values[6] = values[6]['fixture-retention']['policy']
            values[7] = [{'_id': hit['_id'], '_source': hit['_source']} for hit in values[7]['hits']['hits']]
            return values
        expected = inventory(port)
        prior_admission = elasticsearch_admission(
            self.http(port, '/fixture/_settings?flat_settings=true'))

        def observed_admission():
            actual = elasticsearch_admission(
                self.http(port, '/fixture/_settings?flat_settings=true'))
            if actual['index_uuid'] != prior_admission['index_uuid']:
                raise RuntimeError('Elasticsearch fixture index replaced')
            return actual

        # The add-block API waits for in-flight writes before acknowledging the
        # block. Test the actual writer boundary, not merely a settings flag.
        def block():
            observed_admission()
            fence = self.http(port, '/fixture/_block/write', 'PUT')
            if (fence.get('acknowledged') is not True
                    or fence.get('shards_acknowledged') is not True
                    or fence.get('indices') != [{'name': 'fixture', 'blocked': True}]):
                raise RuntimeError('Elasticsearch writer fence was not acknowledged')
            if observed_admission()['write_block'] != 'true':
                raise RuntimeError('Elasticsearch writer fence was not observed')

        def restore_admission(saved):
            actual = elasticsearch_admission(self.http(port, '/fixture/_settings?flat_settings=true'))
            if actual['index_uuid'] != saved['index_uuid']:
                raise RuntimeError('Elasticsearch fixture index replaced before recovery')
            if self.http(port, '/fixture/_settings', 'PUT',
                         {'index.blocks.write': saved['write_block']}).get('acknowledged') is not True:
                raise RuntimeError('Elasticsearch fixture writer resume failed')
            if elasticsearch_admission(self.http(port, '/fixture/_settings?flat_settings=true')) != saved:
                raise RuntimeError('Elasticsearch prior fixture admission differs')

        def release():
            restore_admission(prior_admission)

        def restart_observation():
            return elasticsearch_admission(self.http(port, '/fixture/_settings?flat_settings=true'))

        # A fresh journal owner recovers intent left by a lost capture owner.
        # Only this disposable source/index is ever admitted, not production.
        with tempfile.TemporaryDirectory(prefix=self.prefix + '-') as directory:
            journal = Path(directory) / 'fence.json'
            for point in ('intent', 'fenced', 'released-before-journal'):
                def interrupted_capture():
                    with FixtureFence(journal, source, admission=prior_admission) as boundary:
                        def interrupted_block():
                            block()
                            if point == 'intent':
                                os._exit(73)
                        boundary.acquire(interrupted_block)
                        if point == 'released-before-journal':
                            def interrupted_release():
                                release()
                                os._exit(73)
                            boundary.recover(interrupted_release)
                        os._exit(73)

                # Linux ARC only. The child uses the already-created fixture
                # client and real ES API, then exits without context cleanup.
                child = multiprocessing.get_context('fork').Process(target=interrupted_capture)
                child.start()
                try:
                    child.join(timeout=min(90, max(0, self.deadline - time.monotonic())))
                    if child.is_alive() or child.exitcode != 73:
                        raise RuntimeError('Elasticsearch fixture interruption failed')
                finally:
                    if child.is_alive():
                        child.kill()
                        child.join(timeout=10)
                    child.close()
                with FixtureFence.restart_native(journal, source, restart_observation) as (boundary, saved):
                    expected_phase = 'intent' if point == 'intent' else 'fenced'
                    if (boundary.read() != expected_phase
                            or not boundary.recover(lambda: restore_admission(saved))
                            or boundary.recover(lambda: restore_admission(saved))):
                        raise RuntimeError('Elasticsearch fixture restart recovery differs')
                self.http(port, '/fixture/_doc/resumed?refresh=true', 'PUT',
                          {'title': 'post-restart-write', 'number': 3})
                self.http(port, '/fixture/_doc/resumed?refresh=true', 'DELETE')
            # A previously blocked index must remain blocked across a lost
            # release acknowledgement. Its prior state is journaled before
            # mutation and survives a new owner's object construction.
            self.http(port, '/fixture/_settings', 'PUT', {'index.blocks.write': 'true'})
            preblocked = observed_admission()
            closed_journal = Path(directory) / 'preblocked.json'
            with FixtureFence(closed_journal, source, admission=preblocked) as boundary:
                boundary.acquire(block)
                def lost_closed_release():
                    self.http(port, '/fixture/_settings', 'PUT', {'index.blocks.write': 'true'})
                    raise RuntimeError('injected lost preblocked release acknowledgement')
                try:
                    boundary.recover(lost_closed_release)
                except RuntimeError as error:
                    if str(error) != 'injected lost preblocked release acknowledgement':
                        raise
            with FixtureFence.restart_native(closed_journal, source, restart_observation) as (boundary, saved):
                def preserve_closed():
                    restore_admission(saved)
                if not boundary.recover(preserve_closed) or boundary.recover(preserve_closed):
                    raise RuntimeError('preblocked native journal recovery differs')
            closed_denial = self.http(port, '/fixture/_doc/preblocked?refresh=true', 'PUT',
                                     {'title': 'must-stay-blocked'}, expect_write_block=True)
            if closed_denial != {'status': 403, 'error_type': 'cluster_block_exception'}:
                raise RuntimeError('preblocked native writer boundary reopened')
            release()
            with FixtureFence(journal, source, admission=prior_admission) as boundary:
                boundary.acquire(block)
        denial = self.http(port, '/fixture/_doc/fenced?refresh=true', 'PUT',
                           {'title': 'must-not-enter-capture', 'number': 3}, expect_write_block=True)
        if (denial != {'status': 403, 'error_type': 'cluster_block_exception'}
                or self.http(port, '/fixture/_count')['count'] != 2):
            raise RuntimeError('Elasticsearch writer boundary was not enforced')
        repo = {'type': 'fs', 'settings': {'location': '/usr/share/elasticsearch/data/snapshot'}}
        self.http(port, '/_snapshot/fixture', 'PUT', repo)
        result = self.http(port, '/_snapshot/fixture/generation?wait_for_completion=true', 'PUT',
                           {'indices': 'fixture', 'include_global_state': True,
                            'feature_states': ['security']})['snapshot']
        if result['state'] != 'SUCCESS' or result['shards']['failed'] != 0 or result.get('failures'):
            raise RuntimeError('Elasticsearch native snapshot is partial')
        if not any(state.get('feature_name') == 'security' and state.get('indices')
                   for state in result.get('feature_states', [])):
            raise RuntimeError('Elasticsearch security feature state is absent')
        archive = self.run('cp', self.registered_id(source) + ':/usr/share/elasticsearch/data/snapshot/.', '-')
        digest = validate_archive(archive)
        source_retirement = self.remove(source)
        transport_receipt = None
        if transport is not None:
            archive, transport_receipt = transport(archive)
            if validate_archive(archive) != digest:
                raise RuntimeError('Elasticsearch provider-restored repository differs')
        restored, port = self.create('restore', 9200, variables, extra=extra,
                                     archive=repository_envelope(archive), path='/usr/share/elasticsearch/data')
        self.ready(restored, lambda: self.http(port, '/_cluster/health')['status'] in ('yellow', 'green'))
        self.http(port, '/_snapshot/fixture', 'PUT', {'type': 'fs', 'settings': repo['settings'] | {'readonly': True}})
        restore = self.http(port, '/_snapshot/fixture/generation/_restore?wait_for_completion=true', 'POST',
                            {'indices': 'fixture', 'include_global_state': True,
                             'feature_states': ['security']})
        shard_count = validate_elasticsearch_restore(restore)
        self.ready(restored, lambda: self.http(port, '/_cluster/health/fixture')['status'] == 'green')
        restored_settings = self.http(port, '/fixture/_settings?flat_settings=true')
        if restored_settings['fixture']['settings'].get('index.blocks.write') != 'true':
            raise RuntimeError('Elasticsearch captured writer fence was lost')
        # Release only the synthetic index after recovery. Production consumer
        # fences, multi-store generations and restart watchdogs remain open.
        released = self.http(port, '/fixture/_settings', 'PUT', {'index.blocks.write': None})
        if released.get('acknowledged') is not True:
            raise RuntimeError('Elasticsearch fixture writer resume failed')
        actual = inventory(port)
        if actual != expected:
            mismatches = [reads[i] for i in range(len(reads)) if actual[i] != expected[i]]
            raise RuntimeError('Elasticsearch documents or metadata differ: ' + ', '.join(mismatches))
        recovered_role = self.http(port, '/_security/role/fixture-reader').get('fixture-reader', {})
        if (recovered_role.get('cluster') != []
                or len(recovered_role.get('indices', [])) != 1
                or recovered_role['indices'][0].get('names') != ['fixture']
                or recovered_role['indices'][0].get('privileges') != ['read']):
            raise RuntimeError('Elasticsearch restored role differs')
        self.auth = 'fixture-reader:' + user_password
        identity = self.http(port, '/_security/_authenticate')
        if identity.get('username') != 'fixture-reader' or identity.get('roles') != ['fixture-reader']:
            raise RuntimeError('Elasticsearch original synthetic user did not recover')
        hits = self.http(port, '/fixture/_search?sort=number&size=10')['hits']['hits']
        if [{'_id': hit['_id'], '_source': hit['_source']} for hit in hits] != expected[0]:
            raise RuntimeError('Elasticsearch original user cannot read restored documents')
        self.auth = 'elastic:' + password
        return {'snapshot_sha256': digest, 'snapshot_uuid': result['uuid'],
                'source_server_retirement': source_retirement,
                'documents_mappings_aliases_templates_pipelines_equal': True,
                'timestamps_tags_alias_query_settings_ilm_equal': True,
                'restored_shards': shard_count,
                'fixture_retention_policy': policy['policy'],
                'production_retention_approved': False,
                'synthetic_security_feature_state_recovered': True,
                'synthetic_writer_fence_restored_and_released': True,
                'synthetic_source_fence_journal_recovery_exercised': True,
                'synthetic_native_admission_journal_bound': True,
                'synthetic_preblocked_lost_release_ack_preserved': True,
                'synthetic_capture_process_loss_points': ['intent', 'fenced', 'released-before-journal'],
                'production_restart_watchdog_qualified': False,
                'provider_native_fixture_receipt': transport_receipt,
                'production_consumer_coherence_qualified': False,
                'security_feature_state_recovery_qualified': False}

    def rabbitmq(self):
        password = secrets.token_hex(24)
        self.auth = 'fixture:' + password
        variables = {'RABBITMQ_DEFAULT_USER': 'fixture', 'RABBITMQ_DEFAULT_PASS': password,
                     'RABBITMQ_NODENAME': 'rabbit@fixture'}
        extra = ('--hostname', 'fixture', '--memory=768m')
        source, port = self.create('source', 15672, variables, extra=extra)
        self.ready(source, lambda: self.http(port, '/api/overview').get('rabbitmq_version') is not None)
        for kind in ('classic', 'quorum'):
            self.http(port, '/api/queues/%2F/' + kind, 'PUT',
                      {'durable': True, 'auto_delete': False, 'arguments': {'x-queue-type': kind}})
            for number in (1, 2):
                result = self.http(port, '/api/exchanges/%2F/amq.default/publish', 'POST',
                                   {'routing_key': kind, 'payload': kind + '-' + str(number),
                                    'payload_encoding': 'string', 'properties': {'delivery_mode': 2}})
                if result.get('routed') is not True:
                    raise RuntimeError('RabbitMQ fixture message was not routed')
        definitions = self.http(port, '/api/definitions')
        # Producers/consumers are absent and the entire single-node fixture is
        # stopped gracefully. Definitions-only export is never a message backup.
        self.run('stop', '--time', '90', source, timeout=120)
        state = json.loads(self.run('inspect', '--format', '{{json .State}}', source))
        if state['Running'] or state['ExitCode'] != 0:
            raise RuntimeError('RabbitMQ broker did not stop cleanly')
        archive = self.run('cp', source + ':/var/lib/rabbitmq/.', '-')
        digest = validate_archive(archive)
        self.remove(source)
        restored, port = self.create('restore', 15672, variables, extra=extra,
                                     archive=archive, path='/var/lib/rabbitmq')
        self.ready(restored, lambda: self.http(port, '/api/overview').get('rabbitmq_version') is not None)
        actual = self.http(port, '/api/definitions')
        for key in ('users', 'vhosts', 'permissions', 'topic_permissions', 'parameters',
                    'global_parameters', 'policies', 'queues', 'exchanges', 'bindings'):
            canonical = lambda rows: sorted(json.dumps(row, sort_keys=True) for row in rows)
            if canonical(actual.get(key, [])) != canonical(definitions.get(key, [])):
                raise RuntimeError('RabbitMQ restored topology or identity differs')
        for kind in ('classic', 'quorum'):
            messages = self.http(port, '/api/queues/%2F/' + kind + '/get', 'POST',
                                 {'count': 10, 'ackmode': 'ack_requeue_false',
                                  'encoding': 'auto', 'truncate': 50000})
            if [message['payload'] for message in messages] != [kind + '-1', kind + '-2']:
                raise RuntimeError('RabbitMQ durable messages differ')
            if any(message['properties'].get('delivery_mode') != 2 for message in messages):
                raise RuntimeError('RabbitMQ durable message properties differ')
        return {'snapshot_sha256': digest, 'same_node_name_and_cookie_restored': True,
                'topology_users_and_classic_quorum_messages_equal': True,
                'stream_and_cluster_recovery_qualified': False}


def fixture(service, *, transport=None, deadline=None):
    if transport is not None and service != 'elasticsearch':
        raise ValueError('provider callback is restricted to Elasticsearch')
    run = Fixture(service)
    if deadline is not None:
        run.deadline = min(run.deadline, deadline)
    try:
        run.run('pull', '--platform', 'linux/amd64', IMAGES[service], timeout=600)
        run.run('pull', '--platform', 'linux/amd64', CLIENT_IMAGE, timeout=600)
        run.run('network', 'create', '--internal', run.network)
        method = 'rabbitmq' if service == 'rabbitmq-server' else service
        proof = (run.elasticsearch(transport) if transport is not None
                 else getattr(run, method)())
    finally:
        run.cleanup()
    return {'app': service, 'image': IMAGES[service], 'native_fixture_recovery': True,
            'producer_removed_before_restore': True, 'owned_fixture_resources_removed': True,
            'encrypted_transport_qualified': False, 'production_recovery_accepted': False,
            'retention_policy_qualified': False, **proof}
