#!/usr/bin/env python3
"""CWA startup-only Books hold regression tests, no production writes."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / 'kubernetes/apps/media/calibre-web-automated/app/kopiur-startup-hold.py'
spec = importlib.util.spec_from_file_location('cwa_hold', SOURCE)
assert spec is not None and spec.loader is not None
hold = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hold)
IDENTITY = {'pod_uid': '12345678-1234-1234-1234-123456789abc', 'boot_id': 'boot',
            'pid1_start': 123, 'command_sha256': 'a'*64}


class ExecInit(BaseException):
    pass


class StartupHoldTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name) / 'hold'
        self.state.mkdir(mode=0o700)
        self.marker = dict(IDENTITY, nonce='b'*64, deadline=200,
                           monotonic_end=150.0)
        self.patches = [patch.object(hold, 'STATE', self.state),
                        patch.object(hold, 'identity', return_value=IDENTITY),
                        patch.object(hold.time, 'time', return_value=100),
                        patch.object(hold.time, 'monotonic', return_value=50),
                        patch.object(hold.os, 'getpid', return_value=1),
                        patch.object(hold.os, 'geteuid', return_value=0),
                        patch.object(hold.signal, 'signal')]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)
        # Local non-root test owners differ from the root native container.
        self.real_secure = hold.secure
        def secure(path, directory=False):
            from unittest.mock import MagicMock
            original = path.lstat()
            info = MagicMock(st_mode=original.st_mode, st_uid=0)
            with patch.object(Path, 'lstat', return_value=info):
                self.real_secure(path, directory)
        self.secure = patch.object(hold, 'secure', side_effect=secure)
        self.secure.start()
        self.addCleanup(self.secure.stop)
        self.write()

    def write(self):
        target = self.state / 'active.json'
        target.write_text(json.dumps(self.marker))
        target.chmod(0o600)

    def test_valid_identity_and_minimum(self):
        self.assertEqual(hold.read_marker(20), self.marker)
        with self.assertRaises(ValueError):hold.read_marker(101)
        for bad in (0, -1, True, 1801):
            with self.assertRaises(ValueError):hold.read_marker(bad)

    def test_replacement_nonce_and_complete_expected_contract(self):
        with patch.object(hold, 'identity', return_value=dict(IDENTITY, pid1_start=124)):
            with self.assertRaises(ValueError):hold.read_marker(1)
        self.marker['nonce'] = 'bad'
        self.write()
        with self.assertRaises(ValueError):hold.read_marker(1)

    def test_wall_and_monotonic_expiry_both_fail_closed(self):
        with patch.object(hold.time, 'time', return_value=201):
            with self.assertRaises(ValueError):hold.read_marker(1)
        with patch.object(hold.time, 'time', return_value=0), patch.object(hold.time, 'monotonic', return_value=151):
            with self.assertRaises(ValueError):hold.read_marker(1)
        self.marker['monotonic_end'] = float('nan')
        self.write()
        with self.assertRaises(ValueError):hold.read_marker(1)

    def test_final_second_evidence_is_not_a_capture_guard(self):
        with patch.object(hold.time, 'time', return_value=199.5), patch.object(hold.time, 'monotonic', return_value=149.5):
            self.assertEqual(hold.read_marker(None), self.marker)
            with self.assertRaises(ValueError):hold.read_marker(1)

    def test_absent_evidence_has_distinct_exit(self):
        (self.state/'active.json').unlink()
        with patch('sys.argv', ['hold', 'evidence']):
            with self.assertRaises(SystemExit) as error:hold.main()
            self.assertEqual(error.exception.code, 3)

    def test_permissions_and_symlinks_rejected(self):
        target = self.state / 'active.json'
        target.chmod(0o644)
        with self.assertRaises(ValueError):hold.read_marker(1)
        target.unlink()
        other = Path(self.temp.name)/'target'
        other.write_text('do not touch')
        target.symlink_to(other)
        with self.assertRaises(ValueError):hold.read_marker(1)
        with self.assertRaises(ValueError):hold.remove_marker()
        self.assertEqual(other.read_text(), 'do not touch')

    def test_bad_expiry_executes_native_init_without_hold(self):
        for deadline in (99, 100, 2000):
            with patch.object(hold.os, 'execv', side_effect=ExecInit) as execv:
                with self.assertRaises(ExecInit):hold.start(deadline)
                execv.assert_called_once_with('/init', ['/init'])

    def test_only_native_root_pid1_can_start(self):
        with patch.object(hold.os, 'getpid', return_value=2):
            with self.assertRaises(ValueError):hold.start(200)
        with patch.object(hold.os, 'geteuid', return_value=568):
            with self.assertRaises(ValueError):hold.start(200)

    def test_expiry_removes_evidence_and_execs_native_init(self):
        clock = [50.0]
        def sleep(seconds):clock[0] += seconds
        with patch.object(hold.time, 'monotonic', side_effect=lambda:clock[0]), patch.object(hold.time, 'sleep', side_effect=sleep), patch.object(hold.os, 'execv', side_effect=ExecInit) as execv:
            with self.assertRaises(ExecInit):hold.start(101)
            execv.assert_called_once_with('/init', ['/init'])
        self.assertFalse((self.state/'active.json').exists())

    def test_cli_check_rejects_old_holder(self):
        with patch('sys.argv', ['hold', 'check', json.dumps(dict(self.marker, nonce='c'*64)), '10']):
            with self.assertRaises(ValueError):hold.main()
        with patch('sys.argv', ['hold', 'check', json.dumps(self.marker), '10']):
            hold.main()


class NativeProofTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        native_spec = importlib.util.spec_from_file_location('cwa_native', ROOT / 'scripts/kopiur-cwa-startup-hold-native.py')
        assert native_spec is not None and native_spec.loader is not None
        cls.native = importlib.util.module_from_spec(native_spec)
        native_spec.loader.exec_module(cls.native)

    def test_create_timeout_still_attempts_owned_cleanup(self):
        import subprocess
        from types import SimpleNamespace
        def docker(*args, **_kwargs):
            if args[0] == 'create':raise subprocess.TimeoutExpired('docker', 120)
            return SimpleNamespace(stdout=b'', returncode=0)
        with patch.object(self.native, 'docker', side_effect=docker), patch.object(self.native, 'cleanup') as cleanup:
            with self.assertRaises(subprocess.TimeoutExpired):self.native.exercise(False)
            cleanup.assert_called_once()
            name, owner = cleanup.call_args.args
            self.assertEqual(name, 'k92-cwa-startup-' + owner)

    def test_early_release_fails(self):
        with patch.object(self.native, 'marker', return_value=None), patch.object(self.native.time, 'time', return_value=100), patch.object(self.native.time, 'monotonic', return_value=50):
            with self.assertRaises(AssertionError):self.native.await_expiry('owned', {'deadline':200, 'monotonic_end':150})

    def test_release_at_monotonic_expiry_is_valid(self):
        with patch.object(self.native, 'marker', return_value=None), patch.object(self.native.time, 'time', return_value=100), patch.object(self.native.time, 'monotonic', return_value=150):
            self.native.await_expiry('owned', {'deadline':200, 'monotonic_end':150})

    def test_early_native_services_fail(self):
        expected = {'deadline':200, 'monotonic_end':150}
        with patch.object(self.native, 'marker', return_value=expected), patch.object(self.native, 'no_native_services', return_value=False), patch.object(self.native.time, 'time', return_value=100), patch.object(self.native.time, 'monotonic', return_value=50):
            with self.assertRaises(AssertionError):self.native.await_expiry('owned', expected)

    def test_native_service_roster_can_finish_after_http_readiness(self):
        from types import SimpleNamespace
        first = [SimpleNamespace(returncode=0, stdout=b'false true -1') for _ in self.native.SERVICES]
        second = [SimpleNamespace(returncode=0, stdout=b'true true 123') for _ in self.native.SERVICES]
        with patch.object(self.native, 'docker', side_effect=first + second):
            self.assertTrue(all(x == ['no-native-pid'] for x in self.native.service_states('owned').values()))
            self.assertTrue(all(x == ['true', 'true'] for x in self.native.service_states('owned').values()))

    def test_transport_or_invalid_evidence_is_not_absence(self):
        from types import SimpleNamespace
        with patch.object(self.native, 'docker', return_value=SimpleNamespace(returncode=1)):
            with self.assertRaises(RuntimeError):self.native.marker('owned')
        with patch.object(self.native, 'docker', return_value=SimpleNamespace(returncode=3)):
            self.assertIsNone(self.native.marker('owned'))

    def test_cleanup_cannot_remove_other_owners(self):
        from types import SimpleNamespace
        with patch.object(self.native, 'docker', side_effect=[SimpleNamespace(stdout=b'owned-id\n'), SimpleNamespace(stdout=json.dumps([{'Config':{'Labels':{'k92.cwa-startup-owner':'another-owner'}}}]).encode())]) as docker:
            with self.assertRaises(AssertionError):self.native.cleanup('name', 'expected-owner')
            self.assertEqual(docker.call_count, 2)


if __name__ == '__main__':
    unittest.main()
