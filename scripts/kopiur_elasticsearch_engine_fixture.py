"""Concrete escrow capture and native recovery for disposable ARC Elasticsearch.

Only synthetic bytes enter the already dedicated Kopia identities. Each provider
boots a separate engine from its restored config, keystore, runtime and snapshot.
No original export, production isolation or production admission is qualified.
"""
import copy
import io
import json
import tarfile

from kopiur_elasticsearch_capture import SnapshotCapture
from kopiur_elasticsearch_escrow import CHECKS, EscrowError, _encoded, restore_parts
from kopiur_elasticsearch_escrow_fixture import configuration_archive, configuration_parts
from kopiur_elasticsearch_escrow_transport import encode_bundle, decode_bundle
from kopiur_nonrel_native import IMAGES, repository_envelope, validate_elasticsearch_restore


CONFIG_PATH = '/usr/share/elasticsearch/config'
DATA_PATH = '/usr/share/elasticsearch/data'
EXTRA = ('--memory=1600m', '--user=elasticsearch')


def prove_capture_revocation(adapter, binding):
    """Deny capture before I/O, after authentication and during archive read.

    All successful operations still use the live engine adapter. These injected
    authority losses affect only this synthetic attempt, never source admission.
    """
    denied = []
    for point in ('before-read', 'after-authentication', 'after-archive-read'):
        active = True
        archive_reads = 0
        events = []

        def io_event(name):
            if not active:
                raise EscrowError('source I/O attempted after synthetic authority revocation')
            events.append(name)

        def read_credentials():
            io_event('credentials')
            return adapter.read_credentials()

        def guard():
            return active and adapter.guard() is True

        def request(path, credentials):
            nonlocal active
            io_event(path)
            value = adapter.request(path, credentials)
            if point == 'after-authentication' and path == '/_security/_authenticate':
                active = False
            return value

        def read_archive(location):
            nonlocal active, archive_reads
            io_event('archive')
            archive_reads += 1
            value = adapter.read_archive(location)
            if point == 'after-archive-read':
                active = False
            return value

        attempted = SnapshotCapture(binding, guard=guard, read_credentials=read_credentials,
            request=request, read_archive=read_archive, repository=adapter.repository,
            snapshot=adapter.snapshot, location=adapter.location, indices=sorted(adapter.indices),
            expected_uuid=adapter.expected_uuid, snapshot_version=adapter.snapshot_version)
        if point == 'before-read':
            active = False
        try:
            attempted.native(binding)
        except EscrowError as error:
            # A partial snapshot, failed request or invalid fixture is not proof
            # of authority-loss handling. Require the exact revoked guard error.
            if str(error) != 'affirmative live capture fence required':
                raise
        else:
            raise EscrowError('revoked synthetic capture returned source bytes')
        expected_events = {'before-read': [],
            'after-authentication': ['credentials', '/_security/_authenticate'],
            'after-archive-read': ['credentials', '/_security/_authenticate', '/',
                '/_snapshot/' + adapter.repository,
                '/_snapshot/' + adapter.repository + '/' + adapter.snapshot, 'archive']}
        if events != expected_events[point]:
            raise EscrowError('revoked capture I/O sequence differs')
        if archive_reads != (1 if point == 'after-archive-read' else 0):
            raise EscrowError('revoked synthetic capture crossed the archive boundary')
        denied.append(point)
    return denied


def capture_engine(drill, source, variables, credentials, *, snapshot_uuid, snapshot_version):
    """Capture the real owned snapshot with the authenticated production adapter."""
    source_id = drill.registered_id(source)
    image = IMAGES['elasticsearch']
    version = drill.http(9200, '/')['version']['number']

    def guard():
        drill.check_dispatch()
        actual = json.loads(drill.run('inspect', '--format', '{{json .}}', source_id))
        settings = drill.http(9200, '/fixture/_settings?flat_settings=true')
        if (actual['Id'] != source_id or actual['Name'] != '/' + source
                or actual['Config']['Image'] != image or not actual['State']['Running']
                or settings['fixture']['settings'].get('index.blocks.write') != 'true'
                or drill.http(9200, '/')['version']['number'] != version):
            raise EscrowError('owned synthetic capture binding changed')
        environment = dict(value.split('=', 1) for value in actual['Config']['Env'])
        if any(environment.get(key) != value for key, value in variables.items()):
            raise EscrowError('owned synthetic runtime changed')
        return True

    guard()
    archive = drill.run('cp', source_id + ':' + CONFIG_PATH + '/.', '-')
    guard()
    with tarfile.open(fileobj=io.BytesIO(archive), mode='r:') as files:
        members = list(files)
        paths = sorted(member.name.removeprefix('./') for member in members if member.isfile())
        directories = sorted(member.name.removeprefix('./').rstrip('/') or '.'
                             for member in members if member.isdir())
    binding = {'generation': drill.prefix.removeprefix('k8s92-nonrel-'),
               'source_uid': source_id, 'source_pod_uid': source_id,
               'engine_image': image, 'runtime_version': version,
               'credential_versions': {'owned-synthetic': drill.prefix}, 'config_paths': paths,
               'config_directories': directories}
    def part(data):
        return {'binding': copy.deepcopy(binding), 'data': data, 'mode': 0o600, 'uid': 1000, 'gid': 0}
    # Store exactly the engine environment observed above. ELASTIC_PASSWORD is
    # absent during recovery: restored bootstrap.password must authenticate it.
    runtime = {'image': image, 'version': version,
               'variables': {k: v for k, v in variables.items() if k != 'ELASTIC_PASSWORD'}}
    def request(path, value):
        # Authenticate with the exact credential bytes given to SnapshotCapture,
        # rather than implicitly borrowing whichever auth the fixture last used.
        prior = drill.auth
        try:
            drill.auth = value['elastic_username'] + ':' + value['elastic_password']
            return drill.http(9200, path)
        finally:
            drill.auth = prior

    adapter = SnapshotCapture(binding, guard=guard,
        read_credentials=lambda: _encoded({'elastic_username': 'elastic',
                                           'elastic_password': credentials['elastic']}),
        request=request,
        read_archive=lambda location: drill.run('cp', source_id + ':' + location + '/.', '-'),
        repository='fixture', snapshot='generation', location=DATA_PATH + '/snapshot',
        indices=['fixture'], expected_uuid=snapshot_uuid, snapshot_version=snapshot_version)
    revocations = prove_capture_revocation(adapter, binding)
    native = adapter.native(binding)
    authenticated = json.loads(adapter.credentials(binding)['data'])
    if authenticated['elastic_password'] != credentials['elastic']:
        raise EscrowError('authenticated synthetic credential generation differs')
    parts = configuration_parts(binding, archive, native=native,
                                runtime=part(_encoded(runtime)), credentials=part(_encoded(credentials)))
    guard()
    return binding, parts, {'authenticated_snapshot_capture_verified': True,
                            'engine_version': version, 'snapshot_format_version': adapter.snapshot_version,
                            'captured_snapshot_uuid': snapshot_uuid,
                            'revoked_capture_denied': revocations,
                            'production_capture_accepted': False}


class EngineRestore:
    """Own one immutable stopped target, configure before its first start."""
    def __init__(self, drill, backend, manifest, parts, inventory, expected):
        self.drill, self.manifest, self.parts = drill, manifest, parts
        self.inventory, self.expected = inventory, expected
        self.binding = manifest['binding']
        runtime = json.loads(parts['runtime']['data'])
        self.credentials = json.loads(parts['credentials']['data'])
        if (set(runtime) != {'image', 'version', 'variables'}
                or runtime['image'] != IMAGES['elasticsearch']
                or runtime['version'] != self.binding['runtime_version']
                or not isinstance(runtime['variables'], dict)
                or set(self.credentials) != {'elastic', 'fixture-reader'}
                or any(not isinstance(v, str) or not v for v in self.credentials.values())
                or 'ELASTIC_PASSWORD' in runtime['variables']):
            raise EscrowError('synthetic escrow runtime or credentials invalid')
        self.runtime = runtime
        self.name, _ = drill.create('restore-' + backend, 9200, runtime['variables'], extra=EXTRA, start=False)
        self.target = {'uid': drill.registered_id(self.name), 'isolated': True}
        self.shards = None

    def authority(self, target, binding):
        self.drill.check_dispatch()
        if target != self.target or binding != self.binding:
            return False
        actual = json.loads(self.drill.run('inspect', '--format', '{{json .}}', target['uid']))
        network = json.loads(self.drill.run('network', 'inspect', self.drill.network))[0]
        return (actual['Id'] == target['uid'] and actual['Name'] == '/' + self.name
                and actual['Config']['Image'] == self.binding['engine_image']
                and actual['HostConfig']['NetworkMode'] == self.drill.network
                and not actual['HostConfig'].get('PortBindings') and network['Internal'] is True
                and set(actual['NetworkSettings']['Networks']) <= {self.drill.network})

    def configuration(self, target, binding, config):
        if config != {k: v for k, v in self.parts.items() if k != 'native'}:
            raise EscrowError('engine configuration inputs differ')
        archive = configuration_archive(binding, config)
        # Stdin tar extraction with Docker's -a rewrites every member to the
        # container user. Omit it to preserve numeric owners from our archive.
        # check_config verifies bytes, modes and UID/GID before the engine boots.
        self.drill.run('cp', '-', target['uid'] + ':' + CONFIG_PATH, data=archive)
        self.check_config(target)
        return True

    def check_config(self, target):
        archive = self.drill.run('cp', target['uid'] + ':' + CONFIG_PATH + '/.', '-')
        restored = configuration_parts(self.binding, archive, **{
            k: self.parts[k] for k in ('native', 'runtime', 'credentials')})
        if restored != self.parts:
            differences = {
                name: {'bytes_equal': restored[name]['data'] == part['data'],
                       'expected_metadata': [part[k] for k in ('mode', 'uid', 'gid')],
                       'actual_metadata': [restored[name][k] for k in ('mode', 'uid', 'gid')]}
                for name, part in self.parts.items() if restored[name] != part
            }
            raise EscrowError('engine configuration differs: ' + json.dumps(differences, sort_keys=True))

    def prerequisites(self, target, manifest):
        self.drill.start_registered(self.name)
        self.drill.auth = 'elastic:' + self.credentials['elastic']
        self.drill.ready(self.name, lambda: self.drill.http(9200, '/_cluster/health')['status'] in ('yellow', 'green'))
        self.check_config(target)
        keys = self.drill.run('exec', target['uid'], '/usr/share/elasticsearch/bin/elasticsearch-keystore', 'list')
        identity = self.drill.http(9200, '/_security/_authenticate')
        actual = json.loads(self.drill.run('inspect', '--format', '{{json .Config}}', target['uid']))
        env = dict(v.split('=', 1) for v in actual['Env'])
        if (b'bootstrap.password' not in keys.splitlines() or identity.get('username') != 'elastic'
                or self.drill.http(9200, '/')['version']['number'] != self.runtime['version']
                or actual['Image'] != self.runtime['image'] or 'ELASTIC_PASSWORD' in env
                or any(env.get(k) != v for k, v in self.runtime['variables'].items())):
            raise EscrowError('restored keystore authentication or runtime differs')
        return {k: True for k in ('config_metadata', 'keystore_load', 'credential_authentication', 'runtime_identity')}

    def native(self, target, binding, part):
        if part != self.parts['native']:
            raise EscrowError('engine native input differs')
        self.drill.run('cp', '-a', '-', target['uid'] + ':' + DATA_PATH,
                       data=repository_envelope(part['data']))
        self.drill.http(9200, '/_snapshot/fixture', 'PUT',
                        {'type': 'fs', 'settings': {'location': DATA_PATH + '/snapshot', 'readonly': True}})
        result = self.drill.http(9200, '/_snapshot/fixture/generation/_restore?wait_for_completion=true', 'POST',
                                {'indices': 'fixture', 'include_global_state': True, 'feature_states': ['security']})
        self.shards = validate_elasticsearch_restore(result)
        return True

    def verify(self, target, manifest):
        self.drill.ready(self.name, lambda: self.drill.http(9200, '/_cluster/health/fixture')['status'] == 'green')
        self.check_config(target)
        settings = self.drill.http(9200, '/fixture/_settings?flat_settings=true')
        if settings['fixture']['settings'].get('index.blocks.write') != 'true':
            raise EscrowError('restored synthetic writer fence absent')
        # Only the disposable target is reopened. The original source is gone.
        if self.drill.http(9200, '/fixture/_settings', 'PUT', {'index.blocks.write': None}).get('acknowledged') is not True:
            raise EscrowError('restored synthetic target writer fence not released')
        if self.inventory(9200) != self.expected:
            raise EscrowError('restored documents or metadata differ')
        role = self.drill.http(9200, '/_security/role/fixture-reader').get('fixture-reader', {})
        if (role.get('cluster') != [] or len(role.get('indices', [])) != 1
                or role['indices'][0].get('names') != ['fixture']
                or role['indices'][0].get('privileges') != ['read']):
            raise EscrowError('restored native security role differs')
        self.drill.auth = 'fixture-reader:' + self.credentials['fixture-reader']
        identity = self.drill.http(9200, '/_security/_authenticate')
        hits = self.drill.http(9200, '/fixture/_search?sort=number&size=10')['hits']['hits']
        if (identity.get('username') != 'fixture-reader' or identity.get('roles') != ['fixture-reader']
                or [{'_id': h['_id'], '_source': h['_source']} for h in hits] != self.expected[0]):
            raise EscrowError('restored consumer authentication or query differs')
        self.drill.auth = 'elastic:' + self.credentials['elastic']
        return {k: True for k in CHECKS}

    def exercise(self):
        proof = restore_parts(self.manifest, self.parts, target=self.target,
                              require_isolated_authority=self.authority,
                              restore_configuration=self.configuration, verify_configuration=self.prerequisites,
                              restore_native=self.native, verify=self.verify)
        proof['restored_shards'] = self.shards
        proof['restored_bootstrap_keystore_used'] = True
        proof['target_retirement'] = self.drill.remove(self.name)
        return proof


def provider_recovery(drill, provider, binding, parts, inventory, expected):
    """Boot independently from NAS and R2, never use captured originals at restore."""
    from kopiur_elasticsearch_provider_fixture import restore_archive, validate_identity
    if provider.app != 'elasticsearch':
        raise EscrowError('dedicated Elasticsearch provider required')
    validate_identity(provider.fields)
    bundle, manifest = encode_bundle(binding, parts)
    receipts, proofs = {}, {}
    # R2 gets only a fresh decrypted NAS restore, never a second source capture.
    data = bundle
    for backend in ('nas', 'r2'):
        data, receipts[backend] = restore_archive(provider, backend, data)
        restored = decode_bundle(data, manifest)
        engine = EngineRestore(drill, backend, manifest, restored, inventory, expected)
        proofs[backend] = engine.exercise()
    receipts['r2']['source_nas_snapshot_id'] = receipts['nas']['snapshot_id']
    return {'provider': receipts, 'engines': proofs, 'manifest_sha256': proofs['nas']['manifest_sha256'],
            'independent_provider_engine_restores': 2, 'synthetic_escrow_engine_verified': True,
            'production_acceptance': False, 'source_admission_released': False}
