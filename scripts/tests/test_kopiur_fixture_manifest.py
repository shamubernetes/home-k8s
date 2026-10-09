"""Filesystem/mocked retirement tests, not daemon cessation qualification."""
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import kopiur_fixture_manifest as controller


class RetirementTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.generation = 'k8s92-nonrel-' + 'a' * 32
        self.endpoint = 'unix:///fixture/generation/docker.sock'
        self.path = Path(directory.name) / 'generation.json'
        self.manifest = controller.GenerationManifest(self.path, self.generation, self.endpoint)
        self.manifest.initialize()
        self.name = self.generation + '-source'
        self.operation = self.manifest.create_intent(self.name)

    def result(self, output=b'', code=0):
        return controller.subprocess.CompletedProcess([], code, stdout=output)

    def responses(self, name=None):
        row = {'Id': 'b' * 64, 'Name': '/' + (name or self.name), 'Config': {'Labels': {
            'kopiur.fixture-generation': self.generation,
            'kopiur.fixture-operation': self.operation}}}
        return [self.result(b'b' * 64), self.result(json.dumps([row]).encode())]

    def test_duplicate_unknown_and_malformed_inventory_denies(self):
        for ids in (['b' * 64, 'b' * 64], ['short']):
            with self.subTest(ids=ids), patch.object(controller.subprocess, 'run',
                    return_value=self.result('\n'.join(ids).encode())):
                with self.assertRaises(RuntimeError):
                    self.manifest.reconcile()
        with patch.object(controller.subprocess, 'run', side_effect=self.responses(
                self.generation + '-unknown')):
            with self.assertRaisesRegex(RuntimeError, 'ownership mismatch'):
                self.manifest.reconcile()
        self.assertTrue(self.manifest.read()['revoked'])
        self.assertIsNone(self.manifest.read()['containers'][self.name]['id'])

    def test_interrupted_inspection_retains_intent(self):
        for error in (OSError('private details'), controller.subprocess.TimeoutExpired('docker', 30)):
            with self.subTest(error=type(error)), patch.object(controller.subprocess, 'run',
                    side_effect=[self.result(b'b' * 64), error]):
                with self.assertRaises((OSError, controller.subprocess.TimeoutExpired)):
                    self.manifest.reconcile()
            self.assertTrue(self.manifest.read()['revoked'])
            self.assertIsNone(self.manifest.read()['containers'][self.name]['id'])

    def test_exact_recovered_id_retirement_never_releases_admission(self):
        with patch.object(controller.subprocess, 'run', side_effect=
                self.responses() + [self.result(), self.result()]) as run:
            receipt = self.manifest.retire()
        self.assertEqual(run.call_args_list[2].args[0],
                         ['docker', '--host', self.endpoint, 'rm', '--force', 'b' * 64])
        self.assertEqual(receipt['removal_acknowledged'], ['b' * 64])
        self.assertTrue(receipt['observed_ids_absent'])
        self.assertFalse(receipt['cessation_proved'])
        self.assertFalse(receipt['admission_release_allowed'])
        self.assertTrue(self.manifest.read()['revoked'])
        self.assertEqual(self.manifest.read()['containers'][self.name]['id'], 'b' * 64)
        for call in run.call_args_list:
            self.assertEqual(call.args[0][:3], ['docker', '--host', self.endpoint])

    def test_every_observed_id_attempted_after_failure(self):
        receipt = {'observed': {'source': 'b' * 64, 'client': 'c' * 64}, 'unobserved': [],
                   'startup_allowed': False, 'cessation_proved': False}
        for error in (OSError('secret'), controller.subprocess.TimeoutExpired('secret', 30),
                      self.result(b'secret', 1)):
            with self.subTest(error=type(error)), patch.object(self.manifest, 'reconcile',
                    return_value=receipt), patch.object(controller.subprocess, 'run',
                    side_effect=[error, self.result(), self.result()]) as run:
                with self.assertRaisesRegex(RuntimeError, '^fixture retirement unresolved$'):
                    self.manifest.retire()
            self.assertEqual(run.call_count, 3)
            self.assertEqual({call.args[0][-1] for call in run.call_args_list[:2]},
                             {'b' * 64, 'c' * 64})

    def test_missing_intent_is_not_cessation(self):
        with patch.object(controller.subprocess, 'run', return_value=self.result()) as run:
            receipt = self.manifest.retire()
        self.assertEqual(run.call_count, 2)
        self.assertEqual(receipt['unobserved'], [self.name])
        self.assertFalse(receipt['cessation_proved'])
        self.assertFalse(receipt['admission_release_allowed'])

    def test_residual_ambiguous_and_failed_inventory_denies(self):
        receipt = {'observed': {'source': 'b' * 64}, 'unobserved': [],
                   'startup_allowed': False, 'cessation_proved': False}
        for result in (self.result(b'b' * 64), self.result(b'short'),
                       self.result((('c' * 64 + '\n') * 2).encode()),
                       self.result(b'secret', 1), self.result(b'\xff'), OSError('secret')):
            with self.subTest(result=type(result)), patch.object(self.manifest, 'reconcile',
                    return_value=receipt), patch.object(controller.subprocess, 'run',
                    side_effect=[self.result(), result]):
                with self.assertRaises(RuntimeError):
                    self.manifest.retire()

    def test_cli_denies_host_before_dispatch(self):
        args = ['reconcile', '--manifest', str(self.path), '--generation', self.generation,
                '--endpoint', self.endpoint]
        with patch.object(sys, 'platform', 'darwin'), \
                patch.object(controller.GenerationManifest, 'reconcile') as reconcile:
            with self.assertRaisesRegex(RuntimeError, 'approved Linux ARC'):
                controller.main(args)
            reconcile.assert_not_called()

    def test_cli_exact_binding_and_approved_arc(self):
        args = ['reconcile', '--manifest', str(self.path), '--generation', self.generation,
                '--endpoint', self.endpoint]
        with patch.object(sys, 'platform', 'linux'), \
                patch.dict(controller.os.environ, {'RUNNER_NAME': 'ghar-set-zoo-fixture'}), \
                patch.object(controller.GenerationManifest, 'reconcile',
                             return_value={'startup_allowed': False}) as reconcile, \
                patch('sys.stdout', new_callable=io.StringIO) as output:
            controller.main(args)
        reconcile.assert_called_once_with()
        self.assertEqual(json.loads(output.getvalue()), {'startup_allowed': False})


class AdmissionTests(unittest.TestCase):
    setUp = RetirementTests.setUp
    result = RetirementTests.result
    responses = RetirementTests.responses

    def running_source(self):
        self.manifest.register(self.name, self.operation, 'b' * 64)
        self.manifest.start_intent(self.name, self.operation, 'b' * 64)
        self.manifest.started(self.name, self.operation, 'b' * 64)

    def test_prior_admission_survives_restart_without_normalization(self):
        for block in (None, 'true', 'false'):
            # Each distinct prior state belongs to a distinct manifest.
            self.setUp()
            self.running_source()
            admission = {'index_uuid': 'x' * 22, 'write_block': block}
            self.manifest.bind_admission(self.name, 'b' * 64, admission)
            restarted = controller.GenerationManifest(self.path, self.generation, self.endpoint)
            restarted.require_admission(self.name, 'b' * 64, admission)
            self.assertEqual(restarted.read()['admissions'][self.name]['write_block'], block)
            restarted.bind_admission(self.name, 'b' * 64, admission)

    def test_binding_rejects_changed_uuid_container_and_prior_admission(self):
        self.running_source()
        admission = {'index_uuid': 'x' * 22, 'write_block': 'true'}
        self.manifest.bind_admission(self.name, 'b' * 64, admission)
        before = self.path.read_bytes()
        for identity, saved in [('c' * 64, admission), ('b' * 64, dict(admission, index_uuid='y' * 22)),
                                ('b' * 64, dict(admission, write_block='false'))]:
            with self.assertRaises(RuntimeError):
                self.manifest.bind_admission(self.name, identity, saved)
            with self.assertRaises(RuntimeError):
                self.manifest.require_admission(self.name, identity, saved)
            self.assertEqual(self.path.read_bytes(), before)

    def test_missing_unstarted_and_revoked_binding_denies(self):
        admission = {'index_uuid': 'x' * 22, 'write_block': None}
        with self.assertRaises(RuntimeError):
            self.manifest.bind_admission(self.name, 'b' * 64, admission)
        self.running_source()
        with self.assertRaises(RuntimeError):
            self.manifest.require_admission(self.name, 'b' * 64, admission)
        self.manifest.bind_admission(self.name, 'b' * 64, admission)
        self.manifest.revoke()
        for method in (self.manifest.bind_admission, self.manifest.require_admission):
            with self.assertRaises(RuntimeError):
                method(self.name, 'b' * 64, admission)
        self.assertEqual(self.manifest.read()['admissions'][self.name]['index_uuid'], 'x' * 22)

    def test_malformed_binding_denies_without_rewriting(self):
        self.running_source()
        admission = {'index_uuid': 'x' * 22, 'write_block': None}
        for invalid in (dict(admission, index_uuid='short'), dict(admission, write_block=False),
                        dict(admission, unknown='field')):
            with self.assertRaises(ValueError):
                self.manifest.bind_admission(self.name, 'b' * 64, invalid)
        self.manifest.bind_admission(self.name, 'b' * 64, admission)
        original = self.manifest.read()
        for field, value in [('index_uuid', 'short'), ('write_block', False),
                             ('container_id', 'c' * 64), ('index_name', 'replacement')]:
            state = json.loads(json.dumps(original))
            state['admissions'][self.name][field] = value
            self.manifest.write(state)
            before = self.path.read_bytes()
            with self.assertRaises(RuntimeError):
                self.manifest.read()
            self.assertEqual(self.path.read_bytes(), before)

    def test_retirement_retains_bound_admission_evidence(self):
        self.running_source()
        admission = {'index_uuid': 'x' * 22, 'write_block': 'true'}
        self.manifest.bind_admission(self.name, 'b' * 64, admission)
        binding = self.manifest.read()['admissions']
        with patch.object(controller.subprocess, 'run', side_effect=
                self.responses() + [self.result(), self.result()]):
            receipt = self.manifest.retire()
        self.assertFalse(receipt['admission_release_allowed'])
        self.assertEqual(self.manifest.read()['admissions'], binding)
        self.assertTrue(self.manifest.read()['revoked'])

    def test_legacy_manifest_retirement_readable_but_no_new_dispatch(self):
        state = self.manifest.read()
        state['version'] = 1
        del state['admissions']
        self.manifest.write(state)
        self.assertEqual(self.manifest.read()['version'], 1)
        with self.assertRaises(RuntimeError):
            self.manifest.create_intent(self.generation + '-client')
        with self.assertRaises(RuntimeError):
            self.manifest.register(self.name, self.operation, 'b' * 64)
        with patch.object(controller.subprocess, 'run', side_effect=
                self.responses() + [self.result(), self.result()]):
            receipt = self.manifest.retire()
        self.assertFalse(receipt['admission_release_allowed'])
        self.assertTrue(self.manifest.read()['revoked'])
        self.assertEqual(self.manifest.read()['containers'][self.name]['id'], 'b' * 64)


if __name__ == '__main__':
    unittest.main()
