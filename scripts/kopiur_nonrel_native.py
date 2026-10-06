"""Credential-free, disposable ARC fixtures for shared non-relational stores.

Only native engine recovery is qualified. No production, NAS/R2, retention,
stream-consumer, security-identity or coherent multi-application claim is made.
"""
import base64
import hashlib
import io
import inspect
import json
import os
import secrets
import socket
import subprocess
import sys
import tarfile
import time
from typing import Any
import urllib.error
import urllib.request
import uuid

IMAGES = {
    'dragonfly': 'ghcr.io/dragonflydb/dragonfly:v2.0.0@sha256:7426fdb31ddcf7bd9499b4205f36ebaa83b26149ba1609a0d5f8f474b3631233',
    'elasticsearch': 'docker.elastic.co/elasticsearch/elasticsearch:8.19.22@sha256:e98f9c3b09beb2fbb9eaf667d602df3f0e00bd3644138b8458dc17ba1a675595',
    'rabbitmq-server': 'docker.io/library/rabbitmq:4.2.6-management@sha256:3ab808deef2f6552bc10ede59bdba1437b0d5c778e29d8927c5afbfa2586c22f',
}
MAX_ARCHIVE = 192 * 1024 * 1024
CLIENT_IMAGE = 'docker.io/library/python:3.14.7-slim@sha256:51dafde81dbdb6ebde285137a295cf18a47ca95234fe388a343719cb97305b3d'


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


class Fixture:
    def __init__(self, service):
        if service not in IMAGES:
            raise ValueError('unknown shared-store fixture')
        if not os.environ.get('RUNNER_NAME', '').startswith('ghar-set-zoo-') or sys.platform != 'linux':
            raise RuntimeError('native fixtures require the owned Linux ARC runner')
        self.service = service
        self.prefix = 'k8s92-nonrel-' + uuid.uuid4().hex
        self.network = self.prefix + '-net'
        self.containers = []
        self.deadline = time.monotonic() + 1200
        self.auth = None
        self.client = None
        self.host = None

    def run(self, *args, data=None, timeout=180):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeError('native fixture deadline exceeded')
        result = subprocess.run(['docker', *args], input=data, capture_output=True,
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
        command = ['docker', 'create', '--name', name, '--network', self.network,
                   *extra, *options,
                   IMAGES[self.service], *args]
        # Track exact names even if create partly succeeds, cleanup never prunes.
        self.containers.append(name)
        result = subprocess.run(command, env=env, capture_output=True, timeout=60)
        if result.returncode:
            raise RuntimeError('native fixture create failed')
        if archive is not None:
            validate_archive(archive)
            self.run('cp', '-a', '-', name + ':' + path, data=archive)
        self.run('start', name)
        self.host = name
        return name, port

    def remove(self, name):
        self.run('rm', '-fv', name)
        self.containers.remove(name)

    def cleanup(self):
        for name in self.containers:
            subprocess.run(['docker', 'rm', '-fv', name], capture_output=True, timeout=60, check=True)
        self.containers.clear()
        subprocess.run(['docker', 'network', 'rm', self.network], capture_output=True, timeout=60, check=True)

    def ready(self, name, check):
        deadline = min(self.deadline, time.monotonic() + 240)
        while time.monotonic() < deadline:
            state = json.loads(self.run('inspect', '--format', '{{json .State}}', name))
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
            self.run('run', '-d', '--name', self.client, '--network', self.network,
                     '--memory=128m', CLIENT_IMAGE, 'python', '-c', 'import time;time.sleep(1200)')
        code = ('import sys,json,base64,socket,urllib.request\nfrom typing import Any\n'
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
    with urllib.request.urlopen(request,timeout=60) as response:
        raw=response.read(8*1024*1024+1)
        if len(raw)>8*1024*1024: raise ValueError('response bound')
        result=json.loads(raw) if raw else {}
print(json.dumps(result,default=lambda x:{'__binary__':base64.b64encode(x).decode()}))
''')
        wire = json.dumps(payload | {'host': self.host}).encode()
        result = self.run('exec', '-i', self.client, 'python', '-c', code, data=wire, timeout=90)
        return json.loads(result, object_hook=lambda item: base64.b64decode(item['__binary__'])
                          if set(item) == {'__binary__'} else item)

    def redis(self, port, *args):
        items = [x if isinstance(x, bytes) else str(x).encode() for x in args]
        return self.client_request({'operation': 'redis', 'port': port,
                                    'args': [base64.b64encode(x).decode() for x in items]})

    def http(self, port, path, method='GET', body=None) -> Any:
        return self.client_request({'operation': 'http', 'port': port, 'path': path,
                                    'method': method, 'body': body, 'auth': self.auth})

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

    def elasticsearch(self):
        variables = {'discovery.type': 'single-node', 'xpack.security.enabled': 'false',
                     'xpack.ml.enabled': 'false', 'ingest.geoip.downloader.enabled': 'false',
                     'ES_JAVA_OPTS': '-Xms256m -Xmx256m -XX:ActiveProcessorCount=2',
                     'path.repo': '/usr/share/elasticsearch/data/snapshot'}
        source, port = self.create('source', 9200, variables, extra=('--memory=1600m',))
        self.ready(source, lambda: self.http(port, '/_cluster/health')['status'] in ('yellow', 'green'))
        mappings = {'properties': {'title': {'type': 'keyword'}, 'number': {'type': 'integer'}}}
        self.http(port, '/fixture', 'PUT', {'settings': {'number_of_shards': 1, 'number_of_replicas': 0},
                                          'mappings': mappings, 'aliases': {'fixture-alias': {}}})
        for number in (1, 2):
            self.http(port, '/fixture/_doc/' + str(number) + '?refresh=true', 'PUT',
                      {'title': 'fixture-' + str(number), 'number': number})
        self.http(port, '/_index_template/fixture-template', 'PUT',
                  {'index_patterns': ['fixture-*'], 'template': {'mappings': mappings}})
        self.http(port, '/_ingest/pipeline/fixture-pipeline', 'PUT',
                  {'processors': [{'set': {'field': 'fixture', 'value': True}}]})
        reads = ('/fixture/_search?sort=number&size=10', '/fixture/_mapping',
                 '/fixture/_alias', '/_index_template/fixture-template', '/_ingest/pipeline/fixture-pipeline')
        def inventory(listener):
            values = [self.http(listener, path) for path in reads]
            values[0] = [{'_id': hit['_id'], '_source': hit['_source']} for hit in values[0]['hits']['hits']]
            return values
        expected = inventory(port)
        repo = {'type': 'fs', 'settings': {'location': '/usr/share/elasticsearch/data/snapshot'}}
        self.http(port, '/_snapshot/fixture', 'PUT', repo)
        result = self.http(port, '/_snapshot/fixture/generation?wait_for_completion=true', 'PUT',
                           {'indices': 'fixture', 'include_global_state': True})['snapshot']
        if result['state'] != 'SUCCESS' or result['shards']['failed'] != 0 or result.get('failures'):
            raise RuntimeError('Elasticsearch native snapshot is partial')
        archive = self.run('cp', source + ':/usr/share/elasticsearch/data/snapshot/.', '-')
        digest = validate_archive(archive)
        self.remove(source)
        restored, port = self.create('restore', 9200, variables, extra=('--memory=1600m',),
                                     archive=repository_envelope(archive), path='/usr/share/elasticsearch/data')
        self.ready(restored, lambda: self.http(port, '/_cluster/health')['status'] in ('yellow', 'green'))
        self.http(port, '/_snapshot/fixture', 'PUT', {'type': 'fs', 'settings': repo['settings'] | {'readonly': True}})
        self.http(port, '/_snapshot/fixture/generation/_restore?wait_for_completion=true', 'POST',
                  {'indices': 'fixture', 'include_global_state': True})
        if inventory(port) != expected:
            raise RuntimeError('Elasticsearch documents or metadata differ')
        return {'snapshot_sha256': digest, 'snapshot_uuid': result['uuid'],
                'documents_mappings_aliases_templates_pipelines_equal': True,
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


def fixture(service):
    run = Fixture(service)
    run.run('pull', '--platform', 'linux/amd64', IMAGES[service], timeout=600)
    run.run('pull', '--platform', 'linux/amd64', CLIENT_IMAGE, timeout=600)
    run.run('network', 'create', '--internal', run.network)
    try:
        method = 'rabbitmq' if service == 'rabbitmq-server' else service
        proof = getattr(run, method)()
    finally:
        run.cleanup()
    return {'app': service, 'image': IMAGES[service], 'native_fixture_recovery': True,
            'producer_removed_before_restore': True, 'owned_fixture_resources_removed': True,
            'encrypted_transport_qualified': False, 'production_recovery_accepted': False,
            'retention_policy_qualified': False, **proof}
