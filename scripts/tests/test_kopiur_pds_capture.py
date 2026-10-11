"""PDS-specific capture regression tests. Native Linux execution belongs to ARC."""
import ast
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / 'scripts/kopiur-pds-capture.cjs'


class CaptureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.store = self.base / 'store'
        self.store.mkdir()
        (self.store / 'nested').mkdir()
        (self.store / 'empty').mkdir()
        (self.store / 'nested/.private').write_bytes(b'original-store')
        os.chmod(self.store / 'nested/.private', 0o600)
        self.control = self.base / 'supervisor.sh'
        self.control.write_text('''#!/bin/sh
set -eu
printf '%s\\n' "$1" >>"$CONTROL_LOG"
if [ "$1" = acquire ] && [ "${FAIL_ACQUIRE:-}" = 1 ]; then exit 1; fi
if [ "$1" = check ] && [ "${FAIL_CHECK:-}" = 1 ]; then exit 1; fi
if [ "$1" = release ] && [ "${FAIL_RELEASE:-}" = 1 ]; then exit 1; fi
if [ "$1" = check ] && [ "${MUTATE:-}" = 1 ]; then
  printf changed >"$PDS_DATA_DIRECTORY/nested/.private"
fi
''')
        self.env = os.environ | {'PDS_DATA_DIRECTORY': str(self.store),
            'PDS_CAPTURE_SUPERVISOR': str(self.control), 'CONTROL_LOG': str(self.base / 'calls'),
            'PDS_JWT_SECRET': 'fixture-jwt', 'PDS_ADMIN_PASSWORD': 'fixture-admin',
            'PDS_PLC_ROTATION_KEY_K256_PRIVATE_KEY_HEX': '01' * 32}

    def run_capture(self, **env):
        effective = self.env | env
        command = ['node', str(SCRIPT), 'capture']
        if os.environ.get('RUNNER_NAME'):
            image = 'ghcr.io/bluesky-social/pds@sha256:74c650cd620eee2e6e0272b4cb76036a0271523b1742f61f813b79dc5a8b5273'
            command = ['docker', 'run', '--rm', '--network=none', '--read-only',
                       '--cap-drop=ALL', '--security-opt=no-new-privileges',
                       '--user', str(os.getuid()) + ':' + str(os.getgid()),
                       '-v', str(self.base) + ':' + str(self.base),
                       '-v', str(SCRIPT.parent) + ':' + str(SCRIPT.parent) + ':ro']
            for key, value in effective.items():
                if key.startswith('PDS_') or key in ('CONTROL_LOG', 'FAIL_ACQUIRE', 'FAIL_CHECK', 'FAIL_RELEASE', 'MUTATE'):
                    command += ['-e', key + '=' + value]
            command += ['--entrypoint', 'node', image, str(SCRIPT), 'capture']
        return subprocess.run(command, env=effective, capture_output=True, text=True, timeout=30)

    def test_complete_whole_store_and_original_runtime(self):
        for _ in range(2):
            result = self.run_capture()
            self.assertEqual(result.returncode, 0, result.stderr)
            bundle = self.store / '.kopiur-pds'
            self.assertEqual((bundle / 'COMPLETE').read_text(), 'complete\n')
            current = bundle / 'current'
            with tarfile.open(current / 'application-state.tar') as archive:
                names = archive.getnames()
                self.assertIn('./nested/.private', names)
                self.assertIn('./empty', names)
                self.assertFalse(any('.kopiur-pds' in n for n in names))
                self.assertEqual(archive.extractfile('./nested/.private').read(), b'original-store')
                self.assertEqual(archive.getmember('./nested/.private').mode, 0o600)
            runtime = json.loads((current / 'runtime.json').read_text())
            self.assertEqual(runtime['PDS_JWT_SECRET'], self.env['PDS_JWT_SECRET'])
            self.assertNotIn('CONTROL_LOG', runtime)
            self.assertEqual((current / 'runtime.json').stat().st_mode & 0o777, 0o600)
            for row in (current / 'SHA256SUMS').read_text().splitlines():
                digest, name = row.split('  ')
                self.assertEqual(hashlib.sha256((current / name).read_bytes()).hexdigest(), digest)

    def test_failed_capture_invalidates_old_complete_and_releases(self):
        self.assertEqual(self.run_capture().returncode, 0)
        self.assertNotEqual(self.run_capture(FAIL_CHECK='1').returncode, 0)
        bundle = self.store / '.kopiur-pds'
        self.assertFalse((bundle / 'COMPLETE').exists())
        self.assertFalse((bundle / 'pending').exists())
        self.assertEqual((self.base / 'calls').read_text().splitlines()[-1], 'release')

    def test_changes_links_and_missing_escrow_fail_closed(self):
        for kind in ('mutation', 'symlink', 'hardlink', 'missing-runtime'):
            with self.subTest(kind=kind):
                env = {}
                link = self.store / 'other'
                if kind == 'mutation': env['MUTATE'] = '1'
                elif kind == 'symlink': link.symlink_to(self.store / 'nested/.private')
                elif kind == 'hardlink': os.link(self.store / 'nested/.private', link)
                else: env['PDS_JWT_SECRET'] = ''
                result = self.run_capture(**env)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse((self.store / '.kopiur-pds/COMPLETE').exists())
                self.assertNotIn('fixture-admin', result.stderr + result.stdout)
                if link.is_symlink() or link.exists(): link.unlink()

    def test_release_failure_and_later_preflight_invalidate_success(self):
        self.assertEqual(self.run_capture().returncode, 0)
        result = self.run_capture(FAIL_RELEASE='1')
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.store / '.kopiur-pds/COMPLETE').exists())
        self.assertNotIn('"complete":true', result.stdout)
        self.assertEqual(self.run_capture().returncode, 0)
        (self.store / '.kopiur-pds/pending').symlink_to(self.base / 'absent')
        self.assertNotEqual(self.run_capture().returncode, 0)
        self.assertFalse((self.store / '.kopiur-pds/COMPLETE').exists())

    def test_embedded_native_python_compiles(self):
        tree = ast.parse((SCRIPT.parent / 'kopiur_pds_native.py').read_text())
        for call in ast.walk(tree):
            if not isinstance(call, ast.Call): continue
            for i, arg in enumerate(call.args):
                if isinstance(arg, ast.Constant) and arg.value == 'python3':
                    code = call.args[i + 2]
                    if isinstance(code, ast.Constant) and isinstance(code.value, str):
                        ast.parse(code.value)
        source = next(n.value for n in ast.walk(tree) if isinstance(n, ast.Constant)
                      and isinstance(n.value, str) and "COMPLETE" in n.value)
        self.assertIn("'complete\\n'", source)

    def test_acquisition_failure_and_broken_helper_symlink(self):
        self.assertNotEqual(self.run_capture(FAIL_ACQUIRE='1').returncode, 0)
        (self.store / '.kopiur-pds/pending').symlink_to(self.base / 'absent')
        self.assertNotEqual(self.run_capture().returncode, 0)
        self.assertFalse((self.store / '.kopiur-pds/COMPLETE').exists())


if __name__ == '__main__':
    unittest.main()
