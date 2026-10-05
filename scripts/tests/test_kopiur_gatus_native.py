"""Gatus regressions, executed on authorized ARC only."""
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import kopiur_gatus_native as gatus


class GatusTests(unittest.TestCase):
    def setUp(self):
        self.scope = gatus.contract()
        self.drill = self.scope['DockerDrill']('gatus')

    def test_pinned_native_contract(self):
        self.assertEqual(self.drill.databases, ['gatus'])
        self.assertEqual(self.drill.image, gatus.IMAGE)
        self.assertEqual(self.drill.config_file, '/config/gatus.yaml')

    def test_reconciliation_preserves_config_and_token(self):
        raw = b'path: "${KOPIUR_DB_URI}"\nexternal-endpoints:\n token: original\n'
        self.assertEqual(self.drill.isolated_config(raw), raw)
        with self.assertRaises(ValueError):
            self.drill.isolated_config(b'wrong')

    def test_records_are_compared_before_deliberate_write(self):
        status = {'name': 'fixture', 'results': [{'success': False, 'timestamp': 'persisted'}]}
        invoke = Mock(return_value=SimpleNamespace(stdout=json.dumps(status).encode()))
        with patch.dict(self.scope, run=invoke):
            before = self.drill.application_state('owned-export-app')
            self.assertEqual(invoke.call_count, 1)
            restored = self.drill.application_state('owned-restored-app')
            self.assertEqual(invoke.call_count, 3)
            self.assertEqual(before, restored)
            status['results'][0]['timestamp'] = 'different'
            invoke.return_value.stdout = json.dumps(status).encode()
            self.assertNotEqual(before, self.drill.application_state('owned-export-app'))
        self.assertNotIn(self.drill.api_key, invoke.call_args.args)

    def test_original_token_request_cannot_reach_production(self):
        invoke = Mock(return_value=SimpleNamespace(stdout=b'', returncode=0))
        with patch.dict(self.scope, run=invoke):
            self.drill.request('owned-fixture', '/api/v1/endpoints/recovery_fixture/external?success=true',
                               post=True, authenticated=True)
        self.assertIn('container:owned-fixture', invoke.call_args.args)
        self.assertNotIn(self.drill.api_key, invoke.call_args.args)
        self.assertIn(self.drill.api_key.encode(), invoke.call_args.kwargs['stdin'])


if __name__ == '__main__':
    unittest.main()
