"""Adapter ordering regressions. Real engine acceptance runs separately on ARC."""
import copy
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from kopiur_elasticsearch_engine_fixture import EngineRestore, prove_capture_revocation
from kopiur_elasticsearch_capture import SnapshotCapture, snapshot_metadata, synthetic_snapshot_archive
from kopiur_elasticsearch_escrow import CONFIG_FILES, EscrowError, _encoded


class EngineAdapterTests(unittest.TestCase):
    def setUp(self):
        self.engine = EngineRestore.__new__(EngineRestore)
        self.engine.drill = Mock()
        self.engine.binding = {'generation': 'synthetic'}
        self.engine.parts = {'native': {'data': b'native'}, 'runtime': {'data': b'runtime'}}
        self.engine.check_config = Mock()
        self.target = {'uid': 'a' * 64}
        self.config = {'runtime': copy.deepcopy(self.engine.parts['runtime'])}

    def restore(self):
        with patch('kopiur_elasticsearch_engine_fixture.configuration_archive', return_value=b'archive'):
            return self.engine.configuration(self.target, self.engine.binding, self.config)

    def test_tar_member_owners_not_rewritten_to_container_user(self):
        self.assertIs(self.restore(), True)
        self.engine.drill.run.assert_called_once_with(
            'cp', '-', self.target['uid'] + ':/usr/share/elasticsearch/config', data=b'archive')
        self.engine.check_config.assert_called_once_with(self.target)
        self.engine.drill.start_registered.assert_not_called()

    def test_metadata_check_failure_does_not_start_engine(self):
        self.engine.check_config.side_effect = EscrowError('metadata differs')
        with self.assertRaises(EscrowError):
            self.restore()
        self.engine.drill.start_registered.assert_not_called()

    def test_changed_configuration_denied_before_archive_copy(self):
        self.config['runtime']['data'] = b'changed'
        with self.assertRaises(EscrowError):
            self.restore()
        self.engine.drill.run.assert_not_called()


class CaptureRevocationTests(unittest.TestCase):
    def setUp(self):
        self.binding = {'generation': 'a' * 32, 'source_uid': 'b' * 64, 'source_pod_uid': 'b' * 64,
                        'engine_image': 'engine@sha256:' + 'c' * 64, 'runtime_version': '8.19.1',
                        'credential_versions': {'owned-synthetic': 'fixture'},
                        'config_paths': sorted(CONFIG_FILES)}
        self.requests = []
        self.snapshot_version = '8.19.1'
        self.read = Mock(return_value=synthetic_snapshot_archive())
        self.adapter = SnapshotCapture(self.binding, guard=lambda: True,
            read_credentials=lambda: _encoded({'elastic_username': 'elastic', 'elastic_password': 'synthetic'}),
            request=self.request, read_archive=self.read, repository='fixture', snapshot='generation',
            location='/usr/share/elasticsearch/data/snapshot', indices=['fixture'])

    def request(self, path, credentials):
        self.requests.append(path)
        return {
            '/_security/_authenticate': {'username': 'elastic', 'roles': ['superuser']},
            '/': {'version': {'number': '8.19.1'}},
            '/_snapshot/fixture': {'fixture': {'type': 'fs',
                'settings': {'location': '/usr/share/elasticsearch/data/snapshot'}}},
            '/_snapshot/fixture/generation': {'snapshots': [{
                'snapshot': 'generation', 'uuid': 'fixture-snapshot', 'state': 'SUCCESS',
                'include_global_state': True, 'version': self.snapshot_version,
                'metadata': snapshot_metadata(self.binding), 'indices': ['fixture', '.security-7'],
                'shards': {'total': 2, 'successful': 2, 'failed': 0},
                'feature_states': [{'feature_name': 'security', 'indices': ['.security-7']}]}]},
        }[path]

    def test_revocation_boundaries_use_real_adapter_checks(self):
        self.assertEqual(prove_capture_revocation(self.adapter, self.binding),
                         ['before-read', 'after-authentication', 'after-archive-read'])
        self.read.assert_called_once()
        self.assertEqual(self.requests.count('/_security/_authenticate'), 2)
        self.assertEqual(self.requests.count('/_snapshot/fixture/generation'), 1)
        # No post-read snapshot verification or credentials escape after denial.
        self.assertEqual(self.requests[-1], '/_snapshot/fixture/generation')

    def test_authentication_failure_is_not_revocation_proof(self):
        self.adapter.request = lambda path, credentials: {'username': 'other'}
        with self.assertRaisesRegex(EscrowError, 'authentication not proved'):
            prove_capture_revocation(self.adapter, self.binding)
        self.read.assert_not_called()

    def test_created_snapshot_uuid_is_enforced_before_archive_read(self):
        self.adapter.expected_uuid = 'different-created-snapshot'
        with self.assertRaisesRegex(EscrowError, '^coherent complete native/security snapshot required: snapshot_uuid$'):
            self.adapter.native(self.binding)
        self.read.assert_not_called()

    def test_matching_created_snapshot_uuid_allows_capture(self):
        self.adapter.expected_uuid = 'fixture-snapshot'
        self.assertEqual(self.adapter.native(self.binding)['data'], synthetic_snapshot_archive())
        self.read.assert_called_once()

    def test_index_format_version_is_independent_of_engine_patch_version(self):
        self.snapshot_version = '8.19.0'
        self.adapter.snapshot_version = self.snapshot_version
        self.assertEqual(self.adapter.native(self.binding)['data'], synthetic_snapshot_archive())
        self.read.assert_called_once()

    def test_unexpected_snapshot_format_version_denied_before_archive_read(self):
        self.adapter.snapshot_version = '8.18.0'
        with self.assertRaisesRegex(EscrowError, '^coherent complete native/security snapshot required: snapshot_format_version$'):
            self.adapter.native(self.binding)
        self.read.assert_not_called()

    def test_matching_snapshot_format_never_bypasses_runtime_version(self):
        self.adapter.binding['runtime_version'] = '8.19.2'
        self.binding['runtime_version'] = '8.19.2'
        self.adapter.snapshot_version = self.snapshot_version
        with self.assertRaisesRegex(EscrowError, '^authenticated native runtime version differs$'):
            self.adapter.native(self.binding)
        self.read.assert_not_called()

    def test_io_after_revocation_cannot_count_as_proof(self):
        def missing_guard(attempt, binding):
            # Deliberately emulate an adapter that reads before checking authority.
            attempt.read_credentials()
        with patch('kopiur_elasticsearch_capture.SnapshotCapture.native', missing_guard):
            with self.assertRaisesRegex(EscrowError, 'I/O attempted after synthetic authority revocation'):
                prove_capture_revocation(self.adapter, self.binding)

    def test_transport_failure_is_not_revocation_proof(self):
        self.read.side_effect = RuntimeError('PRIVATE synthetic error')
        with self.assertRaisesRegex(EscrowError, '^authenticated source operation failed$'):
            prove_capture_revocation(self.adapter, self.binding)


if __name__ == '__main__':
    unittest.main()
