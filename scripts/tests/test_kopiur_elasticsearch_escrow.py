"""Synthetic contract tests. No original credentials or production I/O."""
import copy
import hashlib
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from kopiur_elasticsearch_escrow import (
    CHECKS, CONFIG_FILES, EscrowError, capture_export, restore_export,
)


class EscrowTests(unittest.TestCase):
    def setUp(self):
        self.binding = {'generation': 'a' * 32, 'source_uid': 'original-source',
                        'source_pod_uid': 'original-pod', 'engine_image': 'engine@sha256:' + 'b' * 64,
                        'runtime_version': 'runtime-1', 'credential_versions': {'credential': 'version-1'},
                        'config_paths': sorted(CONFIG_FILES)}
        self.parts = {name: {'binding': copy.deepcopy(self.binding), 'data': ('synthetic-' + name).encode(),
                             'mode': 0o660, 'uid': 1000, 'gid': 1000}
                      for name in {'native', 'runtime', 'credentials'} | {'config/' + n for n in CONFIG_FILES}}
        self.observe = Mock(side_effect=lambda: copy.deepcopy(self.binding))
        self.authority = Mock(return_value=True)
        self.capture = Mock(side_effect=lambda binding: copy.deepcopy(self.parts))
        self.encrypt = Mock(side_effect=self.encrypted)
        self.target = {'uid': 'isolated-target', 'isolated': True}
        self.restore_config = Mock(return_value=True)
        self.verify_config = Mock(return_value={name: True for name in
            ('config_metadata', 'keystore_load', 'credential_authentication', 'runtime_identity')})
        self.restore_native = Mock(return_value=True)
        self.verify = Mock(return_value={name: True for name in CHECKS})

    def encrypted(self, manifest, parts):
        # Adapter double only, not encryption proof or a real export receipt.
        digest = hashlib.sha256(json.dumps(manifest, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        return {'ciphertext': b'synthetic-encryption-adapter-output', 'object_id': 'synthetic-object',
                'manifest_sha256': digest}

    def export(self):
        return capture_export(self.binding, observe=self.observe, require_capture_authority=self.authority,
                              capture=self.capture, encrypt_export=self.encrypt)

    def restore(self, exported):
        return restore_export(exported, binding=self.binding, target=self.target,
                              require_isolated_authority=self.authority,
                              decrypt=lambda data, manifest: copy.deepcopy(self.parts),
                              restore_configuration=self.restore_config, restore_native=self.restore_native,
                              verify_configuration=self.verify_config,
                              verify=self.verify)

    def test_complete_contract_and_restore_order(self):
        calls = Mock()
        calls.attach_mock(self.restore_config, 'configuration')
        calls.attach_mock(self.restore_native, 'native')
        calls.attach_mock(self.verify_config, 'prerequisites')
        calls.attach_mock(self.verify, 'verify')
        result = self.restore(self.export())
        self.assertEqual([c[0] for c in calls.mock_calls], ['configuration', 'prerequisites', 'native', 'verify'])
        self.assertTrue(result['contract_verified'])
        self.assertFalse(result['production_acceptance'])
        self.assertFalse(result['source_admission_released'])
        self.assertNotIn('native', self.restore_config.call_args.args[2])

    def test_every_component_required(self):
        original = copy.deepcopy(self.parts)
        for name in original:
            with self.subTest(name=name):
                self.parts = copy.deepcopy(original)
                del self.parts[name]
                with self.assertRaises(EscrowError):
                    self.export()
        self.encrypt.assert_not_called()

    def test_generation_runtime_and_credential_mismatch(self):
        for key, replacement in [('generation', 'c' * 32), ('runtime_version', 'stale'),
                                 ('credential_versions', {'credential': 'stale'})]:
            with self.subTest(key=key):
                self.parts['runtime']['binding'] = {**self.binding, key: replacement}
                with self.assertRaises(EscrowError):
                    self.export()
        self.encrypt.assert_not_called()

    def test_observation_changes_during_capture_or_export(self):
        changed = {**self.binding, 'runtime_version': 'replaced'}
        for observations in ([self.binding, changed], [self.binding, self.binding, changed]):
            with self.subTest(observations=observations):
                self.observe.side_effect = observations
                with self.assertRaises(EscrowError):
                    self.export()

    def test_authority_failure_prevents_capture(self):
        self.authority.side_effect = RuntimeError('generation revoked')
        with self.assertRaises(RuntimeError):
            self.export()
        self.capture.assert_not_called()
        self.encrypt.assert_not_called()

    def test_export_adapter_binding_and_plaintext_rejected(self):
        for field, value in [('manifest_sha256', 'c' * 64), ('object_id', ''),
                             ('ciphertext', self.parts['native']['data'])]:
            with self.subTest(field=field):
                self.encrypt.side_effect = lambda m, p: {**self.encrypted(m, p), field: value}
                with self.assertRaises(EscrowError):
                    self.export()

    def test_ciphertext_manifest_and_component_tampering_denied(self):
        exported = self.export()
        bad = copy.deepcopy(exported)
        bad['ciphertext'] += b'changed'
        with self.assertRaises(EscrowError):
            self.restore(bad)
        bad = copy.deepcopy(exported)
        bad['manifest']['entries']['native']['bytes'] += 1
        with self.assertRaises(EscrowError):
            self.restore(bad)
        self.parts['config/elasticsearch.keystore']['data'] += b'changed'
        with self.assertRaises(EscrowError):
            self.restore(exported)
        self.restore_config.assert_not_called()

    def test_nonisolated_and_original_target_denied(self):
        exported = self.export()
        for target in ({'uid': 'original-source', 'isolated': True},
                       {'uid': 'original-pod', 'isolated': True},
                       {'uid': 'another', 'isolated': False}):
            self.target = target
            with self.assertRaises(EscrowError):
                self.restore(exported)
        self.restore_config.assert_not_called()

    def test_incomplete_configuration_stops_native_restore(self):
        self.restore_config.return_value = False
        with self.assertRaises(EscrowError):
            self.restore(self.export())
        self.restore_native.assert_not_called()

    def test_incomplete_native_or_any_verification_denied(self):
        exported = self.export()
        self.restore_native.return_value = False
        with self.assertRaises(EscrowError):
            self.restore(exported)
        self.verify.assert_not_called()
        self.restore_native.return_value = True
        for name in CHECKS:
            with self.subTest(name=name):
                self.verify.return_value = {check: check != name for check in CHECKS}
                with self.assertRaises(EscrowError):
                    self.restore(exported)

    def test_nonaffirmative_authority_denied(self):
        exported = self.export()
        for value in (False, None, 1):
            self.authority.return_value = value
            with self.assertRaises(EscrowError):
                self.export()
            with self.assertRaises(EscrowError):
                self.restore(exported)
        self.restore_config.assert_not_called()

    def test_additional_config_dependency_required_and_supported(self):
        self.binding['config_paths'].append('certs/tls.key')
        for part in self.parts.values():
            part['binding'] = copy.deepcopy(self.binding)
        with self.assertRaises(EscrowError):
            self.export()
        self.parts['config/certs/tls.key'] = {
            'binding': copy.deepcopy(self.binding), 'data': b'synthetic-key',
            'uid': 1000, 'gid': 1000, 'mode': 0o600}
        self.restore(self.export())

    def test_configuration_prerequisites_stop_native_restore(self):
        self.verify_config.return_value['keystore_load'] = False
        with self.assertRaises(EscrowError):
            self.restore(self.export())
        self.restore_native.assert_not_called()

    def test_caller_receipt_mutation_cannot_replace_verified_manifest(self):
        exported = self.export()
        def decrypt(data, manifest):
            changed = copy.deepcopy(self.parts)
            changed['native']['data'] = b'changed-native'
            exported['manifest']['entries']['native']['sha256'] = hashlib.sha256(b'changed-native').hexdigest()
            exported['manifest']['entries']['native']['bytes'] = len(b'changed-native')
            return changed
        with self.assertRaises(EscrowError):
            restore_export(exported, binding=self.binding, target=self.target,
                           require_isolated_authority=self.authority, decrypt=decrypt,
                           restore_configuration=self.restore_config,
                           verify_configuration=self.verify_config,
                           restore_native=self.restore_native, verify=self.verify)
        self.restore_config.assert_not_called()

    def test_empty_file_metadata_and_extra_coverage_denied(self):
        self.parts['config/users']['data'] = b''
        self.export()
        self.parts['config/elasticsearch.keystore']['data'] = b''
        with self.assertRaises(EscrowError):
            self.export()
        self.parts['config/elasticsearch.keystore']['data'] = b'synthetic-key'
        self.parts['config/users']['mode'] = True
        with self.assertRaises(EscrowError):
            self.export()


if __name__ == '__main__':
    unittest.main()
