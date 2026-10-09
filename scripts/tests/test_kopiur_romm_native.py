"""RomM native fixture regressions for authorized ARC."""
import copy
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import kopiur_romm_native as romm


class RomMTests(unittest.TestCase):
    def setUp(self):
        self.scope = romm.contract()
        self.drill = self.scope['DockerDrill']('romm')

    def test_deployed_postgresql_contract(self):
        self.assertEqual(self.drill.image, romm.IMAGE)
        self.assertEqual(self.drill.databases, ['romm'])
        self.assertEqual(self.drill.port, 8080)

    def test_user_projection_preserves_permissions(self):
        value = [{'id': 1, 'username': 'recovery-fixture', 'email': 'fixture@example.invalid',
            'enabled': True, 'role': 'admin', 'oauth_scopes': ['users.read'], 'last_active': 'runtime'}]
        first = romm.stable_users(value)
        changed = copy.deepcopy(value)
        changed[0]['last_active'] = 'new-runtime'
        self.assertEqual(first, romm.stable_users(changed))
        changed[0]['enabled'] = False
        self.assertNotEqual(first, romm.stable_users(changed))
        with self.assertRaises(ValueError):
            romm.stable_users([])

    def test_anonymous_bootstrap_uses_native_csrf_cookie_and_header(self):
        invoke = Mock(return_value=SimpleNamespace(stdout=b'HTTP/1.1 200 OK\r\nSet-Cookie: romm_csrftoken=fixture-csrf; Path=/\r\n\r\n', returncode=0))
        with patch.dict(self.scope, run=invoke):
            self.drill.bootstrap_csrf('source-app')
        self.assertEqual(self.drill.csrf_token, 'fixture-csrf')
        invoke = Mock(return_value=SimpleNamespace(stdout=b'{}', returncode=0))
        with patch.dict(self.scope, run=invoke):
            self.drill.request('source-app', '/api/users', {'username': 'fixture'})
        self.assertNotIn('fixture-csrf', str(invoke.call_args.args))
        self.assertIn(b'Cookie: romm_csrftoken=fixture-csrf', invoke.call_args.kwargs['stdin'])
        self.assertIn(b'x-csrftoken: fixture-csrf', invoke.call_args.kwargs['stdin'])
        with patch.dict(self.scope, run=invoke):
            self.drill.request('restored-app', '/api/users', authenticated=True)
        self.assertNotIn(b'csrftoken', invoke.call_args.kwargs['stdin'])

    def test_original_jwt_and_signing_secret_preserved(self):
        raw = json.dumps({'original_fixture_key': 'original-fixture-token', 'auth_secret': 'original-fixture-secret'}).encode()
        self.assertEqual(self.drill.isolated_config(raw), raw)
        with self.assertRaises(ValueError):
            self.drill.isolated_config(b'{}')

    def test_all_disposable_app_state_is_in_captured_volume(self):
        identity = json.dumps({'original_fixture_key': 'original-fixture-token', 'auth_secret': 'original-fixture-secret'}).encode()
        with patch.dict(self.scope, run=Mock(return_value=SimpleNamespace(stdout=identity))):
            with patch.object(self.drill, 'start', return_value='restored') as start:
                self.assertEqual(self.drill.app('restored-app', 'database', 'config'), 'restored')
        mounts = start.call_args.kwargs['mounts']
        self.assertEqual({source for source, _, _ in mounts}, {'config'})
        self.assertEqual({target for _, target, _ in mounts}, {'/config', '/romm', '/redis-data', '/etc/nginx/conf.d'})
        self.assertEqual(start.call_args.kwargs['env']['ROMM_AUTH_SECRET_KEY'], 'original-fixture-secret')
        self.assertEqual(start.call_args.kwargs['network'], 'container:database')


if __name__ == '__main__':
    unittest.main()
