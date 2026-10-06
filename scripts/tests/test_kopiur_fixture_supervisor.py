"""Real kernel cgroup tests. Run ONLY in the isolated trusted ARC supervisor."""
import json
import os
from pathlib import Path
import sys
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import kopiur_fixture_supervisor as supervisor
from kopiur_fixture_supervisor import BoundaryError, FixtureSupervisor


class KernelBoundaryTests(unittest.TestCase):
    def setUp(self):
        if os.environ.get('K8S92_CGROUP_FIXTURE') != 'isolated-arc-only':
            self.fail('requires explicitly admitted isolated ARC cgroup fixture')
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name)
        os.chmod(self.directory, 0o700)
        relative = Path('/proc/self/cgroup').read_text().strip().split('::')[1]
        self.root = Path('/sys/fs/cgroup') / relative.lstrip('/')
        self.controller = FixtureSupervisor(self.directory, self.root)
        self.controller.begin()
        self.children = []

    def tearDown(self):
        # Only UUID-owned boundaries created by this test are eligible cleanup.
        proof = self.controller.cease()
        for child in self.children:
            child.wait(timeout=5)
        _, boundary = self.controller._read()
        boundary.rmdir()
        self.assertFalse(boundary.exists())
        print(json.dumps({'test': self.id(), 'kernel_cessation': proof,
                          'owned_boundary_removed': True}), flush=True)
        self.temporary.cleanup()

    def command(self, code):
        child, registration = self.controller.dispatch([sys.executable, '-c', code])
        self.children.append(child)
        return child, registration

    def wait_file(self, path):
        deadline = time.monotonic() + 5
        while not path.exists():
            if time.monotonic() >= deadline:
                self.fail('native worker did not acknowledge')
            time.sleep(0.01)

    def test_registration_precedes_dispatch_and_worker_has_no_authority(self):
        # No controller environment, socket, journal, or writable cgroup is
        # supplied. A public fixture result is the only worker-owned file.
        with tempfile.TemporaryDirectory() as public:
            os.chmod(public, 0o777)
            result = Path(public) / 'result'
            _, boundary = self.controller._read()
            code = f"""
import json, os
from pathlib import Path
assert os.getuid() == 65534
assert os.getgid() == 65534 and os.getgroups() == []
status = Path('/proc/self/status').read_text()
assert 'NoNewPrivs:\t1' in status
assert 'CapEff:\t0000000000000000' in status
assert not os.access({str(self.directory)!r}, os.R_OK)
assert not os.access({str(boundary / 'cgroup.procs')!r}, os.W_OK)
assert 'DOCKER_HOST' not in os.environ
Path({str(result)!r}).write_text(json.dumps({{'uid': os.getuid()}}))
"""
            child, registered = self.command(code)
            self.wait_file(result)
            self.assertEqual(child.wait(timeout=5), 0)
            state, _ = self.controller._read()
            self.assertIn(registered, state['commands'])
            self.assertEqual(registered['stage'], 'registered')
            with self.assertRaises(BoundaryError):
                self.controller.verify_cessation()
            self.controller.cease()
            self.assertEqual(self.controller.verify_cessation()['populated'], 0)

    def test_setsid_double_fork_descendant_is_in_complete_boundary(self):
        with tempfile.TemporaryDirectory() as public:
            os.chmod(public, 0o777)
            marker = Path(public) / 'descendant'
            code = f"""
import os, time
from pathlib import Path
if os.fork():
    os._exit(0)
os.setsid()
if os.fork():
    os._exit(0)
Path({str(marker)!r}).write_text(str(os.getpid()))
while True:
    time.sleep(1)
"""
            child, _ = self.command(code)
            self.wait_file(marker)
            self.assertEqual(child.wait(timeout=5), 0)
            descendant = marker.read_text()
            _, boundary = self.controller._read()
            self.assertIn(descendant, (boundary / 'cgroup.procs').read_text().split())
            self.assertIn('populated 1', (boundary / 'cgroup.events').read_text())
            with self.assertRaises(BoundaryError):
                self.controller.verify_cessation()
            proof = self.controller.cease()
            self.assertEqual(proof['populated'], 0)
            self.assertEqual((boundary / 'cgroup.procs').read_text(), '')

    def test_restart_reconciles_live_command_and_refuses_queued_start(self):
        self.command('import time; time.sleep(60)')
        restarted = FixtureSupervisor(self.directory, self.root)
        restarted.revoke()
        with self.assertRaisesRegex(BoundaryError, 'queued start denied'):
            restarted.dispatch([sys.executable, '-c', 'raise RuntimeError("late side effect")'])
        self.assertEqual(restarted.cease()['populated'], 0)
        self.assertEqual(restarted.verify_cessation()['populated'], 0)

    def test_registration_failure_eof_gate_prevents_side_effect(self):
        with tempfile.TemporaryDirectory() as public:
            os.chmod(public, 0o777)
            marker = Path(public) / 'must-not-exist'
            children = []
            original = subprocess.Popen
            def remember(*args, **kwargs):
                child = original(*args, **kwargs)
                children.append(child)
                return child
            with patch.object(supervisor.subprocess, 'Popen', side_effect=remember):
                with patch.object(supervisor, '_persist', side_effect=OSError('injected fsync failure')):
                    with self.assertRaises(OSError):
                        self.command(f'from pathlib import Path; Path({str(marker)!r}).touch()')
            self.assertEqual(children[0].returncode, 125)
            self.assertFalse(marker.exists())

    def test_supervisor_process_loss_after_registration_before_gate(self):
        with tempfile.TemporaryDirectory() as public:
            os.chmod(public, 0o777)
            marker = Path(public) / 'must-not-exist'
            pid = os.fork()
            if pid == 0:
                original = supervisor._persist
                def crash(path, state):
                    original(path, state)
                    os._exit(71)
                supervisor._persist = crash
                self.controller.dispatch([sys.executable, '-c',
                    f'from pathlib import Path; Path({str(marker)!r}).touch()'])
                os._exit(72)
            _, status = os.waitpid(pid, 0)
            self.assertEqual(os.waitstatus_to_exitcode(status), 71)
            restarted = FixtureSupervisor(self.directory, self.root)
            self.assertEqual(len(restarted._read()[0]['commands']), 1)
            # Observe launcher exit on EOF BEFORE any termination request.
            _, boundary = restarted._read()
            deadline = time.monotonic() + 5
            while 'populated 0' not in (boundary / 'cgroup.events').read_text():
                if time.monotonic() >= deadline:
                    self.fail('abandoned gated launcher did not exit')
                time.sleep(0.01)
            self.assertFalse(marker.exists())

    def test_gate_stays_closed_until_registration_directory_fsync(self):
        with tempfile.TemporaryDirectory() as public:
            os.chmod(public, 0o777)
            marker = Path(public) / 'started'
            original = supervisor._persist
            def barrier(path, state):
                pid = state['commands'][-1]['pid']
                deadline = time.monotonic() + 5
                while Path(f'/proc/{pid}/wchan').read_text().strip() not in ('pipe_read', 'anon_pipe_read'):
                    if time.monotonic() >= deadline:
                        self.fail('launcher did not wait at closed gate')
                    time.sleep(0.01)
                self.assertFalse(marker.exists())
                original(path, state)
                self.assertFalse(marker.exists())
            with patch.object(supervisor, '_persist', side_effect=barrier):
                child, _ = self.command(f'from pathlib import Path; Path({str(marker)!r}).touch()')
            self.wait_file(marker)
            self.assertEqual(child.wait(timeout=5), 0)

    def test_directory_fsync_failure_after_replace_never_opens_gate(self):
        with tempfile.TemporaryDirectory() as public:
            os.chmod(public, 0o777)
            marker = Path(public) / 'must-not-exist'
            original = os.fsync
            calls = []
            children = []
            popen = subprocess.Popen
            def remember(*args, **kwargs):
                child = popen(*args, **kwargs)
                children.append(child)
                return child
            def fail_directory(fd):
                calls.append(fd)
                if len(calls) == 2:
                    raise OSError('injected registration directory fsync failure')
                original(fd)
            with patch.object(supervisor.subprocess, 'Popen', side_effect=remember):
                with patch.object(supervisor.os, 'fsync', side_effect=fail_directory):
                    with self.assertRaises(OSError):
                        self.command(f'from pathlib import Path; Path({str(marker)!r}).touch()')
            self.assertEqual(children[0].returncode, 125)
            self.assertEqual(len(self.controller._read()[0]['commands']), 1)
            self.assertFalse(marker.exists())

    def test_lookalike_filesystem_is_not_a_kernel_boundary(self):
        with tempfile.TemporaryDirectory() as fake:
            root = Path(fake)
            (root / 'cgroup.controllers').touch()
            with self.assertRaisesRegex(BoundaryError, 'real kernel'):
                FixtureSupervisor(self.directory, root)

    def test_changed_boundary_identity_never_proves_cessation(self):
        state, _ = self.controller._read()
        original = state['boundary'].copy()
        state['boundary']['inode'] += 1
        supervisor._persist(self.controller.path, state)
        try:
            with self.assertRaisesRegex(BoundaryError, 'identity changed'):
                self.controller.cease()
        finally:
            state['boundary'] = original
            supervisor._persist(self.controller.path, state)

    def test_old_generation_is_not_overwritten(self):
        original = self.controller.path.read_bytes()
        with self.assertRaises(BoundaryError):
            self.controller.begin()
        self.assertEqual(original, self.controller.path.read_bytes())


if __name__ == '__main__':
    unittest.main()
