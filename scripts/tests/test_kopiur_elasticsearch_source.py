"""Source capture boundary regressions. Run only on the existing ARC runner.

Synthetic Kubernetes responses test binding/admission, not a production capture.
The independent native NAS/R2 engine drill remains a separate real gate.
"""
import copy
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from kopiur_elasticsearch_escrow import CONFIG_FILES, EscrowError
from kopiur_elasticsearch_escrow_fixture import configuration_archive
from kopiur_elasticsearch_source import KubernetesSource


def fixture():
    binding = {'generation': 'a' * 32, 'source_uid': 'containerd://' + 'b' * 64,
        'source_pod_uid': 'synthetic-pod-uid', 'engine_image': 'engine@sha256:' + 'c' * 64,
        'runtime_version': '8.19.1', 'config_paths': sorted(CONFIG_FILES),
        'config_directories': ['.'],
        'source_lifetime': {'namespace': 'database', 'pod': 'elasticsearch-0', 'container': 'app',
            'runtime_image': 'engine@sha256:' + 'd' * 64, 'restart_count': 0,
            'started_at': '2026-10-08T00:00:00Z'},
        'credential_versions': {'external-secret-uid': 'provider-uid',
            'external-secret-resource-version': '1', 'external-secret-synced-version': '1-v1',
            'target-secret-uid': 'secret-uid', 'target-secret-resource-version': '2'}}
    pod = {'metadata': {'uid': binding['source_pod_uid']},
        'spec': {'containers': [{'name': 'app', 'image': binding['engine_image'], 'env': [
            {'name': 'ELASTIC_PASSWORD', 'valueFrom': {'secretKeyRef': {
                'name': 'elasticsearch-secret', 'key': 'ELASTIC_PASSWORD'}}}]}]},
        'status': {'phase': 'Running', 'containerStatuses': [{'name': 'app', 'ready': True,
            'containerID': binding['source_uid'], 'imageID': binding['source_lifetime']['runtime_image'],
            'restartCount': 0, 'state': {'running': {'startedAt': binding['source_lifetime']['started_at']}}}]}}
    provider = {'metadata': {'uid': 'provider-uid', 'resourceVersion': '1'},
        'spec': {'target': {'name': 'elasticsearch-secret'}, 'secretStoreRef': {
            'kind': 'ClusterSecretStore', 'name': 'op-secret-store'}},
        'status': {'conditions': [{'type': 'Ready', 'status': 'True'}], 'syncedResourceVersion': '1-v1'}}
    secret = {'uid': 'secret-uid', 'resourceVersion': '2', 'ownerReferences': [
        {'controller': True, 'uid': 'provider-uid', 'kind': 'ExternalSecret'}]}
    parts = {name: {'binding': copy.deepcopy(binding), 'data': ('synthetic-' + name).encode(),
                   'mode': 0o600, 'uid': 1000, 'gid': 1000}
             for name in {'native', 'runtime', 'credentials'} | {'config/' + p for p in CONFIG_FILES}}
    parts['credentials']['data'] = json.dumps({'elastic_username': 'elastic',
                                             'elastic_password': 'synthetic-password'}).encode()
    parts['config-dir/.'] = {'binding': copy.deepcopy(binding), 'data': b'',
                            'mode': 0o750, 'uid': 1000, 'gid': 0}
    return binding, pod, provider, secret, parts


class SourceTests(unittest.TestCase):
    def setUp(self):
        self.binding, self.pod, self.provider, self.secret, self.parts = fixture()
        self.calls = []
        self.authority = Mock(return_value=True)
        self.version = Mock(return_value=self.binding['runtime_version'])
        self.native = Mock(return_value=self.parts['native'])
        self.credentials = Mock(return_value=self.parts['credentials'])
        self.environment = b'PATH=/bin\0discovery.type=single-node\0ELASTIC_PASSWORD=synthetic-password\0'
        self.source = KubernetesSource(self.binding, read_version=self.version,
                                      require_authority=self.authority, run=self.remote_read)

    def remote_read(self, argv):
        self.calls.append(argv)
        if argv[1] == 'get':
            value = {'pod': self.pod, 'externalsecret': self.provider, 'secret': self.secret}[argv[2]]
            if argv[2] == 'secret':
                self.assertEqual(argv[-1], 'jsonpath={.metadata}')
            return json.dumps(value).encode()
        if argv[-2:] == ['cat', '/proc/1/environ']:
            return self.environment
        self.assertEqual(argv[-6:], ['tar', '-C', '/usr/share/elasticsearch/config', '-cf', '-', '.'])
        return configuration_archive(self.binding, self.parts)

    def capture(self):
        return self.source.capture(self.binding, capture_native=self.native,
                                   capture_credentials=self.credentials)

    def test_bound_capture_contains_original_keystore_metadata_and_runtime(self):
        parts = self.capture()
        for name in ['config/elasticsearch.keystore', 'config-dir/.', 'native', 'credentials']:
            self.assertEqual(parts[name], self.parts[name])
        runtime = json.loads(parts['runtime']['data'])
        self.assertEqual(runtime['variables']['discovery.type'], 'single-node')
        self.assertNotIn('ELASTIC_PASSWORD', runtime['variables'])
        self.assertGreater(self.authority.call_count, 4)
        self.assertEqual([x[1] for x in self.calls].count('exec'), 2)

    def test_no_original_authority_reads_no_source_data(self):
        for denied in (False, None, 1):
            self.authority.return_value = denied
            with self.subTest(denied=denied), self.assertRaises(EscrowError):
                self.capture()
        self.assertEqual(self.calls, [])
        self.native.assert_not_called()
        self.credentials.assert_not_called()

    def test_restart_pod_replacement_image_and_provider_rotation_deny_capture(self):
        mutations = [('pod', lambda: self.pod['metadata'].update(uid='replacement')),
            ('container', lambda: self.pod['status']['containerStatuses'][0].update(containerID='containerd://' + 'e' * 64)),
            ('restart', lambda: self.pod['status']['containerStatuses'][0].update(restartCount=1)),
            ('runtime-image', lambda: self.pod['status']['containerStatuses'][0].update(imageID='engine@sha256:' + 'e' * 64)),
            ('started', lambda: self.pod['status']['containerStatuses'][0]['state']['running'].update(startedAt='new')),
            ('secret-version', lambda: self.secret.update(resourceVersion='3')),
            ('provider-version', lambda: self.provider['metadata'].update(resourceVersion='3')),
            ('provider-synced', lambda: self.provider['status'].update(syncedResourceVersion='new')),
            ('runtime-version', lambda: setattr(self.version, 'return_value', '9.0.0'))]
        for name, mutation in mutations:
            self.setUp()
            mutation()
            with self.subTest(name=name), self.assertRaises(EscrowError):
                self.capture()
            self.assertFalse(any(x[1] == 'exec' for x in self.calls))
            self.native.assert_not_called()

    def test_capture_callback_restart_denied_before_configuration_or_export(self):
        def restart(_):
            self.pod['status']['containerStatuses'][0]['restartCount'] += 1
            return self.parts['native']
        self.native.side_effect = restart
        encrypt = Mock()
        with self.assertRaises(EscrowError):
            self.source.export(capture_native=self.native, capture_credentials=self.credentials,
                               encrypt_export=encrypt)
        self.credentials.assert_not_called()
        encrypt.assert_not_called()
        self.assertFalse(any(x[1] == 'exec' for x in self.calls))

    def test_revoked_authority_after_config_exec_denies_export(self):
        old_run = self.source.run
        def revoked(argv):
            result = old_run(argv)
            if argv[1] == 'exec' and 'tar' in argv:
                self.authority.return_value = False
            return result
        self.source.run = revoked
        encrypt = Mock()
        with self.assertRaises(EscrowError):
            self.source.export(capture_native=self.native, capture_credentials=self.credentials,
                               encrypt_export=encrypt)
        encrypt.assert_not_called()

    def test_env_duplicate_invalid_or_unterminated_denied(self):
        for raw in (b'A=1\0A=2\0', b'A=1', b'=value\0', b'bad\0', b'A=\xff\0'):
            self.environment = raw
            with self.subTest(raw=raw), self.assertRaises(EscrowError):
                self.capture()
            self.assertFalse(any('tar' in x for x in self.calls))

    def test_startup_overrides_denied_before_native_capture(self):
        for field in ('command', 'args'):
            self.setUp()
            self.pod['spec']['containers'][0][field] = ['eswrapper', '-Ecluster.name=custom']
            with self.subTest(field=field), self.assertRaises(EscrowError):
                self.capture()
            self.native.assert_not_called()

    def test_duplicate_declared_password_rejected_before_native_capture(self):
        self.pod['spec']['containers'][0]['env'].append({'name': 'ELASTIC_PASSWORD', 'value': 'foreign'})
        with self.assertRaises(EscrowError):
            self.capture()
        self.native.assert_not_called()

    def test_rotated_provider_password_not_loaded_by_source_is_denied(self):
        self.parts['credentials']['data'] = json.dumps({'elastic_username': 'elastic',
                                                     'elastic_password': 'rotated-password'}).encode()
        with self.assertRaises(EscrowError):
            self.capture()
        self.assertFalse(any('tar' in x for x in self.calls))

    def test_alternate_active_config_root_is_denied(self):
        self.environment += b'ES_PATH_CONF=/alternate/config\0'
        with self.assertRaises(EscrowError):
            self.capture()
        self.assertFalse(any('tar' in x for x in self.calls))

    def test_provider_ownership_deletion_and_secret_reference_denied(self):
        for mutation in (
            lambda: self.secret['ownerReferences'][0].update(uid='foreign'),
            lambda: self.provider['status']['conditions'][0].update(status='False'),
            lambda: self.secret.update(deletionTimestamp='now'),
            lambda: self.pod['spec']['containers'][0]['env'][0]['valueFrom']['secretKeyRef'].update(name='foreign'),
            lambda: self.pod['spec']['containers'][0].update(envFrom=[{'secretRef': {'name': 'foreign'}}]),
        ):
            self.setUp(); mutation()
            with self.assertRaises(EscrowError):
                self.capture()
            self.native.assert_not_called()

    def test_remote_error_and_timeout_never_disclose_buffers(self):
        for failure in (subprocess.CompletedProcess([], 1, b'synthetic-secret', b'synthetic-secret'),
                        subprocess.TimeoutExpired([], 1, output=b'synthetic-secret')):
            with patch('kopiur_elasticsearch_source.subprocess.run') as run:
                if isinstance(failure, Exception):
                    run.side_effect = failure
                else:
                    run.return_value = failure
                with self.assertRaises(EscrowError) as caught:
                    KubernetesSource._run(['kubectl'])
                self.assertNotIn('synthetic-secret', str(caught.exception))

    def test_lifetime_shape_and_wrong_original_service_rejected(self):
        for field, value in [('restart_count', True), ('runtime_image', 'mutable:latest'),
                             ('namespace', 'foreign')]:
            candidate = copy.deepcopy(self.binding)
            candidate['source_lifetime'][field] = value
            with self.subTest(field=field), self.assertRaises(EscrowError):
                KubernetesSource(candidate, read_version=self.version, require_authority=self.authority)


if __name__ == '__main__':
    unittest.main()
