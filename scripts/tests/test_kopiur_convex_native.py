"""Convex native fixture regressions for authorized ARC."""
import copy
import json
from pathlib import Path
import sys
from types import SimpleNamespace
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
        self.assertEqual(invoke.call_count, 1)
        self.assertNotIn('generate_admin_key.sh', str(invoke.call_args.args))
        env = start.call_args.kwargs['env']
        self.assertEqual(env['INSTANCE_SECRET'], 'a' * 64)
        self.assertFalse(any(key.startswith(('S3_', 'AWS_')) for key in env))
        self.assertEqual(start.call_args.kwargs['network'], 'container:database')


if __name__ == '__main__':
    unittest.main()
