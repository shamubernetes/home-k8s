#!/usr/bin/env python3
"""Local regressions for the disposable restored-original controller contract."""
import json
import os
from pathlib import Path
import subprocess
import shutil
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
        # Retain the qualified implementation without keeping proof consumers live.
        render = Path(self.temporary.name) / 'render'
        render.mkdir()
        files = ['consumer-prepare.py', 'consumer-verify.py', 'controller-app.sh',
                 'controller-database.sh', 'controller-verify.py', 'restore.sh', 'runtime.sh']
        for name in files + ['kopiur-controller-recovery.yaml']:
            shutil.copyfile(HERE / name, render / name)
        (render / 'kustomization.yaml').write_text(json.dumps({
            'apiVersion': 'kustomize.config.k8s.io/v1beta1', 'kind': 'Kustomization',
            'resources': ['kopiur-controller-recovery.yaml'],
            'configMapGenerator': [{'name': 'grimmory-books-controller-recovery-contract',
                                    'files': files,
                                    'options': {'disableNameSuffixHash': True}}]}))
        result = subprocess.run(['kubectl', 'kustomize', str(render)],
                                capture_output=True, text=True, check=True)
        result = subprocess.run(['yq', '-o=json', '-I=0', '.'], input=result.stdout,
                                capture_output=True, text=True, check=True)
        resources = [json.loads(line) for line in result.stdout.splitlines() if line.startswith('{')]
        contract = next(d for d in resources if d.get('kind') == 'ConfigMap' and
                        d['metadata']['name'] == 'grimmory-books-controller-recovery-contract')
        self.assertIn('runtime.sh', contract['data'])
        self.assertIn('restore.sh', contract['data'])
        for deployment in (d for d in resources if d.get('kind') == 'Deployment' and
                           d['metadata']['name'].startswith('grimmory-k8s92-books-recovery-')):
            pod = deployment['spec']['template']['spec']
            self.assertFalse(pod['automountServiceAccountToken'])
            self.assertNotIn('fsGroup', pod['securityContext'])
            self.assertNotIn('shareProcessNamespace', pod)
            original = next(v for v in pod['volumes'] if v['name'] == 'original')
            self.assertTrue(original['persistentVolumeClaim']['readOnly'])
            self.assertFalse(any('secret' in v or 'nfs' in v for v in pod['volumes']))
            application = next(c for c in pod['containers'] if c['name'] == 'application')
            self.assertEqual(application['livenessProbe']['httpGet']['path'], '/api/v1/healthcheck')
            backend = deployment['metadata']['name'].rsplit('-', 1)[1]
            self.assertEqual(original['persistentVolumeClaim']['claimName'],
                             'grimmory-k8s92-books-original-' + backend)
            books = [m for m in application['volumeMounts'] if m['name'] == 'books']
            self.assertEqual({m['mountPath'] for m in books}, {'/books', '/bookdrop'})
            self.assertTrue(all(m['readOnly'] for m in books))
            self.assertEqual(next(m for m in books if m['mountPath'] == '/bookdrop')['subPath'], 'bookdrop')
            prepare = pod['initContainers'][0]
            self.assertIn('FSETID', prepare['securityContext']['capabilities']['add'])
            self.assertEqual(next(v for v in pod['volumes'] if v['name'] == 'books')['emptyDir']['medium'], 'Memory')
        self.assertEqual(sum(d.get('kind') == 'Deployment' and
                             d['metadata']['name'].startswith('grimmory-k8s92-books-recovery-')
                             for d in resources), 2)
        policy = next(d for d in resources if d.get('kind') == 'NetworkPolicy' and
                      d['metadata']['name'] == 'grimmory-k8s92-books-recovery')
        self.assertEqual(policy['spec']['ingress'], [])
        self.assertEqual(policy['spec']['egress'], [])

    def test_production_keeps_originals_without_disposable_consumers(self):
        result = subprocess.run(['kubectl', 'kustomize', str(HERE)],
                                capture_output=True, text=True, check=True)
        result = subprocess.run(['yq', '-o=json', '-I=0', '.'], input=result.stdout,
                                capture_output=True, text=True, check=True)
        resources = [json.loads(line) for line in result.stdout.splitlines() if line.startswith('{')]
        names = {d['metadata']['name'] for d in resources}
        self.assertNotIn('grimmory-books-controller-recovery-contract', names)
        self.assertFalse(any(n.startswith('grimmory-k8s92-books-recovery') for n in names))
        self.assertIn('grimmory-k8s92-books-original-nas', names)
        self.assertIn('grimmory-k8s92-books-original-r2', names)
        self.assertIn('../../../../templates/volsync', (HERE / 'kustomization.yaml').read_text())

    def test_native_database_fault_selects_exact_daemon(self):
        source = (HERE / 'controller-database.sh').read_text()
        self.assertNotIn('kill -TERM 1', source)
        snippet = source.split("s6-setuidgid abc bash -c '", 1)[1].split("\n    '", 1)[0]
        for count in (0, 1, 2):
            with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR')) as temporary:
                root = Path(temporary)
                for index in range(count):
                    folder = root / str(index + 100)
                    folder.mkdir()
                    (folder / 'comm').write_text('mariadbd\n')
                other = root / '200'
                other.mkdir()
                (other / 'comm').write_text('mariadbd-safe\n')
                record = root / 'signal'
                code = 'kill() { printf "%s %s\\n" "$1" "$2" >"' + str(record) + '"; };\n'
                code += snippet.replace('/proc/[0-9]*/comm', str(root) + '/[0-9]*/comm')
                result = subprocess.run(['bash', '-c', code], capture_output=True, text=True)
                self.assertEqual(result.returncode == 0, count == 1)
                self.assertEqual(record.exists(), count == 1)
                if count == 1:
                    self.assertEqual(record.read_text(), '-STOP 100\n')

    def test_failed_database_api_refuses_fault_and_receipt(self):
        c = self.controller
        (c.CONTROL / 'database-ready').write_text(c.native.EXPECTED_DATA)
        def wait(check):
            if not check():
                raise TimeoutError('database-backed API failed')
        with patch.object(c.native, 'wait_for', side_effect=wait), \
                patch.object(c.native, 'health', return_value=True), \
                patch.object(c.native, 'setup_status', return_value=False):
            with self.assertRaises(TimeoutError):
                c.qualify()
        self.assertFalse((c.CONTROL / 'replace-database').exists())
        self.assertFalse((c.CONTROL / 'controller-receipt.json').exists())
        self.assertFalse((c.CONTROL / 'qualification-complete').exists())

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
                patch.object(c.native, 'setup_status', return_value=True), \
                patch.object(c.native, 'check_database', Mock()):
            with self.assertRaises(TimeoutError), patch.dict(os.environ, {'BACKEND': 'nas'}):
                c.qualify()
        self.assertFalse((c.CONTROL / 'qualification-complete').exists())
        self.assertEqual(len(checks), 4)


if __name__ == '__main__':
    unittest.main()
