import importlib.util
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('chaptarr_edge', ROOT / 'kubernetes/apps/arrs/chaptarr/recovery/online-postgres.py')
assert spec and spec.loader
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class NativePostgres(unittest.TestCase):
    def test_archive_commands_are_per_database_and_no_credentials_in_argv(self):
        calls = []
        def run(argv, **kwargs):
            calls.append(argv)
            if argv[0] == 'pg_dump':
                Path(argv[-1]).write_bytes(b'synthetic archive fixture')
            return subprocess.CompletedProcess(argv, 0, stdout=b'toc fixture', stderr=b'')
        with tempfile.TemporaryDirectory() as tmp, patch.object(module.subprocess, 'run', run):
            manifest = module.capture(Path(tmp) / 'generation')
            self.assertEqual([r['database'] for r in manifest['databases']], list(module.DATABASES))
            self.assertFalse(manifest['restore_verified'])
            self.assertFalse(manifest['config_captured'])
            self.assertEqual(len(calls), 6)
            self.assertTrue(all(c[0] in ('pg_dump', 'pg_restore') for c in calls))
            self.assertNotIn('password', repr(calls).lower())

    def test_failure_is_private_and_does_not_publish(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / 'generation'
            with patch.object(module.subprocess, 'run', return_value=subprocess.CompletedProcess([], 1, b'', b'secret server details')):
                with self.assertRaisesRegex(RuntimeError, '^native pg_dump failed for chaptarr_main$'):
                    module.capture(target)
            self.assertFalse((target / 'manifest.json').exists())


if __name__ == '__main__':
    unittest.main()
