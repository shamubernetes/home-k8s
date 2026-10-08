"""Concrete escrow capture and native recovery for disposable ARC Elasticsearch.

Only synthetic bytes enter the already dedicated Kopia identities. Each provider
boots a separate engine from its restored config, keystore, runtime and snapshot.
No original export, production isolation or production admission is qualified.
"""
import copy
import io
import json
import tarfile

from kopiur_elasticsearch_escrow import CHECKS, EscrowError, _encoded, restore_parts
from kopiur_elasticsearch_escrow_fixture import configuration_archive, configuration_parts
from kopiur_elasticsearch_escrow_transport import encode_bundle, decode_bundle
from kopiur_nonrel_native import IMAGES, repository_envelope, validate_elasticsearch_restore


CONFIG_PATH = '/usr/share/elasticsearch/config'
DATA_PATH = '/usr/share/elasticsearch/data'
EXTRA = ('--memory=1600m', '--user=elasticsearch')


def capture_engine(drill, source, native, variables, credentials):
    """Observe the owned, write-blocked engine around the complete config read."""
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
        directories = sorted(member.name.removeprefix('./').rstrip('/') for member in members
                             if member.isdir() and member.name not in ('.', './'))
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
    parts = configuration_parts(binding, archive, native=part(native),
                                runtime=part(_encoded(runtime)), credentials=part(_encoded(credentials)))
    guard()
    return binding, parts


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
        self.drill.run('cp', '-a', '-', target['uid'] + ':' + CONFIG_PATH, data=archive)
        self.check_config(target)
        return True

    def check_config(self, target):
        archive = self.drill.run('cp', target['uid'] + ':' + CONFIG_PATH + '/.', '-')
        restored = configuration_parts(self.binding, archive, **{
            k: self.parts[k] for k in ('native', 'runtime', 'credentials')})
        if restored != self.parts:
            raise EscrowError('engine configuration bytes or metadata differ')

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
