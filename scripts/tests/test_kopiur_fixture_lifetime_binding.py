"""Binding regressions; mocked checks are NOT kernel/daemon qualification."""
import copy
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from kopiur_fixture_controller import FixtureController
from kopiur_fixture_manifest import GenerationManifest
from kopiur_fixture_supervisor import FixtureSupervisor, BoundaryError
import kopiur_nonrel_native as native


class LifetimeBindingTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / 'manifest.json'
        self.generation = 'k8s92-nonrel-' + 'a' * 32
        self.endpoint = 'unix:///fixture/docker.sock'
        self.state = {'generation': '11111111-1111-4111-8111-111111111111',
                      'root': '/sys/fs/cgroup', 'boundary': {'device': 1, 'inode': 2, 'boot': 'boot'},
                      'revoked': False}
        self.supervisor = Mock(spec=FixtureSupervisor)
        self.supervisor.directory = Path(directory.name)
        self.supervisor._read.side_effect = lambda: (copy.deepcopy(self.state), None)
        self.supervisor._watchdog_revoked.return_value = False
        self.manifest = GenerationManifest(self.path, self.generation, self.endpoint,
                                           supervisor=self.supervisor)
        self.manifest.initialize()

    def fixture(self):
        with patch.dict(native.os.environ, {'RUNNER_NAME': 'ghar-set-zoo-fixture'}), \
                patch.object(native.sys, 'platform', 'linux'):
            return native.Fixture('elasticsearch', docker_endpoint=self.endpoint,
                                  generation_manifest=self.manifest)

    def test_tombstone_denies_existing_handle_before_native_io(self):
        fixture = self.fixture()
        self.supervisor._watchdog_revoked.return_value = True
        calls = [lambda: fixture.run('info'), lambda: fixture.create('source', 9200),
                 lambda: fixture.http(9200, '/fixture'), lambda: fixture.redis(6379, 'PING'),
                 lambda: fixture.client_request({})]
        with patch.object(native.subprocess, 'run') as run:
            for call in calls:
                with self.assertRaisesRegex(RuntimeError, 'lifetime revoked'):
                    call()
            run.assert_not_called()
        self.assertFalse(self.manifest.read()['revoked'])
        self.assertEqual(self.manifest.read()['containers'], {})

    def test_missing_changed_and_unreadable_supervisor_deny(self):
        restarted = GenerationManifest(self.path, self.generation, self.endpoint)
        with self.assertRaisesRegex(RuntimeError, 'independent supervisor'):
            restarted.require_dispatch()
        self.state['boundary']['inode'] += 1
        with self.assertRaisesRegex(RuntimeError, 'binding changed'):
            self.manifest.require_dispatch()
        self.supervisor._read.side_effect = OSError('unreadable')
        with self.assertRaises(OSError):
            self.manifest.require_dispatch()

    def test_legacy_cannot_silently_enter_bound_path(self):
        state = self.manifest.read()
        del state['supervisor']
        self.manifest.write(state)
        with self.assertRaisesRegex(RuntimeError, 'binding changed'):
            self.manifest.require_dispatch()
        with self.assertRaisesRegex(RuntimeError, 'binding changed'):
            FixtureController(self.supervisor, generation_manifest=self.manifest)
        self.assertEqual(self.manifest.read(), state)

    def test_recovery_tombstone_precedes_manifest_lock_failure(self):
        controller = FixtureController(self.supervisor, generation_manifest=self.manifest)
        def cease():
            self.supervisor._watchdog_revoked.return_value = True
        self.supervisor.watchdog_cease.side_effect = cease
        with patch.object(self.manifest, 'transaction', side_effect=BlockingIOError('held')):
            with self.assertRaises(BlockingIOError):
                controller.recover()
        self.supervisor.cease.assert_not_called()
        self.supervisor._locked.assert_not_called()
        with self.assertRaisesRegex(RuntimeError, 'lifetime revoked'):
            self.manifest.require_dispatch()

    def test_manifest_durably_revoked_before_supervisor_lock_failure(self):
        controller = FixtureController(self.supervisor, generation_manifest=self.manifest)
        self.supervisor.cease.side_effect = BoundaryError('owner lock held')
        with self.assertRaises(BoundaryError):
            controller.recover()
        self.supervisor.watchdog_cease.assert_called_once_with()
        restarted = GenerationManifest(self.path, self.generation, self.endpoint)
        self.assertTrue(restarted.read()['revoked'])

    def test_recovery_identity_mismatch_never_ceases_other_boundary(self):
        controller = FixtureController(self.supervisor, generation_manifest=self.manifest)
        self.state['generation'] = '22222222-2222-4222-8222-222222222222'
        with self.assertRaisesRegex(RuntimeError, 'binding changed'):
            controller.recover()
        self.supervisor.watchdog_cease.assert_not_called()
        self.supervisor._locked.assert_not_called()

    def test_distinct_prior_admission_preserved_on_bound_path(self):
        for number, block in enumerate((None, 'false', 'true')):
            name = self.generation + '-source-' + str(number)
            operation = self.manifest.create_intent(name)
            container_id = str(number + 1) * 64
            self.manifest.register(name, operation, container_id)
            self.manifest.start_intent(name, operation, container_id)
            self.manifest.started(name, operation, container_id)
            admission = {'index_uuid': 'x' * 22, 'write_block': block}
            self.manifest.bind_admission(name, container_id, admission)
            self.manifest.require_admission(name, container_id, admission)
            self.assertEqual(self.manifest.read()['admissions'][name]['write_block'], block)


if __name__ == '__main__':
    unittest.main()
