"""Convex native fixture regressions for authorized ARC."""
import copy
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from urllib.parse import urlparse
import unittest
from unittest.mock import Mock, patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import kopiur_convex_native as convex


class ConvexTests(unittest.TestCase):
    def setUp(self):
        self.scope = convex.contract()
        self.drill = self.scope['DockerDrill']('convex')

    def test_deployment_pinned_postgresql_contract(self):
        self.assertEqual(self.drill.databases, ['convex'])
        self.assertEqual(self.drill.image, convex.IMAGE)
        self.assertEqual(self.drill.port, 3210)

    def test_snapshot_is_complete_and_preserves_original_document(self):
        value = {'hasMore': False, 'values': [{'_id': 'original-document',
            '_table': convex.TABLE, '_creationTime': 1, '_ts': 2, 'value': 'native-recovery-proof'}]}
        self.assertEqual(convex.stable_snapshot(value), value['values'])
        for mutate in (lambda v: v.update(hasMore=True), lambda v: v.update(values=[]),
                       lambda v: v['values'][0].update(value='changed')):
            changed = copy.deepcopy(value)
            mutate(changed)
            with self.assertRaises(ValueError):
                convex.stable_snapshot(changed)

    def test_original_admin_key_and_instance_secret_preserved(self):
        raw = json.dumps({'original_fixture_key': 'original-fixture-admin-key', 'auth_secret': 'a' * 64}).encode()
        self.assertEqual(self.drill.isolated_config(raw), raw)
        for bad in (b'{}', b'null'):
            with self.assertRaises(ValueError):
                self.drill.isolated_config(bad)

    def test_restored_process_does_not_generate_new_identity_or_select_rgw(self):
        identity = json.dumps({'original_fixture_key': 'original-fixture-admin-key', 'auth_secret': 'a' * 64}).encode()
        invoke = Mock(return_value=SimpleNamespace(stdout=identity))
        with patch.dict(self.scope, run=invoke):
            with patch.object(self.drill, 'start', return_value='restored') as start:
                self.assertEqual(self.drill.app('restored-app', 'database', 'config'), 'restored')
        self.assertEqual(invoke.call_count, 2)
        self.assertIn('chown 0:568 /config', invoke.call_args.args[-1])
        self.assertIn('chown -R 568:568 /config/.kopiur-postgres', invoke.call_args.args[-1])
        self.assertIn('chmod 0700 /config/.kopiur-postgres', invoke.call_args.args[-1])
        self.assertIn('--cap-add=DAC_READ_SEARCH', invoke.call_args.args)
        self.assertNotIn('--cap-add=DAC_OVERRIDE', invoke.call_args.args)
        self.assertNotIn('generate_admin_key', ' '.join(invoke.call_args.args))
        env = start.call_args.kwargs['env']
        self.assertEqual(env['INSTANCE_SECRET'], 'a' * 64)
        self.assertEqual(urlparse(env['POSTGRES_URL']).path, '')
        self.assertFalse(any(key.startswith(('S3_', 'AWS_')) for key in env))
        self.assertEqual(start.call_args.kwargs['network'], 'container:database')
        self.assertEqual(start.call_args.kwargs['user'], '0:0')

    def test_launch_diagnostics_do_not_print_fixture_credentials(self):
        key = 'original-private-fixture-admin-key'
        secret = 'a' * 64
        raw = json.dumps({'original_fixture_key': key, 'auth_secret': secret}).encode()
        invoke = Mock(side_effect=[SimpleNamespace(stdout=raw), SimpleNamespace(stdout=b''), SimpleNamespace(stdout=b'0\n'),
            SimpleNamespace(stdout=b'750 0 0 /convex/run_backend.sh\n')])
        with patch.dict(self.scope, run=invoke):
            with patch.object(self.drill, 'start', side_effect=RuntimeError('isolated launch failed')):
                with self.assertRaises(RuntimeError) as failure:
                    self.drill.app('restored-app', 'database', 'config')
        self.assertIn('750 0 0', str(failure.exception))
        self.assertNotIn(key, str(failure.exception))
        self.assertNotIn(secret, str(failure.exception))

    def test_source_identity_is_group_readable_not_world_readable(self):
        self.drill.auth_secret = 'a' * 64
        invoke = Mock(return_value=SimpleNamespace(stdout=b'original-admin-key'))
        responses = [SimpleNamespace(returncode=0, stdout=b'{}'),
            SimpleNamespace(returncode=0, stdout=b'{"numWritten":1}')]
        with patch.dict(self.scope, run=invoke):
            with patch.object(self.drill, 'request', side_effect=responses):
                with patch.object(self.drill, 'application_state', return_value='state'):
                    self.drill.healthy('fixture-source-app')
        command = invoke.call_args.args[-1]
        self.assertIn('chmod 0640 /config/fixture-identity.json', command)
        self.assertNotIn('chmod 0644', command)


if __name__ == '__main__':
    unittest.main()
