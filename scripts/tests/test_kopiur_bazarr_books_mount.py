import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('bazarr_mount', ROOT / 'scripts/kopiur-bazarr-books-mount-native.py')
if spec is None or spec.loader is None:
    raise RuntimeError('native fixture module unavailable')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class BooksMountTests(unittest.TestCase):
    def result(self, code=0, out='', err=''):
        return subprocess.CompletedProcess([], code, out, err)

    def test_container_owner(self):
        with patch.object(module, 'docker', return_value=self.result(out=json.dumps([{'Config': {'Labels': {module.LABEL: 'owner'}}}]))):
            self.assertTrue(module.inspect_owned('container', 'name', 'owner'))

    def test_volume_owner(self):
        with patch.object(module, 'docker', return_value=self.result(out=json.dumps([{'Labels': {module.LABEL: 'owner'}}]))):
            self.assertTrue(module.inspect_owned('volume', 'name', 'owner'))

    def test_wrong_owner_rejected(self):
        with patch.object(module, 'docker', return_value=self.result(out=json.dumps([{'Labels': {module.LABEL: 'other'}}]))):
            with self.assertRaisesRegex(RuntimeError, 'ownership'):
                module.inspect_owned('volume', 'name', 'owner')

    def test_missing_resource(self):
        with patch.object(module, 'docker', return_value=self.result(1, err='Error: No such volume: name')):
            self.assertFalse(module.inspect_owned('volume', 'name', 'owner'))

    def test_inspection_failure_not_missing(self):
        with patch.object(module, 'docker', return_value=self.result(1, err='connection refused')):
            with self.assertRaisesRegex(RuntimeError, 'inspection'):
                module.inspect_owned('volume', 'name', 'owner')

    def test_docker_failure_sanitized(self):
        with patch.object(module.subprocess, 'run', return_value=self.result(1, err='private provider detail')):
            with self.assertRaisesRegex(RuntimeError, '^Bazarr owned Docker operation failed$'):
                module.docker('info')

    def test_installed_docker_missing_volume(self):
        with patch.object(module, 'docker', return_value=self.result(1, err='Error response from daemon: get name: no such volume')):
            self.assertFalse(module.inspect_owned('volume', 'name', 'owner'))

    def test_different_missing_kind_rejected(self):
        with patch.object(module, 'docker', return_value=self.result(1, err='Error response from daemon: No such container: name')):
            with self.assertRaisesRegex(RuntimeError, 'inspection'):
                module.inspect_owned('volume', 'name', 'owner')

    def test_success_receipt_follows_cleanup(self):
        source = (ROOT / 'scripts/kopiur-bazarr-books-mount-native.py').read_text()
        self.assertLess(source.index('cleanup(containers, volumes, owner)', source.index('def main')),
                        source.index("'owned_cleanup_verified': True"))

    def exercise_probe(self, readonly, app='bazarr'):
        contract = module.CONTRACTS[app]
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for name in ['Books', *contract['writable']]:
                (root / name).mkdir()
            (root / 'Books/original').write_bytes(b'original')
            source = module.PROBE.replace("'/media'", repr(temp))
            with patch('os.getuid', return_value=contract['uid']), patch('os.getgid', return_value=contract['gid']), patch.dict(os.environ, EXPECT_READONLY=str(readonly).lower(), EXPECT_UID=str(contract['uid']), EXPECT_GID=str(contract['gid']), WRITABLE_DIRS=json.dumps(contract['writable'])):
                exec(compile(source, '<native-probe>', 'exec'), {})

    def test_writable_books_rejected(self):
        with self.assertRaisesRegex(RuntimeError, 'Books is writable'):
            self.exercise_probe(True)

    def test_rollback_probe(self):
        self.exercise_probe(False)

    def test_listenarr_root_writable_books_rejected(self):
        with self.assertRaisesRegex(RuntimeError, 'Books is writable'):
            self.exercise_probe(True, 'listenarr')

    def test_listenarr_root_rollback_preserves_audiobook_writes(self):
        self.exercise_probe(False, 'listenarr')

    def test_listenarr_digest_matches_live_application(self):
        contract = module.CONTRACTS['listenarr']
        manifest = (ROOT / 'kubernetes/apps/arrs/listenarr/app/helmrelease.yaml').read_text()
        self.assertIn(contract['image'].split(':', 1)[1], manifest)
        self.assertEqual(contract['uid'], 0)
        self.assertEqual(contract['writable'], ['Audiobooks', 'Downloads'])

    def test_radarr_native_bash_syntax(self):
        for source in (module.SHELL_SEED, module.SHELL_PROBE):
            result = subprocess.run(['bash', '-n'], input=source, text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)

    def shell_probe(self, readonly, uid=None, directory='Movies'):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for name in ('Books', directory):
                (root / name).mkdir()
            (root / 'Books/original').write_text('original')
            source = module.SHELL_PROBE.replace('root=/media', 'root=' + temp)
            env = dict(os.environ, EXPECT_UID=str(os.getuid() if uid is None else uid),
                       EXPECT_GID=str(os.getgid()), EXPECT_READONLY=str(readonly).lower(),
                       SHELL_WRITABLE_DIR=directory)
            return subprocess.run(['bash', '-c', source], env=env, text=True, capture_output=True)

    def test_radarr_rollback_native_shell(self):
        result = self.shell_probe(False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(json.loads(result.stdout)['books_original_equal'])

    def test_radarr_writable_books_rejected(self):
        result = self.shell_probe(True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Books is writable', result.stderr)

    def test_radarr_wrong_identity_rejected(self):
        self.assertNotEqual(self.shell_probe(False, os.getuid() + 1).returncode, 0)

    def test_radarr_exact_live_digest(self):
        contract = module.CONTRACTS['radarr']
        manifest = (ROOT / 'kubernetes/apps/arrs/radarr/app/helmrelease.yaml').read_text()
        self.assertIn(contract['image'].split(':', 1)[1], manifest)
        self.assertTrue(contract['shell_probe'])
        self.assertEqual(contract['uid'], 568)
        self.assertEqual(contract['writable'], ['Movies'])

    def test_sonarr_rollback_preserves_tv_writes(self):
        result = self.shell_probe(False, directory='TV')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['writable_paths'], ['TV'])

    def test_sonarr_writable_books_rejected(self):
        result = self.shell_probe(True, directory='TV')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Books is writable', result.stderr)

    def test_shell_unsafe_writable_path_rejected(self):
        self.assertNotEqual(self.shell_probe(False, directory='Unknown').returncode, 0)

    def test_sonarr_exact_live_digest(self):
        contract = module.CONTRACTS['sonarr']
        manifest = (ROOT / 'kubernetes/apps/arrs/sonarr/app/helmrelease.yaml').read_text()
        self.assertIn(contract['image'].split(':', 1)[1], manifest)
        self.assertTrue(contract['shell_probe'])
        self.assertEqual(contract['uid'], 568)
        self.assertEqual(contract['writable'], ['TV'])

    def test_whisparr_rollback_preserves_adult_writes(self):
        result = self.shell_probe(False, directory='Adult')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['writable_paths'], ['Adult'])

    def test_whisparr_writable_books_rejected(self):
        result = self.shell_probe(True, directory='Adult')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Books is writable', result.stderr)

    def test_whisparr_exact_live_digest(self):
        contract = module.CONTRACTS['whisparr']
        manifest = (ROOT / 'kubernetes/apps/arrs/whisparr/app/helmrelease.yaml').read_text()
        self.assertIn(contract['image'].split(':', 1)[1], manifest)
        self.assertTrue(contract['shell_probe'])
        self.assertEqual(contract['uid'], 568)
        self.assertEqual(contract['writable'], ['Adult'])

    def test_cleanup_continues_after_failure(self):
        seen = []
        def inspect(kind, name, owner):
            seen.append((kind, name))
            if name == 'bad':
                raise RuntimeError('inspection failed')
            return False
        with patch.object(module, 'inspect_owned', side_effect=inspect):
            with self.assertRaisesRegex(RuntimeError, 'cleanup incomplete'):
                module.cleanup(['good', 'bad'], ['volume'], 'owner')
        self.assertIn(('container', 'good'), seen)
        self.assertIn(('volume', 'volume'), seen)

    def test_digest_matches_live_application(self):
        manifest = (ROOT / 'kubernetes/apps/arrs/bazarr/app/helmrelease.yaml').read_text()
        self.assertIn(module.IMAGE.split(':', 1)[1], manifest)


if __name__ == '__main__':
    unittest.main()
