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
