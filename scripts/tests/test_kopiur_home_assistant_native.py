"""Home Assistant native fixture regressions for authorized ARC."""
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import kopiur_home_assistant_native as ha


class HomeAssistantTests(unittest.TestCase):
    def setUp(self):
        self.scope = ha.contract()
        self.drill = self.scope['DockerDrill'](ha.APP)

    def test_postgres_is_fixture_only_not_guessed_production_binding(self):
        self.assertEqual(self.drill.databases, ['homeassistant_fixture'])
        self.assertEqual(self.drill.image, ha.IMAGE)
        self.assertEqual(self.drill.port, 8123)

    def test_helper_readiness_retries_without_replacing_identity(self):
        identity = self.drill.api_key
        responses = [SimpleNamespace(returncode=22), SimpleNamespace(returncode=0)]
        with patch.object(self.drill, 'request', side_effect=responses) as request:
            with patch.object(ha.time, 'sleep'):
                self.drill.wait_for_helper('restored')
        self.assertEqual(self.drill.api_key, identity)
        self.assertEqual(request.call_count, 2)
        request.assert_called_with('restored', '/api/states/input_boolean.restore_fixture',
                                   authenticated=True, check=False)

    def test_capture_excludes_only_exact_runtime_stdout_link(self):
        self.drill.containers.append('fixture-source-config-holder')
        invoke = Mock()
        base = type(self.drill).__mro__[1]
        with patch.dict(self.scope, run=invoke):
            with patch.object(base, 'capture', return_value='captured') as capture:
                self.assertEqual(self.drill.capture('database', 'config', ['homeassistant_fixture'], check=False), 'captured')
        script = invoke.call_args.args[-1]
        self.assertIn('if test -L /config/home-assistant.log; then', script)
        self.assertIn('test "$(readlink /config/home-assistant.log)" = /proc/self/fd/1;', script)
        self.assertIn('unlink /config/home-assistant.log; fi', script)
        capture.assert_called_once_with('database', 'config', ['homeassistant_fixture'], check=False)

    def test_runtime_logs_do_not_enter_recovered_configuration(self):
        identity = json.dumps({'original_fixture_key': 'original-fixture-key'}).encode()
        with patch.dict(self.scope, run=Mock(return_value=SimpleNamespace(stdout=identity))):
            with patch.object(self.drill, 'start', return_value='restored') as start:
                self.assertEqual(self.drill.app('restored-app', 'database', 'config'), 'restored')
        self.assertEqual(start.call_args.kwargs['command'], ['--log-file', '/tmp/fixture.log'])

    def test_original_bearer_token_in_stdin_only(self):
        invoke = Mock(return_value=SimpleNamespace(stdout=b'{}'))
        with patch.dict(self.scope, run=invoke):
            self.drill.request('fixture', '/api/', authenticated=True)
        self.assertNotIn(self.drill.api_key, invoke.call_args.args)
        self.assertIn(b'Authorization: Bearer ', invoke.call_args.kwargs['stdin'])
        self.assertIn(self.drill.api_key.encode(), invoke.call_args.kwargs['stdin'])
        self.assertIn('container:fixture', invoke.call_args.args)

    def test_helper_attributes_and_state_remain_strict(self):
        value = {'entity_id': 'input_boolean.restore_fixture', 'state': 'on', 'attributes': {'name': 'fixture'}}
        invoke = Mock(return_value=SimpleNamespace(stdout=json.dumps(value).encode()))
        with patch.dict(self.scope, run=invoke):
            first = self.drill.application_state('fixture')
            value['attributes']['name'] = 'changed'
            invoke.return_value.stdout = json.dumps(value).encode()
            self.assertNotEqual(first, self.drill.application_state('fixture'))
            value['state'] = 'off'
            invoke.return_value.stdout = json.dumps(value).encode()
            with self.assertRaises(ValueError):
                self.drill.application_state('fixture')


if __name__ == '__main__':
    unittest.main()
