"""Lifetime-bound capture for the original Kubernetes Elasticsearch source.

No CLI, default authority, production fence or export destination is provided.
Metadata observations do not authorize reading source configuration or keys.
An independently enforced capture fence and original-source export grant must
remain live around every operation. ARC fixtures qualify code, not that grant.
"""
import base64
import copy
import hmac
import json
import re
import subprocess

from kopiur_elasticsearch_capture import SnapshotCapture, credentials_from_bytes
from kopiur_elasticsearch_escrow import (
    EscrowError, _binding, _encoded, _same_binding, _validate_component, capture_export,
)
from kopiur_elasticsearch_escrow_fixture import configuration_parts


class KubernetesSource:
    """Bind Pod, container lifetime and ESO/target Secret versions before capture.

    read_version must observe the authenticated engine version inside the
    approved boundary. require_authority must check export authorization AND
    independent all-writer fencing. A current Pod is not a writer fence.
    """
    def __init__(self, binding, *, read_version, require_authority, run=None):
        self.binding = _binding(binding)
        lifetime = self.binding.get('source_lifetime', {})
        if (lifetime.get('namespace') != 'database'
                or lifetime.get('pod') != 'elasticsearch-0' or lifetime.get('container') != 'app'
                or not re.fullmatch(r'containerd://[0-9a-f]{64}', self.binding['source_uid'])
                or set(self.binding['credential_versions']) != {
                    'external-secret-uid', 'external-secret-resource-version', 'external-secret-synced-version',
                    'target-secret-uid', 'target-secret-resource-version'}):
            raise EscrowError('exact original Elasticsearch source/provider binding required')
        self.read_version, self.require_authority = read_version, require_authority
        self.run = run or self._run

    @staticmethod
    def _run(argv, *, data=None):
        try:
            result = subprocess.run(argv, input=data, capture_output=True, timeout=120, check=False)
        except (OSError, subprocess.TimeoutExpired):
            raise EscrowError('source read failed or timed out') from None
        if result.returncode:
            # Exec stderr and timeout buffers may contain source credentials.
            raise EscrowError('source read failed')
        return result.stdout

    def get(self, kind, name, *, metadata_only=False):
        output = 'jsonpath={.metadata}' if metadata_only else 'json'
        try:
            return json.loads(self.run(['kubectl', 'get', kind, name, '-n', 'database', '-o', output]))
        except (ValueError, TypeError, UnicodeError):
            raise EscrowError('source metadata invalid') from None

    def observe(self):
        """Read metadata only, never Secret data or source config/key bytes."""
        try:
            pod = self.get('pod', 'elasticsearch-0')
            provider = self.get('externalsecret', 'elasticsearch')
            secret = self.get('secret', 'elasticsearch-secret', metadata_only=True)
            meta = pod['metadata']
            app = next(c for c in pod['spec']['containers'] if c['name'] == 'app')
            status = next(c for c in pod['status']['containerStatuses'] if c['name'] == 'app')
            env = app['env']
            names = [e['name'] for e in env]
            if len(names) != len(set(names)):
                raise EscrowError('duplicate declared source environment names')
            password = next(e for e in env if e['name'] == 'ELASTIC_PASSWORD')
            provider_meta = provider['metadata']
            ready = [c for c in provider['status']['conditions'] if c['type'] == 'Ready']
            owners = [o for o in secret.get('ownerReferences', []) if o.get('controller') is True]
            if (any(m.get('deletionTimestamp') for m in (meta, provider_meta, secret))
                    or pod['status']['phase'] != 'Running' or status['ready'] is not True
                    or password.get('valueFrom') != {'secretKeyRef': {
                        'name': 'elasticsearch-secret', 'key': 'ELASTIC_PASSWORD'}}
                    or app.get('envFrom') or app.get('command') or app.get('args')
                    or len(ready) != 1 or ready[0]['status'] != 'True'
                    or provider['spec']['target']['name'] != 'elasticsearch-secret'
                    or provider['spec']['secretStoreRef'] != {
                        'kind': 'ClusterSecretStore', 'name': 'op-secret-store'}
                    or len(owners) != 1 or owners[0]['uid'] != provider_meta['uid']
                    or owners[0]['kind'] != 'ExternalSecret'):
                raise EscrowError('source/provider not ready or ownership differs')
            observed = copy.deepcopy(self.binding)
            observed.update(source_uid=status['containerID'], source_pod_uid=meta['uid'],
                            engine_image=app['image'], runtime_version=self.read_version())
            observed['source_lifetime'].update(runtime_image=status['imageID'],
                restart_count=status['restartCount'], started_at=status['state']['running']['startedAt'])
            observed['credential_versions'] = {
                'external-secret-uid': provider_meta['uid'],
                'external-secret-resource-version': provider_meta['resourceVersion'],
                'external-secret-synced-version': provider['status']['syncedResourceVersion'],
                'target-secret-uid': secret['uid'], 'target-secret-resource-version': secret['resourceVersion'],
            }
            return _binding(observed)
        except (KeyError, StopIteration, TypeError, ValueError):
            raise EscrowError('source lifetime/provider observation incomplete') from None

    def guard(self):
        expected = copy.deepcopy(self.binding)
        if self.require_authority(expected) is not True:
            raise EscrowError('original export authorization and independent capture fence required')
        if not _same_binding(self.observe(), self.binding):
            raise EscrowError('source lifetime or credential-provider version changed')
        return True

    def exec_read(self, *command, data=None):
        self.guard()
        result = self.run(['kubectl', 'exec', '-n', 'database', 'elasticsearch-0',
                           '-c', 'app', '--stdin', '--', *command], data=data)
        self.guard()
        return result

    def capture(self, binding, *, capture_native, capture_credentials):
        """Capture config and effective environment in memory under live authority.

        Native snapshots and exact-version credentials come from separately
        approved adapters, each returning the existing bound component format.
        Guard checks cannot cancel already accepted remote operations. The
        independent capture fence must provide that lifecycle protection.
        """
        if not _same_binding(binding, self.binding):
            raise EscrowError('capture binding differs')
        self.guard()
        native = capture_native(copy.deepcopy(binding))
        self.guard()
        _validate_component(binding, 'native', native)
        credentials = capture_credentials(copy.deepcopy(binding))
        self.guard()
        _validate_component(binding, 'credentials', credentials)
        raw = self.exec_read('cat', '/proc/1/environ')
        try:
            if not raw.endswith(b'\0'):
                raise ValueError('unterminated environment')
            variables = {}
            for entry in raw[:-1].split(b'\0'):
                name, value = entry.decode().split('=', 1)
                if not name or name in variables:
                    raise ValueError('invalid environment')
                variables[name] = value
        except (ValueError, UnicodeError, TypeError):
            raise EscrowError('effective source runtime environment invalid') from None
        if variables.get('ES_PATH_CONF', '/usr/share/elasticsearch/config') != '/usr/share/elasticsearch/config':
            raise EscrowError('alternate active source configuration root not admitted')
        try:
            captured_credentials = credentials_from_bytes(credentials['data'])
            password = captured_credentials['elastic_password']
            if not hmac.compare_digest(password.encode(), variables['ELASTIC_PASSWORD'].encode()):
                raise EscrowError('provider credentials differ from source runtime generation')
        except EscrowError:
            raise
        except (KeyError, ValueError, TypeError, UnicodeError):
            raise EscrowError('provider/source credential coherence incomplete') from None
        # Recovery loads the captured keystore, never resets bootstrap.password.
        variables.pop('ELASTIC_PASSWORD')
        runtime = {'binding': copy.deepcopy(binding), 'mode': 0o600, 'uid': 1000, 'gid': 1000,
                   'data': _encoded({'image': binding['engine_image'], 'version': binding['runtime_version'],
                                     'variables': variables})}
        archive = self.exec_read('tar', '-C', '/usr/share/elasticsearch/config', '-cf', '-', '.')
        parts = configuration_parts(binding, archive, native=native, runtime=runtime, credentials=credentials)
        self.guard()
        return parts

    def export(self, *, capture_native, capture_credentials, encrypt_export):
        return capture_export(self.binding, observe=self.observe,
            require_capture_authority=self.require_authority,
            capture=lambda binding: self.capture(binding, capture_native=capture_native,
                                                  capture_credentials=capture_credentials),
            encrypt_export=encrypt_export)

    def export_snapshot(self, capture_adapter, *, encrypt_export):
        """Capture through an independently authenticated snapshot adapter.

        The adapter must have been constructed for exactly this source binding.
        Its own guard performs the same export-authority and live-fence checks;
        binding equality here denies any re-pointed, stale or foreign adapter.
        """
        if not isinstance(getattr(capture_adapter, 'binding', None), dict) \
                or not _same_binding(capture_adapter.binding, self.binding):
            raise EscrowError('snapshot capture adapter is not bound to this source')
        return self.export(capture_native=capture_adapter.native,
                           capture_credentials=capture_adapter.credentials,
                           encrypt_export=encrypt_export)


class LoopbackSnapshotIO:
    """Read only the prepared repository through the bound engine's loopback.

    exec_read must independently guard its exact process lifetime. Credentials
    travel in curl's stdin config, never argv, files, environment or diagnostics.
    No caller URL, redirect, proxy, write method or alternate port is admitted.
    """
    def __init__(self, *, exec_read, repository, snapshot, location):
        for value in (repository, snapshot):
            if not isinstance(value, str) or not re.fullmatch(r'[a-z0-9][a-z0-9_-]*', value):
                raise EscrowError('explicit prepared repository and snapshot required')
        root = '/usr/share/elasticsearch/data/snapshot'
        if (not isinstance(location, str) or not (location == root or location.startswith(root + '/'))
                or any(x in ('', '.', '..') for x in location.split('/')[1:])):
            raise EscrowError('explicit prepared archive location required')
        self.exec_read, self.location = exec_read, location
        self.paths = frozenset({'/', '/_security/_authenticate', '/_snapshot/' + repository,
                               '/_snapshot/' + repository + '/' + snapshot})

    def request(self, path, credentials):
        if not isinstance(path, str) or path not in self.paths:
            raise EscrowError('prepared snapshot read path required')
        credentials = credentials_from_bytes(_encoded(credentials))
        password = credentials['elastic_password'].replace('\\', '\\\\').replace('"', '\\"')
        config = ('user = "elastic:' + password + '"\n').encode()
        try:
            raw = self.exec_read('curl', '-q', '--silent', '--fail', '--max-time', '60',
                '--noproxy', '*', '--proto', '=http', '--config', '-',
                '--url', 'http://127.0.0.1:9200' + path, data=config)
            def unique(pairs):
                result = {}
                for key, value in pairs:
                    if key in result:
                        raise ValueError('duplicate response key')
                    result[key] = value
                return result
            if not isinstance(raw, bytes) or len(raw) > 8 * 1024 * 1024:
                raise ValueError('response exceeds bound')
            value = json.loads(raw, object_pairs_hook=unique)
            if not isinstance(value, dict):
                raise ValueError('object response required')
            return value
        except EscrowError:
            raise
        except Exception:
            raise EscrowError('bound loopback snapshot read failed') from None

    def read_archive(self, location):
        if location != self.location:
            raise EscrowError('prepared archive location changed')
        return self.exec_read('tar', '-C', location, '-cf', '-', '.')


class PreparedSnapshotExport:
    """Compose original Secret, authenticated native and source escrow capture.

    This only reads an already prepared snapshot. Its UUID and IndexVersion must
    come from the owned creation response, not a fresh discovery of any snapshot.
    Export encryption/destination stays explicit and independently authorized.
    """
    def __init__(self, source, *, repository, snapshot, location, indices,
                 snapshot_uuid, snapshot_version):
        if not isinstance(source, KubernetesSource) or not snapshot_uuid or not snapshot_version:
            raise EscrowError('original source and prepared snapshot identity required')
        self.source, self.binding = source, _binding(source.binding)
        self.io = LoopbackSnapshotIO(exec_read=source.exec_read, repository=repository,
                                    snapshot=snapshot, location=location)
        self.adapter = SnapshotCapture(self.binding, guard=self.guard,
            read_credentials=self.read_credentials, request=self.io.request,
            read_archive=self.io.read_archive, repository=repository, snapshot=snapshot,
            location=location, indices=indices, expected_uuid=snapshot_uuid,
            snapshot_version=snapshot_version)

    def guard(self):
        if not _same_binding(self.source.binding, self.binding):
            raise EscrowError('prepared source binding changed')
        return self.source.guard()

    def read_credentials(self):
        self.guard()
        raw = self.source.run(['kubectl', 'get', 'secret', 'elasticsearch-secret', '-n', 'database',
                               '-o', 'jsonpath={.data.ELASTIC_PASSWORD}'])
        self.guard()
        try:
            if not isinstance(raw, bytes) or not raw:
                raise ValueError('credential bytes absent')
            decoded = base64.b64decode(raw, validate=True)
            if base64.b64encode(decoded) != raw:
                raise ValueError('credential encoding not canonical')
            value = {'elastic_username': 'elastic', 'elastic_password': decoded.decode()}
            return _encoded(credentials_from_bytes(_encoded(value)))
        except (ValueError, TypeError, UnicodeError):
            raise EscrowError('bound original credential invalid') from None

    def capture_catalog(self, *, ledger, backend, consumers):
        """Read a supplied complete roster, without exporting or mutating source state.

        The original authorization and all-writer fence remain required even for
        catalog reads. The caller may explicitly bind this catalog into a new
        source generation before export; this method cannot authorize that step.
        """
        from kopiur_elasticsearch_catalog import LoopbackCatalogIO, SourceCatalog
        io_adapter = LoopbackCatalogIO(exec_read=self.source.exec_read,
                                       indices=sorted(self.adapter.indices))
        catalog = SourceCatalog(self.binding, ledger=ledger, backend=backend,
            consumer_indices=consumers, indices=sorted(self.adapter.indices),
            guard=self.guard, request=io_adapter.request)
        self.guard()
        credentials = self.adapter.authenticated_credentials(self.binding)
        value = catalog.capture(credentials)
        self.guard()
        return value

    def export(self, *, encrypt_export):
        self.guard()
        # Source capture compares this exact password with /proc/1/environ before
        # reading configuration or forwarding any plaintext to encryption.
        return self.source.export_snapshot(self.adapter, encrypt_export=encrypt_export)
