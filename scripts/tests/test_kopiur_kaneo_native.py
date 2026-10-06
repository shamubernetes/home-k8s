"""Kaneo fixture regressions for authorized ARC."""
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import kopiur_kaneo_native as kaneo


class KaneoTests(unittest.TestCase):
    def setUp(self):
        self.scope = kaneo.contract()
        self.drill = self.scope['DockerDrill']('kaneo')

    def test_pinned_contract(self):
        self.assertEqual(self.drill.databases, ['kaneo'])
        self.assertEqual(self.drill.image, kaneo.IMAGE)
        self.assertEqual(self.drill.port, 1337)

    def test_catalog_includes_drizzle_migrations(self):
        with patch.object(self.drill, 'sql', side_effect=['drizzle.__drizzle_migrations\npublic.task', '2', '1']) as sql:
            self.assertEqual(self.drill.counts('database'), {'drizzle.__drizzle_migrations': 2, 'public.task': 1})
        self.assertIn('quote_ident(schemaname)', sql.call_args_list[0].args[1])

    def test_migration_content_changes_fingerprint(self):
        with patch.object(self.drill, 'counts', return_value={'drizzle.__drizzle_migrations': 1}):
            with patch.object(self.drill, 'sql', side_effect=['original', 'changed']):
                self.assertNotEqual(self.drill.fingerprints('database', 'kaneo'),
                                    self.drill.fingerprints('database', 'kaneo'))

    def test_original_session_and_signing_secret_are_separate(self):
        value = {'original_fixture_key': 'original-session-token',
                 'auth_secret': 'original-signing-secret', 'task_id': 'fixture-task'}
        raw = json.dumps(value).encode()
        self.assertEqual(self.drill.isolated_config(raw), raw)
        with self.assertRaises(ValueError):
            self.drill.isolated_config(b'{}')
        with patch.dict(self.scope, run=Mock(return_value=SimpleNamespace(stdout=raw))):
            with patch.object(self.drill, 'start', return_value='restored') as start:
                self.assertEqual(self.drill.app('restored-app', 'database', 'config'), 'restored')
        self.assertEqual(self.drill.api_key, value['original_fixture_key'])
        self.assertEqual(start.call_args.kwargs['env']['AUTH_SECRET'], value['auth_secret'])
        self.assertEqual(start.call_args.kwargs['entrypoint'], 'node')

    def test_entrypoint_keeps_isolation_flags(self):
        result = SimpleNamespace(returncode=0, stdout=b'', stderr=b'')
        with patch.dict(self.scope, run=Mock(return_value=result)):
            self.drill.start('fixture', kaneo.IMAGE, entrypoint='node', command=['--version'])
            args = self.scope['run'].call_args.args
        self.assertIn('--read-only', args)
        self.assertIn('--cap-drop', args)
        self.assertEqual(args[args.index('--entrypoint') + 1], 'node')


if __name__ == '__main__':
    unittest.main()
