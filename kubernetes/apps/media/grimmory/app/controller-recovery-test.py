#!/usr/bin/env python3
"""Local regressions for the disposable restored-original controller contract."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

HERE = Path(__file__).resolve().parent


def load_verifier():
    source = (HERE / 'controller-verify.py').read_text().replace(
        "'/contract/consumer-verify.py'", repr(str(HERE / 'consumer-verify.py')))
    module = types.ModuleType('controller')
    exec(compile(source, str(HERE / 'controller-verify.py'), 'exec'), module.__dict__)
    return module


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR'))
        self.addCleanup(self.temporary.cleanup)
        self.controller = load_verifier()
        self.controller.CONTROL = Path(self.temporary.name)
        self.controller.native.CONTROL = self.controller.CONTROL

    def test_interrupted_verifier_never_repeats_faults(self):
        c = self.controller
        with patch.object(c, 'qualify', side_effect=KeyboardInterrupt) as qualify:
            with self.assertRaises(KeyboardInterrupt):
                c.main()
            with self.assertRaises(FileExistsError):
                c.main()
            self.assertEqual(qualify.call_count, 1)

    def test_completed_verifier_does_not_repeat_faults(self):
        c = self.controller
        (c.CONTROL / 'qualification-complete').touch()
        with patch.object(c, 'qualify') as qualify, patch.object(c.time, 'sleep', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                c.main()
            qualify.assert_not_called()

    def test_failed_verifier_stays_failed(self):
        c = self.controller
        (c.CONTROL / 'failed').touch()
        with patch.object(c, 'qualify') as qualify:
            with self.assertRaises(RuntimeError):
                c.main()
            qualify.assert_not_called()

    def test_epoch_requires_actual_replacement(self):
        c = self.controller
        self.assertEqual(c.epoch('database-epoch'), '')
        (c.CONTROL / 'database-epoch').write_text('old\n')
        self.assertEqual(c.epoch('database-epoch'), 'old')
        (c.CONTROL / 'database-epoch').write_text('new\n')
        self.assertNotEqual(c.epoch('database-epoch'), 'old')

    def test_restore_runtime_dependency_is_rendered(self):
        result = subprocess.run(['kubectl', 'kustomize', str(HERE)],
                                capture_output=True, text=True, check=True)
        result = subprocess.run(['yq', '-o=json', '-I=0', '.'], input=result.stdout,
                                capture_output=True, text=True, check=True)
        resources = [json.loads(line) for line in result.stdout.splitlines() if line.startswith('{')]
        contract = next(d for d in resources if d.get('kind') == 'ConfigMap' and
                        d['metadata']['name'] == 'grimmory-controller-recovery-contract')
        self.assertIn('runtime.sh', contract['data'])
        self.assertIn('restore.sh', contract['data'])
        for deployment in (d for d in resources if d.get('kind') == 'Deployment' and
                           d['metadata']['name'].startswith('grimmory-k8s92-recovery-')):
            pod = deployment['spec']['template']['spec']
            self.assertFalse(pod['automountServiceAccountToken'])
            self.assertNotIn('fsGroup', pod['securityContext'])
            self.assertNotIn('shareProcessNamespace', pod)
            original = next(v for v in pod['volumes'] if v['name'] == 'original')
            self.assertTrue(original['persistentVolumeClaim']['readOnly'])
            self.assertFalse(any('secret' in v or 'nfs' in v for v in pod['volumes']))
            application = next(c for c in pod['containers'] if c['name'] == 'application')
            self.assertEqual(application['livenessProbe']['httpGet']['path'], '/api/v1/healthcheck')

    def test_suppressed_restart_epoch_cannot_pass(self):
        c = self.controller
        (c.CONTROL / 'database-ready').write_text(c.native.EXPECTED_DATA)
        (c.CONTROL / 'database-epoch').write_text('old-database')
        (c.CONTROL / 'application-epoch').write_text('old-app')
        checks = []
        def wait(check):
            checks.append(check)
            if not check():
                raise TimeoutError('replacement never happened')
        with patch.object(c.native, 'wait_for', side_effect=wait), \
                patch.object(c.native, 'health', return_value=True), \
                patch.object(c.native, 'check_database', Mock()):
            with self.assertRaises(TimeoutError), patch.dict(os.environ, {'BACKEND': 'nas'}):
                c.qualify()
        self.assertFalse((c.CONTROL / 'qualification-complete').exists())
        self.assertEqual(len(checks), 3)


if __name__ == '__main__':
    unittest.main()
