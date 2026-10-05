"""Grafana and common native-fixture regressions, authorized ARC only."""
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import kopiur_grafana_native as grafana
import kopiur_native_fixture as common


class GrafanaTests(unittest.TestCase):
    def setUp(self):
        self.scope = grafana.contract()
        self.drill = self.scope['DockerDrill']('grafana')

    def test_source_engine_and_complete_config_contract(self):
        self.assertEqual(self.drill.databases, ['grafana'])
        self.assertEqual(self.drill.image, grafana.IMAGE)
        self.assertEqual(self.drill.config_file, '/config/grafana.ini')

    def test_fixture_user_matches_owned_config_volume(self):
        with patch.object(self.drill, 'start', return_value='fixture') as start:
            self.drill.app('restore-fixture', 'fixture-db', 'fixture-config')
        self.assertEqual(start.call_args.kwargs['user'], '568:568')
        self.assertEqual(start.call_args.kwargs['network'], 'container:fixture-db')

    def test_reconciliation_preserves_original_key_bytes(self):
        ini = b'[analytics]\nreporting_enabled=false\n[security]\nsecret_key=original\n'
        self.assertEqual(self.drill.isolated_config(ini), ini)
        with self.assertRaises(ValueError):
            self.drill.isolated_config(b'unknown')

    def test_actual_dashboard_api_and_original_identity(self):
        value = {'dashboard': {'uid': 'recovery-fixture', 'title': 'original'}, 'meta': {'version': 1}}
        invoke = Mock(return_value=SimpleNamespace(stdout=json.dumps(value).encode()))
        with patch.dict(self.scope, run=invoke):
            first = self.drill.application_state('owned-fixture')
            value['dashboard']['title'] = 'changed'
            invoke.return_value.stdout = json.dumps(value).encode()
            self.assertNotEqual(first, self.drill.application_state('owned-fixture'))
        args = invoke.call_args.args
        self.assertIn('container:owned-fixture', args)
        self.assertNotIn(self.drill.api_key, args)
        self.assertIn(self.drill.api_key.encode(), invoke.call_args.kwargs['stdin'])


class CommonFixtureTests(unittest.TestCase):
    def test_limitations_cannot_fabricate_acceptance(self):
        for gates in ({'production_recovery_accepted': True}, {'client_decryption_qualified': True}):
            with self.assertRaises(ValueError):
                common.exercise({}, 'fixture', [], gates)

    def test_whole_tree_and_native_checks_and_no_provider_claim(self):
        flags = {key: True for key in ('native_restore', 'restored_app_ping', 'native_table_contents_equal',
                                       'original_fixture_identity_used', 'application_visible_state_equal')}
        restore = Mock(return_value=flags)
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)
            (source / 'fixture').write_bytes(b'original-state')
            def fixture(app, export):
                export(source, {'catalog': 'original'}, 'original-identity', 'original-api-state')
            native = {'PG_IMAGE': 'pinned-pg', 'run': Mock(), 'fixture': fixture, 'restore_pvc': restore}
            result = common.exercise(native, 'fixture', ['pinned-app'])
        self.assertEqual(result['artifact_entries'], 1)
        self.assertFalse(result['production_recovery_accepted'])
        self.assertFalse(result['encrypted_transport_qualified'])
        self.assertEqual(restore.call_args.kwargs['original_api_key'], 'original-identity')

    def test_scrub_all_fixture_credentials(self):
        run = Mock(return_value=SimpleNamespace(stdout=b'pass backup identity', stderr=b''))
        drill = SimpleNamespace(password='pass', backup_password='backup', api_key='identity')
        error = common.startup_failure({'run': run}, drill, 'owned-fixture', 'fixture failed')
        for value in ('pass', 'backup', 'identity'):
            self.assertNotIn(value, str(error))


if __name__ == '__main__':
    unittest.main()
