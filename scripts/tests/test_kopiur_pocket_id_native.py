"""Pocket ID regressions for authorized ARC."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import kopiur_pocket_id_native as pocket


class PocketTests(unittest.TestCase):
    def setUp(self):
        self.scope = pocket.contract()
        self.drill = self.scope['DockerDrill']('pocket-id')

    def test_native_contract(self):
        self.assertEqual(self.drill.databases, ['pocket_id'])
        self.assertEqual(self.drill.image, pocket.IMAGE)

    def test_original_fixture_key_preserved(self):
        raw = json.dumps({'original_fixture_key': 'original-fixture-key'}).encode()
        self.assertEqual(self.drill.isolated_config(raw), raw)
        for bad in (b'{}', b'null', b'{"original_fixture_key":"short"}'):
            with self.assertRaises(ValueError):
                self.drill.isolated_config(bad)

    def test_supplied_original_key_cannot_be_repaired(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'fixture-identity.json').write_text(json.dumps({'original_fixture_key': 'original-key'}))
            with self.assertRaises(ValueError):
                self.scope['restore_pvc'](root, 'pocket-id', original_api_key='changed-key')

    def test_http_failure_reports_only_status(self):
        from types import SimpleNamespace
        result = SimpleNamespace(returncode=22, stdout=b'private response',
                                 stderr=b'curl: (22) The requested URL returned error: 403')
        with patch.dict(self.scope, run=Mock(return_value=result)):
            with self.assertRaisesRegex(RuntimeError, '^isolated fixture HTTP failure, status=403$'):
                self.drill.request('fixture', '/api/users', authenticated=True)
            self.assertIs(self.drill.request('fixture', '/api/users', check=False), result)

    def test_identity_in_stdin_only(self):
        from types import SimpleNamespace
        invoke = Mock(return_value=SimpleNamespace(stdout=b'{}'))
        with patch.dict(self.scope, run=invoke):
            self.assertEqual(self.drill.request('fixture', '/api/users', authenticated=True).stdout, b'{}')
        self.assertNotIn(self.drill.api_key, invoke.call_args.args)
        self.assertIn(self.drill.api_key.encode(), invoke.call_args.kwargs['stdin'])
        self.assertIn('container:fixture', invoke.call_args.args)


if __name__ == '__main__':
    unittest.main()
