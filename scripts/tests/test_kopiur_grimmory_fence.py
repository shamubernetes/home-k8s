#!/usr/bin/env python3
"""Local gates for the app-local fence, with real Linux behavior proved on ARC."""
import ast
from pathlib import Path
import subprocess
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[2]
FENCE = ROOT / 'scripts/kopiur-grimmory-fence.sh'
NATIVE = ROOT / 'scripts/kopiur-grimmory-fence-native.py'


class FenceContract(unittest.TestCase):
    def setUp(self):
        self.source = FENCE.read_text()

    def test_shell_syntax(self):
        self.assertEqual(subprocess.run(['sh', '-n', str(FENCE)], capture_output=True).returncode, 0)

    def test_numeric_inputs(self):
        declaration = self.source.split('number() {', 1)[1].split('\n', 1)[0]
        code = 'number() {' + declaration + '\nnumber "$1"'
        for value in ('1', '1800', '1791834000', '1791834000000'):
            self.assertEqual(subprocess.run(['sh', '-c', code, 'number', value]).returncode, 0, value)
        for value in ('', '0', '001', '-1', '1;true', '2 3', '17918340000000'):
            self.assertNotEqual(subprocess.run(['sh', '-c', code, 'number', value]).returncode, 0, value)

    def test_missing_and_unknown_modes_reject(self):
        for args in ([], ['bad'], ['check'], ['release'], ['acquire'], ['supervise', 'true']):
            self.assertNotEqual(subprocess.run(['sh', str(FENCE), *args], capture_output=True).returncode, 0, args)

    def test_private_root_control(self):
        self.assertIn("= 0:700", self.source)
        self.assertIn("= 0:600", self.source)
        self.assertIn('[ ! -L "$state/$name" ]', self.source)
        self.assertIn('flock -n 8', self.source)
        self.assertIn('(set -C;', self.source)
        self.assertNotIn('eval ', self.source)
        self.assertNotIn('source ', self.source)

    def test_identity_and_entire_group(self):
        self.assertIn('568:568:568:568', self.source)
        self.assertIn('"$(birth "$pid")" = "$started"', self.source)
        self.assertIn('"$pid:$pid"', self.source)
        self.assertIn('"$native"/task/[0-9]*/status', self.source)
        self.assertIn('case "$kind" in T|Z)', self.source)
        self.assertIn('su-exec 568:568 /bin/kill', self.source)

    def test_fixed_absolute_and_monotonic_bounds(self):
        self.assertIn('max_hold=1800', self.source)
        self.assertIn('mono_remaining=$((end - $(monotonic)))', self.source)
        self.assertIn('remaining=$((deadline - $(date +%s)))', self.source)
        self.assertIn('while read_request && [ "$(monotonic)" -lt "$restart_end" ]; do sleep 0.25; done', self.source)
        self.assertIn('cmp -s "$state/request" "$state/active"', self.source)

    def test_autonomous_resume_and_term_cleanup(self):
        self.assertIn('signal_group CONT || reject', self.source)
        cleanup = self.source.split('cleanup() {', 1)[1].split('trap cleanup', 1)[0]
        self.assertNotIn('signal_group CONT', cleanup)
        self.assertIn('signal_group KILL', cleanup)
        self.assertNotIn('kill -0 "$child"', self.source)
        self.assertIn('child_alive', self.source)
        self.assertIn('trap cleanup EXIT', self.source)
        self.assertIn('rm -f "$state/active" "$state/child"', self.source)

    def test_changed_uptime_epoch_keeps_absolute_startup_hold(self):
        prefix = self.source.split('read_child() {', 1)[0]
        old = '00000000-0000-4000-8000-000000000001'
        new = '00000000-0000-4000-8000-000000000002'
        for same_epoch in (False, True):
            with tempfile.TemporaryDirectory(prefix='grimm-fence-test-') as temp:
                path = Path(temp)
                deadline = int(time.time()) + 25
                (path / 'request').write_text(f'{old} {"a" * 32} {deadline} 1 {old}\n')
                program = prefix.replace('state=/runtime-escrow/fence', f'state={temp}')
                program = program.replace('$(cat /proc/sys/kernel/random/boot_id)', old if same_epoch else new)
                program += '\nsecure() { return 0; }\nmonotonic() { printf 999999999; }\nread_request\n'
                result = subprocess.run(['sh', '-c', program], capture_output=True)
                self.assertEqual(result.returncode == 0, not same_epoch)

    def test_real_native_not_assert_or_external(self):
        native = NATIVE.read_text()
        self.assertFalse(any(isinstance(n, ast.Assert) for n in ast.walk(ast.parse(native))))
        for value in ('--network', "'none'", "'--log-driver=none'", "'--cap-drop=ALL'", 'owned_cleanup_verified', 'production_shared_books_acceptance', 'replacement_preserves_absolute_expiry'):
            self.assertIn(value, native)
        self.assertNotIn('--privileged', native)
        self.assertNotIn('SYS_PTRACE', native)
        self.assertNotIn('SYS_ADMIN', native)
        self.assertNotIn('kubectl', native)
        self.assertNotIn('assert ', native)


if __name__ == '__main__':
    unittest.main()
