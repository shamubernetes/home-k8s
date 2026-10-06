"""Host-only parsing/admission regressions, never native engine proof."""
import io
import json
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import kopiur_nonrel_native as native


class AdmissionTests(unittest.TestCase):
    def test_unknown_service(self):
        with self.assertRaises(ValueError):
            native.Fixture('production')

    def test_host_runtime_denied(self):
        with patch.dict(native.os.environ, {'RUNNER_NAME': ''}):
            with self.assertRaises(RuntimeError):
                native.Fixture('dragonfly')

    def test_other_scale_set_denied(self):
        with patch.dict(native.os.environ, {'RUNNER_NAME': 'ghar-set-maudecode-fixture'}):
            with self.assertRaises(RuntimeError):
                native.Fixture('rabbitmq-server')

    def test_images_are_pinned(self):
        self.assertEqual(set(native.IMAGES), {'dragonfly', 'elasticsearch', 'rabbitmq-server'})
        for image in native.IMAGES.values():
            self.assertRegex(image, r'@sha256:[0-9a-f]{64}$')


class EndpointTests(unittest.TestCase):
    def fixture(self, endpoint=None, required=False):
        with patch.dict(native.os.environ, {'RUNNER_NAME': 'ghar-set-zoo-fixture'}), \
                patch.object(native.sys, 'platform', 'linux'):
            return native.Fixture('elasticsearch', docker_endpoint=endpoint,
                                  require_explicit_endpoint=required)

    def test_required_endpoint_never_defaults_to_ambient(self):
        with self.assertRaisesRegex(ValueError, 'explicit fixture'):
            self.fixture(required=True)
        for endpoint in ('', 'tcp://example.invalid:2375', 'unix://relative',
                         'unix:///tmp/../docker.sock', 'unix:///tmp//docker.sock',
                         'unix:///tmp/./docker.sock', 'unix:///tmp/docker.sock?x',
                         'unix:///tmp/docker.sock#x', 'unix:///tmp/docker%2esock',
                         'unix:///tmp/docker.sock\n', 'unix:///', 42):
            with self.subTest(endpoint=endpoint), self.assertRaises(ValueError):
                self.fixture(endpoint, required=True)

    def test_explicit_host_survives_ambient_changes(self):
        fixture = self.fixture('unix:///fixture/generation/docker.sock', required=True)
        result = native.subprocess.CompletedProcess([], 0, stdout=b'ok')
        with patch.dict(native.os.environ, {'DOCKER_HOST': 'tcp://example.invalid:2375'}), \
                patch.object(native.subprocess, 'run', return_value=result) as run:
            self.assertEqual(fixture.run('info'), b'ok')
        self.assertEqual(run.call_args.args[0],
                         ['docker', '--host', 'unix:///fixture/generation/docker.sock', 'info'])

    def test_endpoint_failure_never_retries_ambient(self):
        fixture = self.fixture('unix:///fixture/generation/docker.sock', required=True)
        for result in (native.subprocess.CompletedProcess([], 1, stdout=b'', stderr=b'secret'),
                       OSError('endpoint absent')):
            with patch.object(native.subprocess, 'run', **(
                    {'side_effect': result} if isinstance(result, OSError) else {'return_value': result})) as run:
                with self.assertRaises((RuntimeError, OSError)):
                    fixture.run('info')
            self.assertEqual(run.call_count, 1)
            self.assertEqual(run.call_args.args[0][1:3],
                             ['--host', 'unix:///fixture/generation/docker.sock'])

    def test_create_and_cleanup_share_bound_endpoint(self):
        fixture = self.fixture('unix:///fixture/generation/docker.sock', required=True)
        created = native.subprocess.CompletedProcess([], 0, stdout=b'a' * 64)
        with patch.object(native.subprocess, 'run', return_value=created) as create, \
                patch.object(fixture, 'run', side_effect=[b'a' * 64, b'']):
            fixture.create('source', 9200)
        self.assertEqual(create.call_args.args[0][:4],
                         ['docker', '--host', fixture.docker_endpoint, 'create'])
        removed = native.subprocess.CompletedProcess([], 0, stdout=b'')
        with patch.object(native.subprocess, 'run', return_value=removed) as cleanup:
            fixture.cleanup()
        self.assertEqual(len(cleanup.call_args_list), 3)
        for call in cleanup.call_args_list:
            self.assertEqual(call.args[0][:3], ['docker', '--host', fixture.docker_endpoint])

    def test_legacy_endpoint_is_explicitly_unqualified(self):
        fixture = self.fixture()
        self.assertEqual(fixture.docker_command('info'), ['docker', 'info'])


class ManifestTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.generation = 'k8s92-nonrel-' + 'a' * 32
        self.endpoint = 'unix:///fixture/generation/docker.sock'
        self.path = Path(self.directory.name) / 'generation.json'
        self.manifest = native.GenerationManifest(self.path, self.generation, self.endpoint)
        self.manifest.initialize()

    def fixture(self):
        with patch.dict(native.os.environ, {'RUNNER_NAME': 'ghar-set-zoo-fixture'}), \
                patch.object(native.sys, 'platform', 'linux'):
            return native.Fixture('elasticsearch', docker_endpoint=self.endpoint,
                                  require_explicit_endpoint=True, generation_manifest=self.manifest)

    def test_restart_retains_intent_and_denies_duplicate_create(self):
        name = self.generation + '-source'
        operation = self.manifest.create_intent(name)
        restarted = native.GenerationManifest(self.path, self.generation, self.endpoint)
        self.assertEqual(restarted.read()['containers'][name],
                         {'operation': operation, 'phase': 'create-intent', 'id': None})
        with self.assertRaises(RuntimeError):
            restarted.create_intent(name)
        with self.assertRaises(RuntimeError):
            self.fixture()
        with self.assertRaises(RuntimeError):
            restarted.initialize()

    def reconciliation_responses(self, name, operation, container_id='b' * 64):
        row = {'Id': container_id, 'Name': '/' + name, 'Config': {'Labels': {
            'kopiur.fixture-generation': self.generation,
            'kopiur.fixture-operation': operation}}}
        return [native.subprocess.CompletedProcess([], 0, stdout=(container_id + '\n').encode()),
                native.subprocess.CompletedProcess([], 0, stdout=json.dumps([row]).encode())]

    def test_reconcile_lost_create_ack_only_recovers_retirement_identity(self):
        name = self.generation + '-source'
        operation = self.manifest.create_intent(name)
        with patch.object(native.subprocess, 'run', side_effect=self.reconciliation_responses(
                name, operation)) as run:
            result = self.manifest.reconcile()
        self.assertEqual(result['observed'], {name: 'b' * 64})
        self.assertFalse(result['startup_allowed'])
        self.assertFalse(result['cessation_proved'])
        self.assertTrue(self.manifest.read()['revoked'])
        self.assertEqual(self.manifest.read()['containers'][name]['id'], 'b' * 64)
        for call in run.call_args_list:
            self.assertEqual(call.args[0][:3], ['docker', '--host', self.endpoint])
        with self.assertRaises(RuntimeError):
            self.manifest.start_intent(name, operation, 'b' * 64)

    def test_reconcile_missing_create_is_not_cessation(self):
        name = self.generation + '-source'
        self.manifest.create_intent(name)
        with patch.object(native.subprocess, 'run', return_value=
                          native.subprocess.CompletedProcess([], 0, stdout=b'')):
            result = self.manifest.reconcile()
        self.assertEqual(result['unobserved'], [name])
        self.assertFalse(result['cessation_proved'])
        self.assertIsNone(self.manifest.read()['containers'][name]['id'])

    def test_reconcile_wrong_operation_or_replaced_id_denies(self):
        name = self.generation + '-source'
        operation = self.manifest.create_intent(name)
        self.manifest.register(name, operation, 'b' * 64)
        for token, container_id in (('c' * 32, 'b' * 64), (operation, 'c' * 64)):
            with self.subTest(token=token, container_id=container_id), \
                    patch.object(native.subprocess, 'run', side_effect=
                                 self.reconciliation_responses(name, token, container_id)):
                with self.assertRaises(RuntimeError):
                    self.manifest.reconcile()
            self.assertEqual(self.manifest.read()['containers'][name]['id'], 'b' * 64)
            self.assertTrue(self.manifest.read()['revoked'])

    def test_reconcile_failure_is_terminal_and_never_falls_back(self):
        with patch.object(native.subprocess, 'run', return_value=
                          native.subprocess.CompletedProcess([], 1, stdout=b'')) as run:
            with self.assertRaises(RuntimeError):
                self.manifest.reconcile()
        self.assertEqual(run.call_count, 1)
        self.assertTrue(self.manifest.read()['revoked'])

    def test_reconcile_manifest_change_during_io_denies_write(self):
        name = self.generation + '-source'
        operation = self.manifest.create_intent(name)
        responses = iter(self.reconciliation_responses(name, operation))
        def query(*args, **kwargs):
            state = self.manifest.read()
            state['containers'][name]['operation'] = 'c' * 32
            self.manifest.write(state)
            return next(responses)
        with patch.object(native.subprocess, 'run', side_effect=query):
            with self.assertRaisesRegex(RuntimeError, 'changed during reconciliation'):
                self.manifest.reconcile()
        self.assertIsNone(self.manifest.read()['containers'][name]['id'])

    def test_durable_identity_and_revocation_never_reset(self):
        name = self.generation + '-source'
        operation = self.manifest.create_intent(name)
        self.manifest.register(name, operation, 'b' * 64)
        self.manifest.revoke()
        restarted = native.GenerationManifest(self.path, self.generation, self.endpoint)
        self.assertTrue(restarted.read()['revoked'])
        self.assertEqual(restarted.read()['containers'][name]['id'], 'b' * 64)
        with self.assertRaises(RuntimeError):
            restarted.start_intent(name, operation, 'b' * 64)
        with self.assertRaises(RuntimeError):
            restarted.create_intent(self.generation + '-client')

    def test_registration_rejects_wrong_operation_and_start_identity(self):
        name = self.generation + '-source'
        operation = self.manifest.create_intent(name)
        with self.assertRaises(RuntimeError):
            self.manifest.register(name, 'b' * 32, 'b' * 64)
        with self.assertRaises(ValueError):
            self.manifest.register(name, operation, 'short')
        self.manifest.register(name, operation, 'b' * 64)
        with self.assertRaises(RuntimeError):
            self.manifest.start_intent(name, operation, 'c' * 64)
        self.manifest.start_intent(name, operation, 'b' * 64)
        with self.assertRaises(RuntimeError):
            self.manifest.start_intent(name, operation, 'b' * 64)

    def test_binding_mismatch_denies_restart(self):
        for generation, endpoint in ((self.generation, 'unix:///other/docker.sock'),
                                     ('k8s92-nonrel-' + 'b' * 32, self.endpoint)):
            with self.assertRaises(RuntimeError):
                native.GenerationManifest(self.path, generation, endpoint).read()

    def test_create_start_only_after_persisted_intents(self):
        fixture = self.fixture()
        name = self.generation + '-source'
        def create(command, **kwargs):
            self.assertEqual(self.manifest.read()['containers'][name]['phase'], 'create-intent')
            self.assertIn('kopiur.fixture-operation=' +
                          self.manifest.read()['containers'][name]['operation'], command)
            return native.subprocess.CompletedProcess([], 0, stdout=b'b' * 64)
        def run(*args, **kwargs):
            if args[0] == 'inspect':
                return b'b' * 64
            self.assertEqual(args, ('start', 'b' * 64))
            self.assertEqual(self.manifest.read()['containers'][name]['phase'], 'start-intent')
            return b''
        with patch.object(native.subprocess, 'run', side_effect=create), \
                patch.object(fixture, 'run', side_effect=run):
            fixture.create('source', 9200)
        self.assertEqual(self.manifest.read()['containers'][name]['phase'], 'running')

    def test_lost_create_acknowledgement_retains_intent_without_start(self):
        fixture = self.fixture()
        with patch.object(native.subprocess, 'run', side_effect=OSError('ack lost')), \
                patch.object(fixture, 'run') as run:
            with self.assertRaises(OSError):
                fixture.create('source', 9200)
        run.assert_not_called()
        self.assertEqual(self.manifest.read()['containers'][self.generation + '-source']['phase'],
                         'create-intent')

    def test_intent_fsync_failure_denies_dispatch(self):
        fixture = self.fixture()
        with patch.object(native.os, 'fsync', side_effect=OSError('fsync failed')), \
                patch.object(native.subprocess, 'run') as run:
            with self.assertRaises(OSError):
                fixture.create('source', 9200)
        run.assert_not_called()

    def test_client_create_start_intents(self):
        fixture = self.fixture()
        name = self.generation + '-client'
        def run(*args, **kwargs):
            if args[0] in ('create', 'inspect'):
                self.assertEqual(self.manifest.read()['containers'][name]['phase'], 'create-intent')
                return b'b' * 64
            if args[0] == 'start':
                self.assertEqual(self.manifest.read()['containers'][name]['phase'], 'start-intent')
                return b''
            self.assertEqual(self.manifest.read()['containers'][name]['phase'], 'running')
            return b'{}'
        with patch.object(fixture, 'run', side_effect=run):
            fixture.client_request({})
        self.assertEqual(self.manifest.read()['containers'][name]['id'], 'b' * 64)

    def test_revoked_generation_denies_real_dispatch(self):
        fixture = self.fixture()
        self.manifest.revoke()
        with patch.object(native.subprocess, 'run') as run:
            with self.assertRaises(RuntimeError):
                fixture.run('exec', 'b' * 64, 'true')
        run.assert_not_called()

    def test_revocation_after_server_intent_denies_create_dispatch(self):
        fixture = self.fixture()
        original = self.manifest.create_intent
        def intent(name):
            operation = original(name)
            self.manifest.revoke()
            return operation
        with patch.object(self.manifest, 'create_intent', side_effect=intent), \
                patch.object(native.subprocess, 'run') as run:
            with self.assertRaises(RuntimeError):
                fixture.create('source', 9200)
        run.assert_not_called()

    def test_duplicate_fields_deny_restart_and_dispatch(self):
        fixture = self.fixture()
        state = self.path.read_text()
        for malformed in (state.replace('"revoked": false', '"revoked": true, "revoked": false'),
                          state.replace('"containers": {}', '"containers": {}, "containers": {}')):
            self.path.write_text(malformed)
            with patch.object(native.subprocess, 'run') as run:
                with self.assertRaisesRegex(RuntimeError, 'duplicate'):
                    fixture.run('info')
            run.assert_not_called()
            with self.assertRaisesRegex(RuntimeError, 'duplicate'):
                self.fixture()

    def test_manifest_backed_client_create_ack_loss_denies_retry(self):
        fixture = self.fixture()
        with patch.object(native.subprocess, 'run', side_effect=OSError('ack lost')):
            with self.assertRaises(OSError):
                fixture.client_request({})
        self.assertEqual(self.manifest.read()['containers'][self.generation + '-client']['phase'],
                         'create-intent')
        with patch.object(native.subprocess, 'run') as run:
            with self.assertRaises(RuntimeError):
                fixture.client_request({})
        run.assert_not_called()
        with self.assertRaises(RuntimeError):
            self.fixture()

    def test_manifest_backed_server_start_ack_loss_retains_start_intent(self):
        fixture = self.fixture()
        responses = [native.subprocess.CompletedProcess([], 0, stdout=b'b' * 64),
                     native.subprocess.CompletedProcess([], 0, stdout=b'b' * 64),
                     OSError('start ack lost')]
        with patch.object(native.subprocess, 'run', side_effect=responses):
            with self.assertRaises(OSError):
                fixture.create('source', 9200)
        self.assertEqual(self.manifest.read()['containers'][self.generation + '-source']['phase'],
                         'start-intent')
        with patch.object(native.subprocess, 'run') as run:
            with self.assertRaises(RuntimeError):
                fixture.create('source', 9200)
        run.assert_not_called()
        with self.assertRaises(RuntimeError):
            self.fixture()

    def test_manifest_backed_client_start_ack_loss_denies_exec(self):
        fixture = self.fixture()
        responses = [native.subprocess.CompletedProcess([], 0, stdout=b'b' * 64),
                     native.subprocess.CompletedProcess([], 0, stdout=b'b' * 64),
                     OSError('start ack lost')]
        with patch.object(native.subprocess, 'run', side_effect=responses):
            with self.assertRaises(OSError):
                fixture.client_request({})
        self.assertEqual(self.manifest.read()['containers'][self.generation + '-client']['phase'],
                         'start-intent')
        with patch.object(native.subprocess, 'run') as run:
            with self.assertRaises(RuntimeError):
                fixture.client_request({})
        run.assert_not_called()
        with self.assertRaises(RuntimeError):
            self.fixture()


class ServerStartupTests(unittest.TestCase):
    def fixture(self):
        fixture = native.Fixture.__new__(native.Fixture)
        fixture.docker_endpoint = None
        fixture.generation_manifest = None
        fixture.service = 'elasticsearch'
        fixture.prefix = 'owned'
        fixture.network = 'owned-network'
        fixture.containers = []
        fixture.container_ids = {}
        fixture.deadline = native.time.monotonic() + 60
        fixture.client = None
        fixture.client_started = False
        fixture.host = 'owned-source'
        return fixture

    def test_source_archive_and_start_use_registered_id(self):
        fixture = self.fixture()
        created = native.subprocess.CompletedProcess([], 0, stdout=b'a' * 64 + b'\n')
        with patch.object(native.subprocess, 'run', return_value=created), \
                patch.object(native, 'validate_archive'), \
                patch.object(fixture, 'run', side_effect=[b'a' * 64, b'', b'']) as run:
            self.assertEqual(fixture.create('source', 9200, archive=b'archive', path='/data'),
                             ('owned-source', 9200))
        self.assertEqual([call.args for call in run.call_args_list], [
            ('inspect', '--format', '{{.Id}}', 'owned-source'),
            ('cp', '-a', '-', 'a' * 64 + ':/data'), ('start', 'a' * 64)])
        self.assertEqual(fixture.container_ids['owned-source'], 'a' * 64)

    def test_source_missing_or_replaced_identity_never_starts(self):
        for created_id, observed in ((b'invalid', b'a' * 64), (b'a' * 64, b'b' * 64)):
            fixture = self.fixture()
            created = native.subprocess.CompletedProcess([], 0, stdout=created_id)
            with patch.object(native.subprocess, 'run', return_value=created), \
                    patch.object(fixture, 'run', return_value=observed) as run:
                with self.assertRaises(RuntimeError):
                    fixture.create('source', 9200)
            self.assertTrue(all(call.args[0] == 'inspect' for call in run.call_args_list))

    def test_client_registered_stopped_before_exact_start_and_exec(self):
        fixture = self.fixture()
        def response(*args, **kwargs):
            if args[0] in ('create', 'inspect'):
                self.assertFalse(fixture.client_started)
                return b'a' * 64
            self.assertEqual(fixture.container_ids, {'owned-client': 'a' * 64})
            self.assertEqual(fixture.containers, ['owned-client'])
            self.assertEqual(fixture.client_started, args[0] == 'exec')
            return b'{}' if args[0] == 'exec' else b''
        with patch.object(fixture, 'run', side_effect=response) as run:
            self.assertEqual(fixture.client_request({'operation': 'http'}), {})
        calls = [call.args for call in run.call_args_list]
        self.assertEqual(calls[0][0], 'create')
        self.assertNotIn('-d', calls[0])
        self.assertEqual(calls[1], ('inspect', '--format', '{{.Id}}', 'owned-client'))
        self.assertEqual(calls[2], ('start', 'a' * 64))
        self.assertEqual(calls[3][:3], ('exec', '-i', 'a' * 64))
        self.assertTrue(fixture.client_started)

    def test_client_bad_or_replaced_identity_never_starts_or_executes(self):
        for responses in ([b'invalid'], [b'a' * 64, b'b' * 64]):
            fixture = self.fixture()
            with patch.object(fixture, 'run', side_effect=responses) as run:
                with self.assertRaises(RuntimeError):
                    fixture.client_request({})
            self.assertTrue(all(call.args[0] in ('create', 'inspect') for call in run.call_args_list))
            with patch.object(fixture, 'run') as run:
                with self.assertRaisesRegex(RuntimeError, 'startup remains unresolved'):
                    fixture.client_request({})
                run.assert_not_called()

    def test_client_lost_start_acknowledgement_denies_retry_exec(self):
        fixture = self.fixture()
        with patch.object(fixture, 'run', side_effect=[b'a' * 64, b'a' * 64,
                                                     RuntimeError('start acknowledgement lost')]):
            with self.assertRaisesRegex(RuntimeError, 'acknowledgement lost'):
                fixture.client_request({})
        self.assertEqual(fixture.container_ids, {'owned-client': 'a' * 64})
        with patch.object(fixture, 'run') as run:
            with self.assertRaisesRegex(RuntimeError, 'startup remains unresolved'):
                fixture.client_request({})
            run.assert_not_called()

    def test_client_create_or_inspect_failure_denies_retry(self):
        for responses in ([RuntimeError('create failed')],
                          [b'a' * 64, RuntimeError('inspect failed')]):
            fixture = self.fixture()
            with patch.object(fixture, 'run', side_effect=responses) as run:
                with self.assertRaises(RuntimeError):
                    fixture.client_request({})
            self.assertTrue(all(call.args[0] in ('create', 'inspect') for call in run.call_args_list))
            with patch.object(fixture, 'run') as run:
                with self.assertRaisesRegex(RuntimeError, 'startup remains unresolved'):
                    fixture.client_request({})
                run.assert_not_called()

    def test_source_disappearing_after_identity_check_never_falls_back(self):
        created = native.subprocess.CompletedProcess([], 0, stdout=b'a' * 64)
        for responses in ([b'a' * 64, RuntimeError('copy target absent')],
                          [b'a' * 64, b'', RuntimeError('start target absent')]):
            fixture = self.fixture()
            with patch.object(native.subprocess, 'run', return_value=created), \
                    patch.object(native, 'validate_archive'), \
                    patch.object(fixture, 'run', side_effect=responses) as run:
                with self.assertRaisesRegex(RuntimeError, 'target absent'):
                    fixture.create('source', 9200, archive=b'archive', path='/data')
            self.assertEqual(run.call_args_list[1].args, ('cp', '-a', '-', 'a' * 64 + ':/data'))
            if len(responses) == 3:
                self.assertEqual(run.call_args_list[2].args, ('start', 'a' * 64))
            self.assertEqual(fixture.container_ids, {'owned-source': 'a' * 64})

    def test_existing_client_exec_uses_original_id(self):
        fixture = self.fixture()
        fixture.client = 'owned-client'
        fixture.client_started = True
        fixture.containers = ['owned-client']
        fixture.container_ids = {'owned-client': 'a' * 64}
        with patch.object(fixture, 'run', return_value=b'{}') as run:
            fixture.client_request({})
        self.assertEqual(run.call_args.args[:3], ('exec', '-i', 'a' * 64))
        fixture.container_ids.clear()
        with patch.object(fixture, 'run') as run:
            with self.assertRaisesRegex(RuntimeError, 'lacks registered'):
                fixture.client_request({})
            run.assert_not_called()

    def test_readiness_inspects_original_id_not_reused_name(self):
        fixture = self.fixture()
        fixture.containers = ['owned-source']
        fixture.container_ids = {'owned-source': 'a' * 64}
        with patch.object(fixture, 'run', return_value=b'{"Status":"running"}') as run:
            fixture.ready('owned-source', lambda: True)
        run.assert_called_once_with('inspect', '--format', '{{json .State}}', 'a' * 64)
        fixture.container_ids.clear()
        with patch.object(fixture, 'run') as run:
            with self.assertRaisesRegex(RuntimeError, 'lacks registered'):
                fixture.ready('owned-source', lambda: True)
            run.assert_not_called()


class ServerRetirementTests(unittest.TestCase):
    def fixture(self):
        fixture = native.Fixture.__new__(native.Fixture)
        fixture.docker_endpoint = None
        fixture.generation_manifest = None
        fixture.containers = ['owned-source']
        fixture.container_ids = {'owned-source': 'a' * 64}
        return fixture

    def test_exact_server_removal_read_back(self):
        fixture = self.fixture()
        with patch.object(fixture, 'run', side_effect=[b'a' * 64 + b'\n', b'', b'b' * 64 + b'\n']) as run:
            proof = fixture.remove('owned-source')
        self.assertEqual([call.args for call in run.call_args_list], [
            ('inspect', '--format', '{{.Id}}', 'owned-source'),
            ('rm', '-fv', 'a' * 64),
            ('ps', '-a', '--no-trunc', '--format', '{{.ID}}')])
        self.assertEqual(proof, {'container_id': 'a' * 64, 'daemon_inventory_absent': True,
                                'production_mutation_cessation_qualified': False})
        self.assertEqual(fixture.containers, [])
        self.assertEqual(fixture.container_ids, {})

    def test_replaced_name_never_removed(self):
        fixture = self.fixture()
        with patch.object(fixture, 'run', return_value=b'b' * 64) as run:
            with self.assertRaisesRegex(RuntimeError, 'identity changed'):
                fixture.remove('owned-source')
        self.assertEqual(run.call_count, 1)
        self.assertEqual(fixture.containers, ['owned-source'])

    def test_server_still_present_denies_restore_proof(self):
        fixture = self.fixture()
        with patch.object(fixture, 'run', side_effect=[b'a' * 64, b'', b'a' * 64 + b'\n']):
            with self.assertRaisesRegex(RuntimeError, 'remains after removal'):
                fixture.remove('owned-source')
        self.assertEqual(fixture.container_ids, {'owned-source': 'a' * 64})
        self.assertEqual(fixture.containers, ['owned-source'])

    def test_unregistered_server_denied(self):
        fixture = self.fixture()
        fixture.container_ids.clear()
        with patch.object(fixture, 'run') as run:
            with self.assertRaisesRegex(RuntimeError, 'identity changed'):
                fixture.remove('owned-source')
        run.assert_not_called()

    def test_inventory_failure_never_becomes_absence(self):
        fixture = self.fixture()
        with patch.object(fixture, 'run', side_effect=[b'a' * 64, b'', RuntimeError('daemon unavailable')]):
            with self.assertRaisesRegex(RuntimeError, 'daemon unavailable'):
                fixture.remove('owned-source')
        self.assertEqual(fixture.container_ids, {'owned-source': 'a' * 64})


    def test_lost_removal_acknowledgement_remains_unresolved(self):
        fixture = self.fixture()
        with patch.object(fixture, 'run', side_effect=[b'a' * 64, RuntimeError('ack lost')]) as run:
            with self.assertRaisesRegex(RuntimeError, 'ack lost'):
                fixture.remove('owned-source')
        self.assertEqual(run.call_count, 2)
        self.assertEqual(fixture.containers, ['owned-source'])

    def test_cleanup_never_removes_by_replaced_name(self):
        fixture = self.fixture()
        fixture.network = 'owned-network'
        with patch.object(native.subprocess, 'run', return_value=native.subprocess.CompletedProcess([], 0, stdout=b'')) as run:
            fixture.cleanup()
        self.assertEqual([call.args[0] for call in run.call_args_list], [
            ['docker', 'rm', '-fv', 'a' * 64],
            ['docker', 'ps', '-a', '--no-trunc', '--format', '{{.ID}}'],
            ['docker', 'network', 'rm', 'owned-network']])
        self.assertEqual(fixture.container_ids, {})

    def test_cleanup_missing_identity_never_falls_back_to_name(self):
        fixture = self.fixture()
        fixture.network = 'owned-network'
        fixture.container_ids.clear()
        with patch.object(native.subprocess, 'run') as run:
            with self.assertRaisesRegex(RuntimeError, 'lacks immutable'):
                fixture.cleanup()
        self.assertEqual([call.args[0] for call in run.call_args_list], [
            ['docker', 'network', 'rm', 'owned-network']])
        self.assertEqual(fixture.containers, ['owned-source'])

    def test_cleanup_missing_identity_does_not_skip_known_servers(self):
        fixture = self.fixture()
        fixture.network = 'owned-network'
        fixture.containers.insert(0, 'unresolved')
        with patch.object(native.subprocess, 'run', return_value=native.subprocess.CompletedProcess([], 0, stdout=b'')) as run:
            with self.assertRaisesRegex(RuntimeError, 'lacks immutable'):
                fixture.cleanup()
        self.assertEqual([call.args[0] for call in run.call_args_list], [
            ['docker', 'rm', '-fv', 'a' * 64],
            ['docker', 'ps', '-a', '--no-trunc', '--format', '{{.ID}}'],
            ['docker', 'network', 'rm', 'owned-network']])
        self.assertEqual(fixture.containers, ['unresolved'])
        self.assertEqual(fixture.container_ids, {})

    def test_cleanup_failure_does_not_skip_other_servers_and_suppresses_output(self):
        fixture = self.fixture()
        fixture.network = 'owned-network'
        fixture.containers.append('second')
        fixture.container_ids['second'] = 'b' * 64
        success = native.subprocess.CompletedProcess([], 0, stdout=b'')
        failure = native.subprocess.CalledProcessError(1, ['docker'], output=b'fixture-secret')
        for error in (failure, native.subprocess.TimeoutExpired(['docker'], 60), OSError('daemon unavailable')):
            with self.subTest(error=type(error).__name__):
                fixture.containers = ['owned-source', 'second']
                fixture.container_ids = {'owned-source': 'a' * 64, 'second': 'b' * 64}
                with patch.object(native.subprocess, 'run', side_effect=[error, success, success, failure]) as run:
                    with self.assertRaisesRegex(RuntimeError, 'exact server retirement unresolved') as caught:
                        fixture.cleanup()
                self.assertNotIn('fixture-secret', str(caught.exception))
                self.assertEqual([call.args[0] for call in run.call_args_list], [
                    ['docker', 'rm', '-fv', 'a' * 64],
                    ['docker', 'rm', '-fv', 'b' * 64],
                    ['docker', 'ps', '-a', '--no-trunc', '--format', '{{.ID}}'],
                    ['docker', 'network', 'rm', 'owned-network']])
                self.assertEqual(fixture.containers, ['owned-source'])
                self.assertEqual(fixture.container_ids, {'owned-source': 'a' * 64})

    def test_cleanup_inventory_failure_or_residual_server_remains_unresolved(self):
        for inventory in (native.subprocess.CompletedProcess([], 0, stdout=b'a' * 64 + b'\n'),
                          OSError('daemon unavailable'),
                          native.subprocess.CompletedProcess([], 0, stdout=b'\xff')):
            fixture = self.fixture()
            fixture.network = 'owned-network'
            success = native.subprocess.CompletedProcess([], 0, stdout=b'')
            with patch.object(native.subprocess, 'run', side_effect=[success, inventory, success]):
                with self.assertRaisesRegex(RuntimeError, 'exact server retirement unresolved'):
                    fixture.cleanup()
            self.assertEqual(fixture.containers, ['owned-source'])
            self.assertEqual(fixture.container_ids, {'owned-source': 'a' * 64})


class ElasticsearchAdmissionTests(unittest.TestCase):
    def test_prior_setting_preserved(self):
        for block in (None, 'false', 'true'):
            values = {'index.uuid': 'original-index'}
            if block is not None:
                values['index.blocks.write'] = block
            self.assertEqual(native.elasticsearch_admission({'fixture': {'settings': values}}),
                             {'index_uuid': 'original-index', 'write_block': block})

    def test_incomplete_or_ambiguous_observation_denied(self):
        for settings in (None, {}, {'alias': {'settings': {'index.uuid': 'original'}}},
                         {'fixture': None}, {'fixture': {'settings': {}}},
                         {'fixture': {'settings': {'index.uuid': ''}}},
                         {'fixture': {'settings': {'index.uuid': True}}},
                         {'fixture': {}, 'other': {}}):
            with self.subTest(settings=settings), self.assertRaises(RuntimeError):
                native.elasticsearch_admission(settings)

    def test_malformed_block_denied(self):
        for block in (None, True, False, 0, 1, 'TRUE', '', [], {}):
            with self.subTest(block=block), self.assertRaises(RuntimeError):
                native.elasticsearch_admission({'fixture': {'settings': {
                    'index.uuid': 'original', 'index.blocks.write': block}}})


class ElasticsearchRestoreTests(unittest.TestCase):
    def test_complete_restore(self):
        self.assertEqual(native.validate_elasticsearch_restore({
            'snapshot': {'indices': ['fixture'],
                         'shards': {'total': 1, 'successful': 1, 'failed': 0}}}), 1)

    def test_partial_or_missing_restore_denied(self):
        for shards in ({}, {'total': 0, 'successful': 0, 'failed': 0},
                       {'total': 2, 'successful': 1, 'failed': 1},
                       {'total': 2, 'successful': 1, 'failed': 0},
                       {'total': True, 'successful': True, 'failed': 0},
                       {'total': 1, 'successful': 1, 'failed': False}):
            with self.subTest(shards=shards), self.assertRaises(RuntimeError):
                native.validate_elasticsearch_restore({'snapshot': {'indices': ['fixture'], 'shards': shards}})
        with self.assertRaises(RuntimeError):
            native.validate_elasticsearch_restore({})

    def test_unexpected_indices_denied(self):
        with self.assertRaises(RuntimeError):
            native.validate_elasticsearch_restore({'snapshot': {
                'indices': ['other'], 'shards': {'total': 1, 'successful': 1, 'failed': 0}}})

    def test_security_feature_shards_counted(self):
        self.assertEqual(native.validate_elasticsearch_restore({'snapshot': {
            'indices': ['fixture', '.security-7'],
            'shards': {'total': 2, 'successful': 2, 'failed': 0}}}), 2)
        for indices in (['fixture', 'other'], ['fixture', None], 'fixture'):
            with self.subTest(indices=indices), self.assertRaises(RuntimeError):
                native.validate_elasticsearch_restore({'snapshot': {
                    'indices': indices, 'shards': {'total': 2, 'successful': 2, 'failed': 0}}})


class RespTests(unittest.TestCase):
    def test_binary_bulk(self):
        self.assertEqual(native.resp_read(io.BytesIO(b'$3\r\n\x00\xffx\r\n')), b'\x00\xffx')

    def test_nested_array(self):
        self.assertEqual(native.resp_read(io.BytesIO(b'*3\r\n+OK\r\n:2\r\n$-1\r\n')), [b'OK', 2, None])

    def test_server_error_suppresses_content(self):
        with self.assertRaises(RuntimeError) as caught:
            native.resp_read(io.BytesIO(b'-fixture-password\r\n'))
        self.assertNotIn('fixture-password', str(caught.exception))

    def test_invalid_frames(self):
        frames = (b'$-2\r\n', b'$1048577\r\n', b'*4097\r\n', b'$3\r\nx\r\n',
                  b'$1\r\nxzz', b'hello\r\n', b'+truncated', b'*' + b'1\r\n*' * 17 + b'0\r\n')
        for frame in frames:
            with self.subTest(frame=frame[:20]), self.assertRaises(ValueError):
                native.resp_read(io.BytesIO(frame))


class ArchiveTests(unittest.TestCase):
    def archive(self, entries):
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode='w') as archive:
            for name, kind in entries:
                member = tarfile.TarInfo(name)
                member.type = kind
                member.linkname = '/outside' if kind in (tarfile.SYMTYPE, tarfile.LNKTYPE) else ''
                if kind == tarfile.REGTYPE:
                    member.size = 3
                    archive.addfile(member, io.BytesIO(b'abc'))
                else:
                    archive.addfile(member)
        return stream.getvalue()

    def test_regular_archive(self):
        self.assertRegex(native.validate_archive(self.archive([('fixture', tarfile.REGTYPE)])), r'^[0-9a-f]{64}$')

    def test_unsafe_members(self):
        entries = ([('/outside', tarfile.REGTYPE)], [('../outside', tarfile.REGTYPE)],
                   [('link', tarfile.SYMTYPE)], [('link', tarfile.LNKTYPE)],
                   [('pipe', tarfile.FIFOTYPE)], [('duplicate', tarfile.REGTYPE)] * 2)
        for members in entries:
            with self.subTest(members=members), self.assertRaises(ValueError):
                native.validate_archive(self.archive(members))

    def test_empty_archive(self):
        with self.assertRaises(ValueError):
            native.validate_archive(self.archive([]))

    def test_repository_envelope(self):
        data = self.archive([('.', tarfile.DIRTYPE), ('./index-0', tarfile.REGTYPE)])
        wrapped = native.repository_envelope(data)
        with tarfile.open(fileobj=io.BytesIO(wrapped), mode='r:') as archive:
            self.assertEqual(archive.getnames(), ['snapshot', 'snapshot/index-0'])
            self.assertEqual(archive.extractfile('snapshot/index-0').read(), b'abc')
            self.assertTrue(archive.getmember('snapshot').isdir())

    def test_archive_size_bound(self):
        with patch.object(native, 'MAX_ARCHIVE', 1):
            with self.assertRaises(ValueError):
                native.validate_archive(b'xx')


if __name__ == '__main__':
    unittest.main()
