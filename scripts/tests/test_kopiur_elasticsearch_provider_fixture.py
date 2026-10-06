"""Provider orchestration regressions. Native proof is a separate ARC drill."""
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import kopiur_elasticsearch_provider_fixture as provider


class FakeDrill:
    def __init__(self):
        self.fields = {'NAS_KOPIA_PASSWORD': 'nas-password', 'R2_KOPIA_PASSWORD': 'r2-password'}
        self.events = []
        self.restored = b'archive'
        self.denial = SimpleNamespace(returncode=1, stderr=b'invalid repository password')
        self.count = 0
        self.object_id = 'abc123'

    def start(self):
        self.count += 1
        name = 'container-' + str(self.count)
        self.events.append(('start', name))
        return name

    def remove(self, name):
        self.events.append(('remove', name))

    def backend(self, kind):
        return kind

    def run(self, args, **kwargs):
        self.events.append(('run', args))
        return SimpleNamespace(stdout=self.restored)

    def exec(self, container, body, **kwargs):
        self.events.append(('exec', container, body, kwargs))
        if kwargs.get('check') is False:
            return self.denial
        return SimpleNamespace(stdout=json.dumps({'id': 'snapshot', 'rootEntry': {'obj': self.object_id}}).encode())


class ProviderTests(unittest.TestCase):
    def fields(self):
        return {'R2_BUCKET': 'kopiur-elasticsearch', 'NAS_SHARE': 'kopiur-elasticsearch',
                'NAS_USERNAME': 'kp-elasticsearch', 'NAS_KOPIA_PASSWORD': 'nas',
                'R2_KOPIA_PASSWORD': 'r2', 'NAS_RCLONE_CONFIG':
                '[mnemosyne]\ntype = smb\nhost = 10.100.47.100\nuser = kp-elasticsearch\n'
                'domain = WORKGROUP\npass = obscured\n'}

    def test_dedicated_identity_config(self):
        provider.validate_identity(self.fields())

    def test_mismatched_nas_config_is_rejected(self):
        fields = self.fields()
        original = fields['NAS_RCLONE_CONFIG']
        for before, after in [('10.100.47.100', '127.0.0.1'),
                              ('kp-elasticsearch', 'other'), ('smb', 's3'),
                              ('obscured', ''), ('WORKGROUP', 'other'),
                              ('[mnemosyne]', '[other]')]:
            fields['NAS_RCLONE_CONFIG'] = original.replace(before, after)
            with self.subTest(before=before), self.assertRaises(ValueError):
                provider.validate_identity(fields)
        fields['NAS_RCLONE_CONFIG'] = original + '\n[extra]\ntype=smb\n'
        with self.assertRaises(ValueError):
            provider.validate_identity(fields)

    def test_shared_encryption_password_is_rejected(self):
        fields = self.fields()
        fields['R2_KOPIA_PASSWORD'] = fields['NAS_KOPIA_PASSWORD']
        with self.assertRaises(ValueError):
            provider.validate_identity(fields)

    def test_signal_handler_enters_exception_cleanup_path(self):
        with self.assertRaisesRegex(RuntimeError, 'interrupted by signal 15'):
            provider.interrupted(15, None)

    def test_expired_channel_deadline_prevents_resource_creation(self):
        transport = SimpleNamespace(TOOL_IMAGE='tools', IMAGE='mover', run=Mock())
        with patch.object(provider.sys, 'platform', 'linux'), \
                patch.dict(provider.os.environ, {'RUNNER_NAME': 'ghar-set-zoo-test'}), \
                patch.object(provider, 'load_transport', return_value=transport), \
                patch.object(provider.signal, 'signal'), \
                patch.object(provider.time, 'monotonic', return_value=1000):
            with self.assertRaisesRegex(RuntimeError, 'absolute deadline exceeded'):
                provider.exercise_payload({'app': 'elasticsearch', 'fields': self.fields()}, deadline=1100)
        transport.run.assert_not_called()

    def test_success_removes_producer_before_connecting_restorer(self):
        drill = FakeDrill()
        restored, receipt = provider.restore_archive(drill, 'nas', b'archive')
        self.assertEqual(restored, b'archive')
        self.assertEqual(receipt['archive_bytes'], 7)
        self.assertTrue(receipt['wrong_password_denied'])
        remove = drill.events.index(('remove', 'container-1'))
        start = drill.events.index(('start', 'container-3'))
        self.assertLess(remove, start)
        final = [e for e in drill.events if e[0] == 'exec' and e[1] == 'container-3'][0]
        self.assertIn('repository connect nas', final[2])
        self.assertEqual(final[3]['password'], 'nas-password')

    def test_r2_uses_independent_password(self):
        drill = FakeDrill()
        provider.restore_archive(drill, 'r2', b'archive')
        actual = [e for e in drill.events if e[0] == 'exec' and e[1] == 'container-3'][0]
        self.assertEqual(actual[3]['password'], 'r2-password')

    def test_corrupt_direct_restore_is_rejected(self):
        drill = FakeDrill()
        drill.restored = b'changed'
        with self.assertRaisesRegex(RuntimeError, 'bytes differ'):
            provider.restore_archive(drill, 'nas', b'archive')

    def test_wrong_password_network_error_does_not_count_as_encryption_denial(self):
        drill = FakeDrill()
        drill.denial = SimpleNamespace(returncode=1, stderr=b'connection refused')
        with self.assertRaisesRegex(RuntimeError, 'denial not proved'):
            provider.restore_archive(drill, 'nas', b'archive')
        self.assertEqual(drill.count, 2)

    def test_wrong_password_success_is_rejected(self):
        drill = FakeDrill()
        drill.denial = SimpleNamespace(returncode=0, stderr=b'invalid repository password')
        with self.assertRaisesRegex(RuntimeError, 'denial not proved'):
            provider.restore_archive(drill, 'nas', b'archive')

    def test_unsafe_object_id_is_rejected(self):
        drill = FakeDrill()
        drill.object_id = 'bad;command'
        with self.assertRaisesRegex(RuntimeError, 'identifier'):
            provider.restore_archive(drill, 'nas', b'archive')

    def test_generation_r2_input_is_nas_restored_bytes(self):
        drill = FakeDrill()
        archive = b'archive'
        with patch.object(provider, 'restore_archive', side_effect=[
                (archive, {'snapshot_id': 'nas-id', 'archive_sha256': provider.hashlib.sha256(archive).hexdigest()}),
                (archive, {'snapshot_id': 'r2-id', 'archive_sha256': provider.hashlib.sha256(archive).hexdigest()})]) as restore:
            actual, receipt = provider.restore_generation(drill, archive)
        self.assertEqual(actual, archive)
        self.assertEqual(restore.call_args_list[1].args, (drill, 'r2', archive))
        self.assertEqual(receipt['r2']['source_nas_snapshot_id'], 'nas-id')
        self.assertTrue(receipt['synthetic_archive_lineage_qualified'])
        self.assertFalse(receipt['production_replication_qualified'])

    def test_generation_rejects_mismatched_provider_digest(self):
        archive = b'archive'
        digest = provider.hashlib.sha256(archive).hexdigest()
        with patch.object(provider, 'restore_archive', side_effect=[
                (archive, {'snapshot_id': 'nas-id', 'archive_sha256': digest}),
                (archive, {'snapshot_id': 'r2-id', 'archive_sha256': 'wrong'})]):
            with self.assertRaisesRegex(RuntimeError, 'lineage differs'):
                provider.restore_generation(FakeDrill(), archive)

    def test_generation_nas_failure_prevents_r2_creation(self):
        with patch.object(provider, 'restore_archive', side_effect=RuntimeError('NAS failed')) as restore:
            with self.assertRaisesRegex(RuntimeError, 'NAS failed'):
                provider.restore_generation(FakeDrill(), b'archive')
        self.assertEqual(restore.call_count, 1)

    def test_nonsearch_callback_is_rejected_before_runtime(self):
        with self.assertRaises(ValueError):
            provider.fixture('dragonfly', transport=lambda x: x)


if __name__ == '__main__':
    unittest.main()
