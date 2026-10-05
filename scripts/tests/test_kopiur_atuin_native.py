"""Atuin fixture regressions, run only on authorized Linux ARC."""
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import kopiur_atuin_native as atuin


class AtuinTests(unittest.TestCase):
    def setUp(self):
        self.scope = atuin.contract()
        self.drill = self.scope['DockerDrill']('atuin')

    def test_native_database_and_pinned_source_image(self):
        self.assertEqual(self.drill.databases, ['atuin'])
        self.assertEqual(self.drill.image, atuin.IMAGE)
        self.assertIn('@sha256:', self.drill.image)

    def test_fixture_user_matches_owned_config_volume(self):
        with patch.object(self.drill, 'start', return_value='fixture') as start:
            self.drill.app('restore-fixture', 'fixture-db', 'fixture-config')
        self.assertEqual(start.call_args.kwargs['user'], '568:568')
        self.assertEqual(start.call_args.kwargs['network'], 'container:fixture-db')

    def test_original_session_is_not_rewritten(self):
        raw = json.dumps({'username': 'recovery-fixture', 'session': 'original-session'}).encode()
        self.assertEqual(self.drill.isolated_config(raw), raw)
        for bad in (b'{}', b'null', b'x' * 65537):
            with self.assertRaises((ValueError, TypeError)):
                self.drill.isolated_config(bad)

    def test_application_identity_change_rejects(self):
        original = json.dumps({'username': 'recovery-fixture', 'session': self.drill.api_key}).encode()
        responses = iter([SimpleNamespace(stdout=original), SimpleNamespace(stdout=b'{"username":"wrong"}')])
        with patch.dict(self.scope, run=lambda *a, **kw: next(responses)):
            with self.assertRaises(ValueError):
                self.drill.application_state('fixture')

    def test_credentials_use_stdin_not_command_arguments(self):
        calls = []
        def invoke(*args, **kwargs):
            calls.append((args, kwargs))
            return SimpleNamespace(stdout=b'{}')
        with patch.dict(self.scope, run=invoke):
            self.drill.request('fixture', '/api/v0/me', authenticated=True)
        args, kwargs = calls[0]
        self.assertNotIn(self.drill.api_key, args)
        self.assertIn(self.drill.api_key.encode(), kwargs['stdin'])


if __name__ == '__main__':
    unittest.main()
