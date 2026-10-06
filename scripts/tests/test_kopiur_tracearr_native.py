"""Tracearr native contract regressions, run on assigned ARC only."""
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import kopiur_tracearr_native as tracearr


class TracearrTests(unittest.TestCase):
    def setUp(self):
        self.scope = tracearr.contract()
        self.drill = self.scope['DockerDrill']('tracearr')

    def test_pinned_native_contract(self):
        self.assertEqual(self.drill.databases,['tracearr'])
        self.assertEqual(self.drill.image,tracearr.IMAGE)
        self.assertEqual(self.drill.config_file,'/config/fixture.json')

    def test_original_identity_is_preserved(self):
        value = {'jwtSecret':'original','cookieSecret':'original','originalSession':'original'}
        raw = json.dumps(value).encode()
        self.assertEqual(self.drill.isolated_config(raw),raw)
        del value['originalSession']
        with self.assertRaises(ValueError):
            self.drill.isolated_config(json.dumps(value).encode())

    def test_original_session_only_uses_fixture_network_and_stdin(self):
        invoke = Mock(return_value=SimpleNamespace(stdout=b'',returncode=0))
        with patch.dict(self.scope,run=invoke):
            self.drill.request('owned-fixture','/api/v1/auth/get-session',authenticated=True)
        self.assertIn('container:owned-fixture',invoke.call_args.args)
        self.assertNotIn(self.drill.api_key,invoke.call_args.args)
        self.assertIn(self.drill.api_key.encode(),invoke.call_args.kwargs['stdin'])

    def test_visible_owner_or_setup_changes_are_rejected(self):
        session = {'user':{'id':'fixture','email':'recovery@fixture.invalid','role':'owner','name':'Fixture'}}
        status = {'needsSetup':False,'hasPasswordAuth':True}
        def request(container,path,**kwargs):
            return SimpleNamespace(stdout=json.dumps(session if 'get-session' in path else status).encode())
        self.drill.request = request
        before = self.drill.application_state('owned-fixture')
        session['user']['name'] = 'Different'
        self.assertNotEqual(before,self.drill.application_state('owned-fixture'))
        session['user']['role'] = 'member'
        with self.assertRaises(ValueError):
            self.drill.application_state('owned-fixture')

    def fixture_boot(self):
        value = {'jwtSecret':'original-jwt','cookieSecret':'original-cookie','originalSession':'original-session'}
        def start(name,image,**kwargs):
            name = 'owned-' + name
            self.drill.containers.append(name)
            return name
        self.drill.start = Mock(side_effect=start)
        def invoke(*args,**kwargs):
            return SimpleNamespace(stdout=json.dumps(value).encode(),stderr=b'',
                                   returncode=1 if args[1] == 'inspect' else 0)
        return invoke

    def test_restore_recovers_original_session_and_uses_fresh_nonroot_cache(self):
        with patch.dict(self.scope,run=self.fixture_boot()):
            self.drill.app('restored-app','owned-database','owned-config')
            self.assertEqual(self.drill.api_key,'original-session')
            calls = self.drill.start.call_args_list
            cache = next(call for call in calls if call.args[1] == tracearr.CACHE_IMAGE)
            self.assertEqual(cache.kwargs['user'],'568:568')
            self.assertEqual(cache.kwargs['entrypoint'],'dragonfly')
            env = calls[-1].kwargs['env']
            self.assertEqual(env['JWT_SECRET'],'original-jwt')
            self.assertEqual(env['COOKIE_SECRET'],'original-cookie')

    def test_expected_original_session_cannot_be_replaced(self):
        self.drill.api_key = 'expected-original'
        with patch.dict(self.scope,run=self.fixture_boot()), self.assertRaises(ValueError):
            self.drill.app('restored-app','owned-database','owned-config')

    def test_reboot_removes_only_owned_previous_cache(self):
        invoke = Mock(side_effect=self.fixture_boot())
        with patch.dict(self.scope,run=invoke):
            self.drill.app('restored-app','owned-database','owned-config')
            self.drill.app('export-app','owned-database','owned-config')
        self.assertIn(('docker','rm','-fv','owned-restored-app-cache'),[call.args for call in invoke.call_args_list])
        self.assertNotIn('owned-restored-app-cache',self.drill.containers)

    def test_request_failure_redacts_original_fixture_credentials(self):
        self.drill.auth_secret = 'original-jwt'
        message = self.drill.password + ' ' + self.drill.api_key + ' original-jwt'
        invoke = Mock(return_value=SimpleNamespace(stdout=message.encode(),stderr=b'',returncode=22))
        with patch.dict(self.scope,run=invoke), self.assertRaises(RuntimeError) as raised:
            self.drill.request('owned-fixture','/api/v1/auth/get-session',authenticated=True)
        self.assertNotIn(self.drill.password,str(raised.exception))
        self.assertNotIn(self.drill.api_key,str(raised.exception))
        self.assertNotIn('original-jwt',str(raised.exception))


if __name__ == '__main__':
    unittest.main()
