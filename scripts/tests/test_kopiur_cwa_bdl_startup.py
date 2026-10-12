#!/usr/bin/env python3
"""CWA-BDL exact-image startup prerequisite regression tests."""
import ast
import importlib.util
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace
import unittest
from unittest.mock import call, patch

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('bdl_fixture', ROOT / 'scripts/kopiur-cwa-bdl-startup-native.py')
assert spec and spec.loader
native = importlib.util.module_from_spec(spec)
spec.loader.exec_module(native)


class BdlStartupTest(unittest.TestCase):
    def test_native_entrypoint_preserved(self):
        self.assertEqual(native.SHIM, '#!/bin/sh\nexec /usr/bin/dumb-init -- /app/entrypoint.sh\n')
        self.assertEqual(native.PYTHON, '/app/.venv/bin/python3')
        self.assertEqual(native.SOURCE, ROOT / 'kubernetes/apps/media/calibre-web-automated/app/kopiur-startup-hold.py')

    def test_invalid_transport_is_not_absent(self):
        with patch.object(native, 'docker', return_value=SimpleNamespace(returncode=1)):
            with self.assertRaises(RuntimeError): native.marker('owned')
        with patch.object(native, 'docker', return_value=SimpleNamespace(returncode=3)):
            self.assertIsNone(native.marker('owned'))

    def test_status_payload_is_read(self):
        with patch.object(native, 'docker', return_value=SimpleNamespace(returncode=0, stdout=b'{"nonce":"owned"}')):
            self.assertEqual(native.marker('owned'), {'nonce':'owned'})

    def test_probe_passes_complete_identity(self):
        evidence = {'nonce':'owned', 'deadline':300}
        with patch.object(native, 'docker', return_value=SimpleNamespace(returncode=0)) as docker:
            self.assertTrue(native.probe('owned', evidence, 25))
            self.assertEqual(json.loads(docker.call_args.args[-2]), evidence)
            self.assertEqual(docker.call_args.args[-1], '25')

    def test_early_release_rejected(self):
        with patch.object(native, 'marker', return_value=None), patch.object(native.time, 'time', return_value=100), patch.object(native.time, 'monotonic', return_value=50):
            with self.assertRaises(AssertionError): native.await_expiry('owned', {'deadline':200, 'monotonic_end':150})

    def test_expired_release_admitted(self):
        with patch.object(native, 'marker', return_value=None), patch.object(native.time, 'time', return_value=100), patch.object(native.time, 'monotonic', return_value=150):
            native.await_expiry('owned', {'deadline':200, 'monotonic_end':150})

    def test_early_writer_rejected(self):
        expected = {'deadline':200, 'monotonic_end':150}
        with patch.object(native, 'marker', return_value=expected), patch.object(native, 'no_native_writer', return_value=False), patch.object(native.time, 'time', return_value=100), patch.object(native.time, 'monotonic', return_value=50):
            with self.assertRaises(AssertionError): native.await_expiry('owned', expected)

    def test_changed_evidence_rejected(self):
        with patch.object(native, 'marker', return_value={'nonce':'different'}):
            with self.assertRaises(AssertionError): native.await_expiry('owned', {'deadline':0, 'monotonic_end':0})

    def test_create_failure_owns_cleanup(self):
        def docker(*args, **kwargs):
            if args[0] == 'create': raise subprocess.TimeoutExpired('docker',120)
            return SimpleNamespace(returncode=0, stdout=b'')
        with patch.object(native, 'docker', side_effect=docker), patch.object(native.fixture, 'cleanup') as cleanup:
            with self.assertRaises(subprocess.TimeoutExpired): native.exercise(False)
            cleanup.assert_called_once()
            name, owner = cleanup.call_args.args
            self.assertEqual(name, 'k92-cwa-startup-' + owner)

    def test_monitor_cleanup_failure_still_cleans_primary(self):
        with patch.object(native.fixture, 'cleanup', side_effect=[RuntimeError('monitor'), None]) as cleanup:
            with self.assertRaises(RuntimeError):
                native.cleanup_pair('primary', 'monitor', 'owner', True, True)
            self.assertEqual(cleanup.call_args_list, [call('monitor', 'owner'), call('primary', 'owner')])

    def test_fixed_expiry_replacement_and_monitor_scope(self):
        source = (ROOT / 'scripts/kopiur-cwa-bdl-startup-native.py').read_text()
        tree = ast.parse(source)
        constants = [n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value,str)]
        bootstrap = next(v for v in constants if v.startswith('if [ ! -e /fixture-deadline ]'))
        self.assertIn('>/fixture-deadline; fi;', bootstrap)
        self.assertIn('assert second[\'deadline\'] == first[\'deadline\']', source)
        self.assertIn('--network=none', constants)
        self.assertIn('--tmpfs=/books:ro,size=8m', constants)
        self.assertIn('--read-only', constants)
        self.assertNotIn('--privileged', constants)
        self.assertNotIn('--network=host', constants)


if __name__ == '__main__':
    unittest.main()
