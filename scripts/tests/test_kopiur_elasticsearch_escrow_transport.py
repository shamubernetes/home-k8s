"""Synthetic bundle/provider integration tests, no credentials or provider I/O."""
import copy
import hashlib
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import kopiur_elasticsearch_escrow_transport as transport
from kopiur_elasticsearch_escrow import CHECKS, CONFIG_FILES, EscrowError


class BundleTests(unittest.TestCase):
    def setUp(self):
        self.binding = {'generation': 'a' * 32, 'source_uid': 'synthetic-source',
                        'source_pod_uid': 'synthetic-pod', 'engine_image': 'engine@sha256:' + 'b' * 64,
                        'runtime_version': 'synthetic-runtime', 'credential_versions': {'synthetic': 'v1'},
                        'config_paths': sorted(CONFIG_FILES)}
        self.parts = {name: {'binding': copy.deepcopy(self.binding), 'data': b'\x00\xffsynthetic',
                             'mode': 0o600, 'uid': 1000, 'gid': 1000}
                      for name in {'native', 'runtime', 'credentials'} | {'config/' + n for n in CONFIG_FILES}}
        self.parts['config/users']['data'] = b''
        self.drill = SimpleNamespace(app='elasticsearch', fields={})
        self.authority = Mock(return_value=True)

    def provider(self, drill, data):
        digest = hashlib.sha256(data).hexdigest()
        def backend(kind):
            return {'backend': kind, 'snapshot_id': kind + '-snapshot', 'object_id': kind + '-object',
                    'archive_sha256': digest, 'archive_bytes': len(data),
                    'producer_removed_before_direct_restore': True, 'fresh_restorer': True,
                    'wrong_password_denied': True}
        receipt = {'generation': 'c' * 32, 'nas': backend('nas'), 'r2': backend('r2'),
                   'same_generation_archive_sha256': digest, 'r2_input_is_fresh_nas_restore': True,
                   'synthetic_archive_lineage_qualified': True, 'production_replication_qualified': False}
        receipt['r2']['source_nas_snapshot_id'] = receipt['nas']['snapshot_id']
        return data, receipt

    def roundtrip(self):
        return transport.roundtrip_synthetic_bundle(self.drill, self.binding, self.parts,
                                                   require_authority=self.authority, synthetic=True)

    def test_binary_empty_bytes_and_original_metadata_roundtrip(self):
        bundle, manifest = transport.encode_bundle(self.binding, self.parts)
        self.assertEqual(transport.decode_bundle(bundle, manifest), self.parts)
        self.assertNotIn(b'\x00\xffsynthetic', bundle)
        self.assertEqual(self.parts['native']['data'], b'\x00\xffsynthetic')

    def test_incomplete_capture_never_encoded(self):
        del self.parts['config/elasticsearch.keystore']
        with self.assertRaises(EscrowError):
            transport.encode_bundle(self.binding, self.parts)

    def test_changed_manifest_bytes_metadata_and_stale_binding_denied(self):
        bundle, manifest = transport.encode_bundle(self.binding, self.parts)
        for kind in ('manifest', 'bytes', 'metadata', 'binding', 'extra'):
            with self.subTest(kind=kind):
                value = json.loads(bundle)
                if kind == 'manifest':
                    value['manifest']['binding']['generation'] = 'c' * 32
                elif kind == 'bytes':
                    value['parts']['native']['data'] = 'Y2hhbmdlZA=='
                elif kind == 'metadata':
                    value['parts']['native']['mode'] = 0o777
                elif kind == 'binding':
                    value['parts']['native']['binding']['runtime_version'] = 'stale'
                else:
                    value['parts']['extra'] = value['parts']['native']
                with self.assertRaises(EscrowError):
                    transport.decode_bundle(transport._encoded(value), manifest)

    def test_duplicate_noncanonical_and_invalid_encoding_denied_sanitized(self):
        bundle, manifest = transport.encode_bundle(self.binding, self.parts)
        value = json.loads(bundle)
        value['parts']['native']['data'] = 'PRIVATE-invalid-base64'
        cases = [b'{"schema":1,"schema":2}', bundle + b' ', b'\xff',
                 transport._encoded(value), b'[]']
        for data in cases:
            with self.subTest(data=data[:20]):
                with self.assertRaises(EscrowError) as caught:
                    transport.decode_bundle(data, manifest)
                self.assertNotIn('PRIVATE', str(caught.exception))

    @patch.object(transport.sys, 'platform', 'linux')
    @patch.dict(transport.os.environ, {'RUNNER_NAME': 'ghar-set-zoo-synthetic'})
    def test_same_service_provider_bridge_preserves_generation_and_no_engine_claim(self):
        with patch.object(transport, 'validate_identity') as identity, \
                patch.object(transport, 'restore_generation', side_effect=self.provider) as provider:
            restored, receipt = self.roundtrip()
        identity.assert_called_once_with(self.drill.fields)
        provider.assert_called_once()
        self.assertEqual(restored, self.parts)
        self.assertEqual(receipt['binding'], self.binding)
        self.assertTrue(receipt['synthetic_bundle_transport_verified'])
        self.assertFalse(receipt['engine_restore_verified'])
        self.assertFalse(receipt['production_acceptance'])
        self.assertNotIn('ciphertext', receipt)

    @patch.object(transport.sys, 'platform', 'linux')
    @patch.dict(transport.os.environ, {'RUNNER_NAME': 'ghar-set-zoo-synthetic'})
    def test_bad_provider_evidence_never_returns_parts(self):
        mutations = [lambda r: r['nas'].update(wrong_password_denied=False),
                     lambda r: r['r2'].update(source_nas_snapshot_id='different'),
                     lambda r: r.update(production_replication_qualified=True),
                     lambda r: r['nas'].update(archive_bytes=True),
                     lambda r: r['r2'].update(snapshot_id=''),
                     lambda r: r['r2'].update(archive_sha256='0' * 64)]
        for mutate in mutations:
            def changed(drill, data):
                restored, receipt = self.provider(drill, data)
                mutate(receipt)
                return restored, receipt
            with self.subTest(mutate=mutate), patch.object(transport, 'validate_identity'), \
                    patch.object(transport, 'restore_generation', side_effect=changed):
                with self.assertRaises(EscrowError):
                    self.roundtrip()

    @patch.object(transport.sys, 'platform', 'linux')
    @patch.dict(transport.os.environ, {'RUNNER_NAME': 'ghar-set-zoo-synthetic'})
    def test_authority_revocation_before_or_after_provider_denied(self):
        for results in ([False], [True, False], [True, True, False], [True, True, True, False]):
            with self.subTest(results=results), patch.object(transport, 'validate_identity'), \
                    patch.object(transport, 'restore_generation', side_effect=self.provider) as provider:
                self.authority.side_effect = results
                with self.assertRaises(EscrowError):
                    self.roundtrip()
                self.assertEqual(provider.call_count, int(len(results) >= 3))

    @patch.object(transport.sys, 'platform', 'linux')
    @patch.dict(transport.os.environ, {'RUNNER_NAME': 'ghar-set-zoo-synthetic'})
    def test_provider_restored_parts_feed_ordered_isolated_restore(self):
        order = []
        def configuration(target, binding, parts):
            order.append('configuration')
            self.assertEqual(parts, {k: v for k, v in self.parts.items() if k != 'native'})
            return True
        def prerequisites(target, manifest):
            order.append('prerequisites')
            return {k: True for k in ('config_metadata', 'keystore_load',
                                     'credential_authentication', 'runtime_identity')}
        def native(target, binding, part):
            order.append('native')
            self.assertEqual(part, self.parts['native'])
            return True
        def verify(target, manifest):
            order.append('verify')
            return {k: True for k in CHECKS}
        with patch.object(transport, 'validate_identity'), \
                patch.object(transport, 'restore_generation', side_effect=self.provider):
            proof = transport.restore_synthetic_bundle(
                self.drill, self.binding, self.parts, require_authority=self.authority,
                synthetic=True, target={'uid': 'isolated-target', 'isolated': True},
                require_isolated_authority=lambda target, binding: True,
                restore_configuration=configuration, verify_configuration=prerequisites,
                restore_native=native, verify=verify)
        self.assertEqual(order, ['configuration', 'prerequisites', 'native', 'verify'])
        self.assertTrue(proof['restore']['contract_verified'])
        self.assertFalse(proof['transport']['engine_restore_verified'])
        self.assertFalse(proof['production_acceptance'])
        self.assertFalse(proof['source_admission_released'])

    @patch.object(transport.sys, 'platform', 'linux')
    @patch.dict(transport.os.environ, {'RUNNER_NAME': 'ghar-set-zoo-synthetic'})
    def test_revoked_isolation_after_transport_never_calls_restore_adapters(self):
        configuration, native = Mock(), Mock()
        with patch.object(transport, 'validate_identity'), \
                patch.object(transport, 'restore_generation', side_effect=self.provider):
            with self.assertRaises(EscrowError):
                transport.restore_synthetic_bundle(
                    self.drill, self.binding, self.parts, require_authority=self.authority,
                    synthetic=True, target={'uid': 'isolated-target', 'isolated': True},
                    require_isolated_authority=lambda target, binding: False,
                    restore_configuration=configuration, verify_configuration=Mock(),
                    restore_native=native, verify=Mock())
        configuration.assert_not_called()
        native.assert_not_called()

    def test_plaintext_restore_revalidates_manifest_and_parts_before_callbacks(self):
        _, manifest = transport.encode_bundle(self.binding, self.parts)
        callbacks = Mock()
        for mutation in ('bytes', 'metadata', 'manifest', 'target'):
            parts, changed = copy.deepcopy(self.parts), copy.deepcopy(manifest)
            target = {'uid': 'isolated-target', 'isolated': True}
            if mutation == 'bytes':
                parts['native']['data'] += b'changed'
            elif mutation == 'metadata':
                parts['native']['uid'] += 1
            elif mutation == 'manifest':
                changed['schema'] = 'invalid'
            else:
                target['uid'] = self.binding['source_uid']
            with self.subTest(mutation=mutation), self.assertRaises(EscrowError):
                transport.restore_parts(changed, parts, target=target,
                    require_isolated_authority=lambda target, binding: True,
                    restore_configuration=callbacks, verify_configuration=callbacks,
                    restore_native=callbacks, verify=callbacks)
        callbacks.assert_not_called()

    def test_nonarc_and_nonsynthetic_denied_before_any_identity_or_provider_call(self):
        with patch.object(transport, 'validate_identity') as identity, \
                patch.object(transport, 'restore_generation') as provider, \
                patch.object(transport.sys, 'platform', 'darwin'):
            with self.assertRaises(EscrowError):
                self.roundtrip()
        identity.assert_not_called()
        provider.assert_not_called()
        with patch.object(transport.sys, 'platform', 'linux'), \
                patch.dict(transport.os.environ, {'RUNNER_NAME': 'ghar-set-zoo-synthetic'}):
            for flag in (False, None, 1):
                with self.assertRaises(EscrowError):
                    transport.roundtrip_synthetic_bundle(self.drill, self.binding, self.parts,
                                                       require_authority=self.authority, synthetic=flag)

    @patch.object(transport.sys, 'platform', 'linux')
    @patch.dict(transport.os.environ, {'RUNNER_NAME': 'ghar-set-zoo-synthetic'})
    def test_wrong_service_or_invalid_identity_never_reaches_provider(self):
        with patch.object(transport, 'restore_generation') as provider:
            self.drill.app = 'another-service'
            with self.assertRaises(EscrowError):
                self.roundtrip()
            self.drill.app = 'elasticsearch'
            with patch.object(transport, 'validate_identity', side_effect=ValueError('invalid scope')):
                with self.assertRaises(ValueError):
                    self.roundtrip()
        provider.assert_not_called()


if __name__ == '__main__':
    unittest.main()
