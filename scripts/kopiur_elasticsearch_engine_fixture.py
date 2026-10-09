"""Concrete escrow capture and native recovery for disposable ARC Elasticsearch.

Only synthetic bytes enter the already dedicated Kopia identities. Each provider
boots a separate engine from its restored config, keystore, runtime and snapshot.
No original export, production isolation or production admission is qualified.
"""
import copy
import io
import json
import tarfile

from kopiur_elasticsearch_capture import SnapshotCapture, credentials_from_bytes
from kopiur_elasticsearch_source import LoopbackSnapshotIO
from kopiur_elasticsearch_restore import RestorePlan
from kopiur_elasticsearch_escrow import CHECKS, EscrowError, _encoded, _same_binding, restore_parts
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


def engine_lifetime(actual):
    """Bind one running process, not merely a reusable Docker container ID."""
    try:
        state = actual['State']
        if (state['Running'] is not True or type(state['Pid']) is not int or state['Pid'] <= 0
                or not isinstance(state['StartedAt'], str) or not state['StartedAt']
                or type(actual['RestartCount']) is not int or actual['RestartCount'] < 0):
            raise ValueError('running lifetime required')
        return state['StartedAt'], state['Pid'], actual['RestartCount']
    except (KeyError, TypeError, ValueError):
        raise EscrowError('owned synthetic process lifetime incomplete') from None


def guarded_engine_read(drill, source_id, guard, *command, data=None):
    if guard() is not True:
        raise EscrowError('owned synthetic process authority required')
    result = drill.run('exec', '-i', source_id, *command, data=data)
    if guard() is not True:
        raise EscrowError('owned synthetic process authority required')
    return result


def capture_engine(drill, source, variables, credentials, *, snapshot_uuid, snapshot_version):
    """Capture the real owned snapshot with the authenticated production adapter."""
    source_id = drill.registered_id(source)
    image = IMAGES['elasticsearch']
    version = drill.http(9200, '/')['version']['number']
    lifetime = engine_lifetime(json.loads(drill.run('inspect', '--format', '{{json .}}', source_id)))

    def guard():
        drill.check_dispatch()
        actual = json.loads(drill.run('inspect', '--format', '{{json .}}', source_id))
        settings = drill.http(9200, '/fixture/_settings?flat_settings=true')
        if (actual['Id'] != source_id or actual['Name'] != '/' + source
                or actual['Config']['Image'] != image or engine_lifetime(actual) != lifetime
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
    def bound_exec(*command, data=None):
        return guarded_engine_read(drill, source_id, guard, *command, data=data)

    io_adapter = LoopbackSnapshotIO(exec_read=bound_exec, repository='fixture',
                                    snapshot='generation', location=DATA_PATH + '/snapshot')
    adapter = SnapshotCapture(binding, guard=guard,
        read_credentials=lambda: _encoded({'elastic_username': 'elastic',
                                           'elastic_password': credentials['elastic']}),
        request=io_adapter.request, read_archive=io_adapter.read_archive,
        repository='fixture', snapshot='generation', location=DATA_PATH + '/snapshot',
        indices=['fixture'], expected_uuid=snapshot_uuid, snapshot_version=snapshot_version)
    from kopiur_elasticsearch_catalog import LoopbackCatalogIO, SourceCatalog
    contract = fixture_consumer_plan()
    catalog_io = LoopbackCatalogIO(exec_read=bound_exec, indices=['fixture'])
    catalog = SourceCatalog(binding, ledger=contract['ledger'], backend=contract['backend'],
        consumer_indices=contract['consumers'], indices=['fixture'], guard=guard,
        request=catalog_io.request)
    authenticated = credentials_from_bytes(adapter.credentials(binding)['data'])
    binding['source_catalog'] = catalog.capture(authenticated)
    # Rebuild the authenticated capture with the now complete escrow binding.
    adapter.binding = copy.deepcopy(binding)
    revocations = prove_capture_revocation(adapter, binding)
    native = adapter.native(binding)
    credential_part = adapter.credentials(binding)
    authenticated = credentials_from_bytes(credential_part['data'])
    if authenticated['elastic_password'] != credentials['elastic']:
        raise EscrowError('authenticated synthetic credential generation differs')
    # Keep the exact-service capture schema through the encrypted bundle. The
    # synthetic reader password is a separate verifier input, never an original
    # credential component or a replacement for the captured elastic identity.
    parts = configuration_parts(binding, archive, native=native,
                                runtime=part(_encoded(runtime)), credentials=credential_part)
    if _encoded(catalog.observe(authenticated)) != _encoded(binding['source_catalog']):
        raise EscrowError('source catalog changed across native/configuration capture')
    guard()
    return binding, parts, {'authenticated_snapshot_capture_verified': True,
                            'source_catalog_captured': True,
                            'concrete_bound_loopback_capture_verified': True,
                            'engine_version': version, 'snapshot_format_version': adapter.snapshot_version,
                            'captured_snapshot_uuid': snapshot_uuid,
                            'revoked_capture_denied': revocations,
                            'production_capture_accepted': False}


def fixture_consumer_plan():
    """Synthetic reader contract, never a substitute for the original ledger."""
    store = 'elasticsearch-indices:fixture/reader'
    app = 'fixture/reader'
    return {'ledger': {'applications': [{'id': app, 'state_dependencies': [store]}],
                       'physical_stores': [{'id': store, 'kind': 'external_elasticsearch_indices',
                           'backend_contract': 'database/elasticsearch', 'consumer_contracts': [app]}]},
            'backend': 'database/elasticsearch',
            'consumers': [{'application': app, 'store': store, 'indices': ['fixture']}]}


class EngineRestore:
    """Own one immutable stopped target, configure before its first start."""
    def __init__(self, drill, backend, manifest, parts, inventory, expected, *, consumer_credentials):
        self.drill, self.manifest, self.parts = drill, manifest, parts
        self.inventory, self.expected = inventory, expected
        self.binding = manifest['binding']
        runtime = json.loads(parts['runtime']['data'])
        self.credentials = credentials_from_bytes(parts['credentials']['data'])
        if (set(runtime) != {'image', 'version', 'variables'}
                or runtime['image'] != IMAGES['elasticsearch']
                or runtime['version'] != self.binding['runtime_version']
                or not isinstance(runtime['variables'], dict)
                or 'ELASTIC_PASSWORD' in runtime['variables']):
            raise EscrowError('synthetic escrow runtime invalid')
        if (not isinstance(consumer_credentials, dict)
                or set(consumer_credentials) != {'username', 'password'}
                or consumer_credentials['username'] != 'fixture-reader'
                or not isinstance(consumer_credentials['password'], str)
                or not consumer_credentials['password']
                or any(ord(c) < 32 or ord(c) == 127 for c in consumer_credentials['password'])
                or consumer_credentials['password'] == self.credentials['elastic_password']):
            raise EscrowError('separate synthetic consumer credentials required')
        self.consumer_credentials = copy.deepcopy(consumer_credentials)
        # Only this fixture caller chooses synthetic runtime dispositions.
        # Original recovery callers must review every captured variable.
        self.plan = RestorePlan(manifest, parts,
            replay=['discovery.type', 'xpack.security.enabled', 'xpack.security.http.ssl.enabled',
                    'xpack.ml.enabled', 'ingest.geoip.downloader.enabled', 'ES_JAVA_OPTS', 'path.repo'],
            omit={}, repository='fixture', snapshot='generation',
            location=DATA_PATH + '/snapshot', indices=['fixture'], **fixture_consumer_plan())
        self.runtime = self.plan.runtime
        self.name, _ = drill.create('restore-' + backend, 9200, runtime['variables'], extra=EXTRA, start=False)
        self.target = {'uid': drill.registered_id(self.name), 'isolated': True}
        self.shards = None

    def authority(self, target, binding):
        self.drill.check_dispatch()
        if target != self.target or not _same_binding(binding, self.binding):
            return False
        actual = json.loads(self.drill.run('inspect', '--format', '{{json .}}', target['uid']))
        network = json.loads(self.drill.run('network', 'inspect', self.drill.network))[0]
        return (actual['Id'] == target['uid'] and actual['Name'] == '/' + self.name
                and actual['Config']['Image'] == self.binding['engine_image']
                and actual['HostConfig']['NetworkMode'] == self.drill.network
                and not actual['HostConfig'].get('PortBindings') and network['Internal'] is True
                and set(actual['NetworkSettings']['Networks']) <= {self.drill.network})

    def configuration(self, target, binding, config):
        self.plan.check(self.manifest, self.parts)
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
        self.plan.check(manifest, self.parts)
        self.drill.start_registered(self.name)
        self.drill.auth = self.credentials['elastic_username'] + ':' + self.credentials['elastic_password']
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
        self.plan.check(self.manifest, self.parts)
        if part != self.parts['native']:
            raise EscrowError('engine native input differs')
        self.drill.run('cp', '-a', '-', target['uid'] + ':' + DATA_PATH,
                       data=repository_envelope(part['data']))
        selection = self.plan.selection
        path = '/_snapshot/' + selection['repository']
        self.drill.http(9200, path, 'PUT',
                        {'type': 'fs', 'settings': {'location': selection['location'], 'readonly': True}})
        result = self.drill.http(9200, path + '/' + selection['snapshot']
                                + '/_restore?wait_for_completion=true', 'POST', self.plan.native_request())
        self.shards = validate_elasticsearch_restore(result)
        return True

    def verify(self, target, manifest):
        self.drill.ready(self.name, lambda: self.drill.http(9200, '/_cluster/health/fixture')['status'] == 'green')
        self.check_config(target)
        if 'source_catalog' in self.binding:
            from kopiur_elasticsearch_catalog import LoopbackCatalogIO, SourceCatalog
            def guarded_read(*command, data=None):
                return guarded_engine_read(self.drill, target['uid'],
                    lambda: self.authority(target, self.binding), *command, data=data)
            io_adapter = LoopbackCatalogIO(exec_read=guarded_read, indices=self.plan.selection['indices'])
            contract = fixture_consumer_plan()
            catalog = SourceCatalog(self.binding, ledger=contract['ledger'], backend=contract['backend'],
                consumer_indices=contract['consumers'], indices=self.plan.selection['indices'],
                guard=lambda: self.authority(target, self.binding), request=io_adapter.request)
            self.catalog_receipt = catalog.verify_restore(self.binding['source_catalog'], self.credentials)
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
        self.drill.auth = self.consumer_credentials['username'] + ':' + self.consumer_credentials['password']
        identity = self.drill.http(9200, '/_security/_authenticate')
        hits = self.drill.http(9200, '/fixture/_search?sort=number&size=10')['hits']['hits']
        if (identity.get('username') != 'fixture-reader' or identity.get('roles') != ['fixture-reader']
                or [{'_id': h['_id'], '_source': h['_source']} for h in hits] != self.expected[0]):
            raise EscrowError('restored consumer authentication or query differs')
        self.drill.auth = self.credentials['elastic_username'] + ':' + self.credentials['elastic_password']
        return {k: True for k in CHECKS}

    def exercise(self):
        proof = restore_parts(self.manifest, self.parts, target=self.target,
                              require_isolated_authority=self.authority,
                              restore_configuration=self.configuration, verify_configuration=self.prerequisites,
                              restore_native=self.native, verify=self.verify)
        proof['restored_shards'] = self.shards
        proof['restored_bootstrap_keystore_used'] = True
        proof['original_credential_schema_verified'] = True
        proof['synthetic_consumer_credentials_separate'] = True
        proof['prepared_restore_plan'] = self.plan.receipt()
        if 'source_catalog' in self.binding:
            proof['source_catalog'] = self.catalog_receipt
        proof['target_retirement'] = self.drill.remove(self.name)
        return proof


def provider_recovery(drill, provider, binding, parts, inventory, expected, *, consumer_credentials):
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
        engine = EngineRestore(drill, backend, manifest, restored, inventory, expected,
                               consumer_credentials=consumer_credentials)
        proofs[backend] = engine.exercise()
    receipts['r2']['source_nas_snapshot_id'] = receipts['nas']['snapshot_id']
    return {'provider': receipts, 'engines': proofs, 'manifest_sha256': proofs['nas']['manifest_sha256'],
            'independent_provider_engine_restores': 2, 'synthetic_escrow_engine_verified': True,
            'production_acceptance': False, 'source_admission_released': False}
