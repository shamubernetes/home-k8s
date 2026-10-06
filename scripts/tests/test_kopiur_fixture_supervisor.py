"""Real kernel cgroup tests. Run ONLY in the isolated trusted ARC supervisor."""
import json
import os
from pathlib import Path
import signal
import sys
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import kopiur_fixture_supervisor as supervisor
import kopiur_fixture_controller as controller_module
import kopiur_fixture_admission as admission_module
from kopiur_fixture_supervisor import BoundaryError, FixtureSupervisor
from kopiur_fixture_controller import FixtureController
from fixture_admission_cases import NativeAdmissionCases


class KernelBoundaryTests(NativeAdmissionCases, unittest.TestCase):
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

    def test_new_process_reconciles_admitted_worker_after_supervisor_loss(self):
        with tempfile.TemporaryDirectory() as public:
            os.chmod(public, 0o777)
            marker = Path(public) / 'admitted'
            pid = os.fork()
            if pid == 0:
                child, registration = self.controller.dispatch([sys.executable, '-c',
                    f'import time; from pathlib import Path; '
                    f'Path({str(marker)!r}).touch(); time.sleep(60)'])
                os._exit(73)
            _, status = os.waitpid(pid, 0)
            self.assertEqual(os.waitstatus_to_exitcode(status), 73)
            self.wait_file(marker)
            _, boundary = self.controller._read()
            self.assertIn('populated 1', (boundary / 'cgroup.events').read_text())
            code = ('from kopiur_fixture_supervisor import FixtureSupervisor; '
                    f's = FixtureSupervisor({str(self.directory)!r}, {str(self.root)!r}); '
                    's.cease(); assert s.verify_cessation()["populated"] == 0')
            subprocess.run([sys.executable, '-c', code],
                           env={'PYTHONPATH': str(Path(supervisor.__file__).parent)},
                           check=True, timeout=10)
            self.assertEqual(self.controller.verify_cessation()['populated'], 0)

    def test_independent_revocation_kills_stopped_native_worker(self):
        with tempfile.TemporaryDirectory() as public:
            os.chmod(public, 0o777)
            marker = Path(public) / 'admitted'
            child, _ = self.command(f'import time; from pathlib import Path; '
                                   f'Path({str(marker)!r}).touch(); time.sleep(60)')
            self.wait_file(marker)
            os.kill(child.pid, signal.SIGSTOP)
            code = ('from kopiur_fixture_supervisor import FixtureSupervisor; '
                    f's = FixtureSupervisor({str(self.directory)!r}, {str(self.root)!r}); '
                    's.cease(); assert s.verify_cessation()["populated"] == 0')
            subprocess.run([sys.executable, '-c', code],
                           env={'PYTHONPATH': str(Path(supervisor.__file__).parent)},
                           check=True, timeout=10)
            self.assertEqual(child.wait(timeout=5), -signal.SIGKILL)

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

    def test_successful_completion_is_durable_and_revocation_invalidates_it(self):
        child, command = self.command('pass')
        proof = self.controller.complete(command['operation'])
        self.assertEqual(child.returncode, 0)
        restarted = FixtureSupervisor(self.directory, self.root)
        self.assertEqual(restarted.verify_completion(command['operation']), proof)
        self.assertEqual(self.controller.complete(command['operation']), proof)
        restarted.revoke()
        with self.assertRaises(BoundaryError):
            self.controller.complete(command['operation'])
        with self.assertRaises(BoundaryError):
            restarted.verify_completion(command['operation'])

    def test_exited_parent_with_live_descendant_cannot_complete(self):
        with tempfile.TemporaryDirectory() as public:
            os.chmod(public, 0o777)
            marker = Path(public) / 'descendant'
            _, command = self.command(
                'import os,time; from pathlib import Path; '
                'pid=os.fork(); '
                'os._exit(0) if pid else None; os.setsid(); '
                f'Path({str(marker)!r}).touch(); time.sleep(60)')
            self.wait_file(marker)
            original = self.controller.path.read_bytes()
            with self.assertRaisesRegex(BoundaryError, 'descendants remain'):
                self.controller.complete(command['operation'])
            self.assertEqual(self.controller.path.read_bytes(), original)

    def test_nonzero_and_unknown_restart_outcomes_cannot_complete(self):
        child, command = self.command('raise SystemExit(9)')
        self.assertEqual(child.wait(timeout=5), 9)
        original = self.controller.path.read_bytes()
        with self.assertRaisesRegex(BoundaryError, 'successfully'):
            self.controller.complete(command['operation'])
        with self.assertRaisesRegex(BoundaryError, 'unknown'):
            FixtureSupervisor(self.directory, self.root).complete(command['operation'])
        self.assertEqual(self.controller.path.read_bytes(), original)

    def test_later_dispatch_invalidates_old_completion_even_after_exit(self):
        _, first = self.command('pass')
        self.controller.complete(first['operation'])
        child, second = self.command('pass')
        self.assertEqual(child.wait(timeout=5), 0)
        with self.assertRaisesRegex(BoundaryError, 'cohort changed'):
            self.controller.verify_completion(first['operation'])
        with self.assertRaisesRegex(BoundaryError, 'cohort changed'):
            self.controller.complete(first['operation'])
        self.controller.complete(second['operation'])

    def test_completion_directory_fsync_failure_retry_repeats_barrier(self):
        _, command = self.command('pass')
        original = os.fsync
        calls = []
        def fail_directory(fd):
            calls.append(fd)
            if len(calls) == 2:
                raise OSError('injected completion directory fsync failure')
            original(fd)
        with patch.object(supervisor.os, 'fsync', side_effect=fail_directory):
            with self.assertRaises(OSError):
                self.controller.complete(command['operation'])
        restarted = FixtureSupervisor(self.directory, self.root)
        with patch.object(supervisor.os, 'fsync', side_effect=OSError('still unavailable')):
            with self.assertRaises(OSError):
                restarted.verify_completion(command['operation'])
        with patch.object(supervisor.os, 'fsync', wraps=original) as barrier:
            proof = self.controller.complete(command['operation'])
            self.assertEqual(barrier.call_count, 2)
        self.assertEqual(self.controller.verify_completion(command['operation']), proof)

    def test_completion_wait_does_not_block_independent_revoke(self):
        child, command = self.command('import time; time.sleep(60)')
        original = child.wait
        def independently_cease(*args, **kwargs):
            code = ('from kopiur_fixture_supervisor import FixtureSupervisor; '
                    f's=FixtureSupervisor({str(self.directory)!r}, {str(self.root)!r}); '
                    's.cease()')
            subprocess.run([sys.executable, '-c', code], check=True, timeout=10,
                           env={'PYTHONPATH': str(Path(supervisor.__file__).parent)})
            return original(*args, **kwargs)
        with patch.object(child, 'wait', side_effect=independently_cease):
            with self.assertRaisesRegex(BoundaryError, 'successfully'):
                self.controller.complete(command['operation'], timeout=5)
        self.assertEqual(self.controller.verify_cessation()['populated'], 0)

    def test_revoke_wins_after_successful_wait_before_completion_lock(self):
        child, command = self.command('pass')
        original = child.wait
        def revoke_after_wait(*args, **kwargs):
            result = original(*args, **kwargs)
            self.assertEqual(result, 0)
            code = ('from kopiur_fixture_supervisor import FixtureSupervisor; '
                    f's=FixtureSupervisor({str(self.directory)!r}, {str(self.root)!r}); '
                    's.revoke()')
            subprocess.run([sys.executable, '-c', code], check=True, timeout=10,
                           env={'PYTHONPATH': str(Path(supervisor.__file__).parent)})
            return result
        with patch.object(child, 'wait', side_effect=revoke_after_wait):
            with self.assertRaisesRegex(BoundaryError, 'revoked generation'):
                self.controller.complete(command['operation'])
        self.assertNotIn('completion', self.controller._read()[0]['commands'][0])

    def test_returned_registration_cannot_mutate_owned_identity(self):
        _, command = self.command('pass')
        operation = command['operation']
        command['boundary']['inode'] += 1
        command['pid'] += 1
        command['argv'][-1] = 'raise RuntimeError("mutated returned argv")'
        proof = self.controller.complete(operation)
        state, _ = self.controller._read()
        self.assertEqual(proof['boundary'], state['boundary'])
        self.assertNotEqual(command['boundary'], state['boundary'])

    def make_controller(self, consumers=None):
        controller = FixtureController(self.controller)
        state = controller.begin(consumers or {'first': 'native-first'}, 30)
        return controller, state

    def controller_plan(self, controller, state, consumer, code='pass'):
        state = controller.stage(state, consumer, [sys.executable, '-c', code])
        plan_id = state['plans'][-1]['id']
        child, state = controller.dispatch(state, plan_id)
        self.children.append(child)
        return controller.complete(state, plan_id)

    def test_controller_entire_cohort_commit_seals_dispatch_and_survives_restart(self):
        controller, state = self.make_controller({'first': 'native-first', 'second': 'native-second'})
        state = self.controller_plan(controller, state, 'first')
        with self.assertRaisesRegex(BoundaryError, 'entire authoritative'):
            controller.prepare(state)
        state = self.controller_plan(controller, state, 'second')
        with self.assertRaises(BoundaryError):
            controller.commit(state)
        prepared = controller.prepare(state)
        self.assertEqual(controller.verify_prepared(prepared)['consumers'], state['consumers'])
        with self.assertRaises(BoundaryError):
            controller.commit(state)
        state = prepared
        state = controller.commit(state)
        restarted = FixtureController(FixtureSupervisor(self.directory, self.root))
        receipt = restarted.verify_terminal(state)
        self.assertFalse(receipt['production_recovery_accepted'])
        self.assertEqual(len(receipt['operations']), 2)
        with self.assertRaisesRegex(BoundaryError, 'queued start denied'):
            self.command('raise RuntimeError("after terminal")')
        self.controller.revoke()
        with self.assertRaises(BoundaryError):
            restarted.verify_terminal(state)

    def test_controller_stale_snapshot_cannot_dispatch_queued_plan(self):
        controller, old = self.make_controller()
        state = controller.stage(old, 'first', [sys.executable, '-c', 'pass'])
        with self.assertRaisesRegex(BoundaryError, 'snapshot changed'):
            controller.dispatch(old, state['plans'][0]['id'])
        self.assertEqual(self.controller._read()[0]['commands'], [])
        self.controller.revoke()
        with self.assertRaises(BoundaryError):
            controller.dispatch(state, state['plans'][0]['id'])
        self.assertEqual(self.controller._read()[0]['commands'], [])

    def test_controller_late_native_completion_cannot_publish_stale_snapshot(self):
        controller, state = self.make_controller()
        state = controller.stage(state, 'first', [sys.executable, '-c', 'pass'])
        plan_id = state['plans'][0]['id']
        child, state = controller.dispatch(state, plan_id)
        self.children.append(child)
        original = self.controller.complete
        def stale_completion(*args, **kwargs):
            proof = original(*args, **kwargs)
            controller.stage(state, 'first', [sys.executable, '-c', 'pass'])
            return proof
        with patch.object(self.controller, 'complete', side_effect=stale_completion):
            with self.assertRaisesRegex(BoundaryError, 'snapshot changed'):
                controller.complete(state, plan_id)
        self.assertEqual(controller.snapshot()['plans'][0]['stage'], 'running')
        with self.assertRaises(BoundaryError):
            controller.commit(controller.snapshot())

    def test_controller_failed_running_acknowledgement_ceases_entire_boundary(self):
        controller, state = self.make_controller()
        state = controller.stage(state, 'first', [sys.executable, '-c', 'import time; time.sleep(60)'])
        original = controller._save
        def lost_ack(candidate):
            result = original(candidate)
            if candidate['plans'][0]['stage'] == 'running':
                raise OSError('lost running acknowledgement')
            return result
        with patch.object(controller, '_save', side_effect=lost_ack):
            with self.assertRaises(OSError):
                controller.dispatch(state, state['plans'][0]['id'])
        for child, _ in self.controller._children.values():
            self.children.append(child)
        self.assertEqual(self.controller.verify_cessation()['populated'], 0)
        restarted = FixtureController(FixtureSupervisor(self.directory, self.root))
        recovered = restarted.recover()
        self.assertEqual(recovered['phase'], 'ceased')
        with self.assertRaises(BoundaryError):
            restarted.commit(recovered)

    def test_controller_independent_recovery_of_stopped_native_plan(self):
        controller, state = self.make_controller()
        state = controller.stage(state, 'first', [sys.executable, '-c', 'import time; time.sleep(60)'])
        child, state = controller.dispatch(state, state['plans'][0]['id'])
        self.children.append(child)
        os.kill(child.pid, signal.SIGSTOP)
        code = ('from kopiur_fixture_supervisor import FixtureSupervisor; '
                'from kopiur_fixture_controller import FixtureController; '
                f'c=FixtureController(FixtureSupervisor({str(self.directory)!r}, {str(self.root)!r})); '
                'assert c.recover()["phase"] == "ceased"')
        subprocess.run([sys.executable, '-c', code], check=True, timeout=10,
                       env={'PYTHONPATH': str(Path(supervisor.__file__).parent)})
        self.assertEqual(child.wait(timeout=5), -signal.SIGKILL)
        with self.assertRaises(BoundaryError):
            controller.complete(state, state['plans'][0]['id'])

    def test_controller_rejects_unplanned_native_registration_at_commit(self):
        controller, state = self.make_controller()
        state = self.controller_plan(controller, state, 'first')
        child, extra = self.command('pass')
        self.controller.complete(extra['operation'])
        self.assertEqual(child.returncode, 0)
        with self.assertRaisesRegex(BoundaryError, 'entire authoritative'):
            controller.prepare(state)

    def test_controller_expired_snapshot_never_dispatches_native_plan(self):
        controller = FixtureController(self.controller)
        state = controller.begin({'first': 'native-first'}, 0.05)
        state = controller.stage(state, 'first', [sys.executable, '-c', 'pass'])
        time.sleep(0.06)
        with self.assertRaisesRegex(BoundaryError, 'expired'):
            controller.dispatch(state, state['plans'][0]['id'])
        self.assertEqual(self.controller._read()[0]['commands'], [])
        self.assertEqual(controller.recover()['phase'], 'ceased')

    def test_controller_terminal_persistence_failure_cannot_reopen_dispatch(self):
        controller, state = self.make_controller()
        state = self.controller_plan(controller, state, 'first')
        state = controller.prepare(state)
        original = controller._save
        def fail_terminal(candidate):
            if candidate['phase'] == 'committed':
                raise OSError('terminal persistence unavailable')
            return original(candidate)
        with patch.object(controller, '_save', side_effect=fail_terminal):
            with self.assertRaises(OSError):
                controller.commit(state)
        with self.assertRaisesRegex(BoundaryError, 'queued start denied'):
            self.command('pass')
        terminal = controller.commit(state)
        self.assertFalse(controller.verify_terminal(terminal)['production_recovery_accepted'])

    def test_controller_prepared_generation_aborts_without_admission_claim(self):
        controller, state = self.make_controller({'first': 'native-first', 'second': 'native-second'})
        state = self.controller_plan(controller, state, 'first')
        state = self.controller_plan(controller, state, 'second')
        state = controller.prepare(state)
        restarted = FixtureController(FixtureSupervisor(self.directory, self.root))
        aborted = restarted.recover()
        self.assertEqual(aborted['abort']['prior_phase'], 'prepared')
        self.assertEqual(aborted['abort']['consumers'], state['consumers'])
        self.assertFalse(aborted['abort']['consumer_admission_restored'])
        self.assertFalse(aborted['abort']['production_recovery_accepted'])
        self.assertEqual(restarted.recover()['abort'], aborted['abort'])
        with self.assertRaises(BoundaryError):
            controller.commit(state)

    def test_controller_lost_preparation_ack_requires_durability_barrier(self):
        controller, state = self.make_controller()
        state = self.controller_plan(controller, state, 'first')
        original = controller._save
        def lost_ack(candidate):
            result = original(candidate)
            if candidate['phase'] == 'prepared':
                raise OSError('lost preparation acknowledgement')
            return result
        with patch.object(controller, '_save', side_effect=lost_ack):
            with self.assertRaises(OSError):
                controller.prepare(state)
        visible = controller.snapshot()
        assert visible is not None
        self.assertEqual(visible['phase'], 'prepared')
        with self.assertRaisesRegex(BoundaryError, 'queued start denied'):
            self.command('pass')
        with patch.object(supervisor.os, 'fsync', side_effect=OSError('unavailable')):
            with self.assertRaises(OSError):
                controller.verify_prepared(visible)
        self.assertEqual(controller.verify_prepared(visible), visible['prepared'])
        self.assertFalse(controller.commit(visible)['terminal']['production_recovery_accepted'])

    def test_controller_process_loss_after_dispatch_is_independently_recoverable(self):
        controller, state = self.make_controller()
        state = controller.stage(state, 'first', [sys.executable, '-c', 'import time; time.sleep(60)'])
        code = ('import os; from kopiur_fixture_supervisor import FixtureSupervisor; '
                'from kopiur_fixture_controller import FixtureController; '
                f'c=FixtureController(FixtureSupervisor({str(self.directory)!r}, {str(self.root)!r})); '
                f'c.dispatch(c.snapshot(), {state["plans"][0]["id"]!r}); os._exit(0)')
        subprocess.run([sys.executable, '-c', code], check=True, timeout=10,
                       env={'PYTHONPATH': str(Path(supervisor.__file__).parent)})
        restarted = FixtureController(FixtureSupervisor(self.directory, self.root))
        running = restarted.snapshot()
        assert running is not None
        self.assertEqual(running['plans'][0]['stage'], 'running')
        with self.assertRaisesRegex(BoundaryError, 'unknown'):
            restarted.complete(running, running['plans'][0]['id'])
        recovered = restarted.recover()
        self.assertEqual(recovered['phase'], 'ceased')
        self.assertEqual(recovered['abort']['plans'], [state['plans'][0]['id']])
        self.assertEqual(recovered['abort']['populated'], 0)

    def test_controller_queued_cohort_abort_has_no_native_side_effect(self):
        controller, state = self.make_controller({'first': 'native-first', 'second': 'native-second'})
        state = controller.stage(state, 'first', [sys.executable, '-c', 'raise RuntimeError("queued")'])
        state = controller.stage(state, 'second', [sys.executable, '-c', 'raise RuntimeError("queued")'])
        recovered = controller.recover()
        self.assertEqual(recovered['abort']['plans'], [p['id'] for p in state['plans']])
        self.assertEqual(recovered['abort']['operations'], [])
        for plan in state['plans']:
            with self.assertRaises(BoundaryError):
                controller.dispatch(state, plan['id'])
        self.assertEqual(self.controller._read()[0]['commands'], [])

    def test_controller_preparation_rejects_changed_durable_native_arguments(self):
        controller, state = self.make_controller()
        state = self.controller_plan(controller, state, 'first')
        native, _ = self.controller._read()
        original = json.loads(json.dumps(native))
        native['commands'][0]['argv'][-1] = 'raise RuntimeError("changed native argv")'
        supervisor._persist(self.controller.path, native)
        try:
            with self.assertRaisesRegex(BoundaryError, 'native registration'):
                controller.prepare(state)
        finally:
            supervisor._persist(self.controller.path, original)

    def test_old_generation_is_not_overwritten(self):
        original = self.controller.path.read_bytes()
        with self.assertRaises(BoundaryError):
            self.controller.begin()
        self.assertEqual(original, self.controller.path.read_bytes())

    def independent_watchdog(self, monitor=False):
        code = ('import json; from kopiur_fixture_supervisor import FixtureSupervisor; '
                f's=FixtureSupervisor({str(self.directory)!r}, {str(self.root)!r}); '
                f'p=s.{"monitor_watchdog" if monitor else "watchdog_cease"}(); '
                'assert s.verify_watchdog_cessation()==p; '
                'print(json.dumps(p))')
        result = subprocess.run([sys.executable, '-c', code], check=True, timeout=10,
                                capture_output=True, text=True,
                                env={'PYTHONPATH': str(Path(supervisor.__file__).parent)})
        proof = json.loads(result.stdout)
        self.assertTrue(proof['frozen'])
        self.assertFalse(proof['consumer_admission_restored'])
        return proof

    def test_watchdog_ceases_descendants_while_capture_owner_holds_lock(self):
        with tempfile.TemporaryDirectory() as public:
            os.chmod(public, 0o777)
            marker = Path(public) / 'descendant'
            self.command('import os,time; from pathlib import Path; '
                         'pid=os.fork(); os._exit(0) if pid else None; os.setsid(); '
                         f'Path({str(marker)!r}).touch(); time.sleep(60)')
            self.wait_file(marker)
            locked = Path(public) / 'locked'
            owner = os.fork()
            if owner == 0:
                with self.controller._locked():
                    locked.touch()
                    os.kill(os.getpid(), signal.SIGSTOP)
                    os._exit(0)
            try:
                self.wait_file(locked)
                _, status = os.waitpid(owner, os.WUNTRACED)
                self.assertTrue(os.WIFSTOPPED(status))
                proof = self.independent_watchdog()
                self.assertEqual(proof['populated'], 0)
                # Ordinary cessation really is excluded by this live owner.
                with self.assertRaisesRegex(BoundaryError, 'lock timeout'):
                    self.controller.revoke()
                self.assertEqual(self.controller.verify_watchdog_cessation(), proof)
            finally:
                os.kill(owner, signal.SIGKILL)
                os.waitpid(owner, 0)
            restarted = FixtureSupervisor(self.directory, self.root)
            with self.assertRaisesRegex(BoundaryError, 'queued start denied'):
                restarted.dispatch([sys.executable, '-c', 'pass'])

    def paused_watchdog_dispatch(self, point):
        with tempfile.TemporaryDirectory() as public:
            os.chmod(public, 0o777)
            registered = Path(public) / 'registered'
            mutation = Path(public) / 'must-not-exist'
            owner = os.fork()
            if owner == 0:
                def pause():
                    registered.touch()
                    os.kill(os.getpid(), signal.SIGSTOP)
                if point == 'registered':
                    original = supervisor._persist
                    def persist(path, state):
                        original(path, state)
                        pause()
                    supervisor._persist = persist
                elif point == 'membership':
                    original = Path.write_text
                    def write_text(path, text, *args, **kwargs):
                        if path.name == 'cgroup.procs':
                            pause()
                        return original(path, text, *args, **kwargs)
                    patch.object(Path, 'write_text', write_text).start()
                elif point == 'gate':
                    original = os.write
                    def write(fd, value):
                        if value == b'G':
                            pause()
                        return original(fd, value)
                    os.write = write
                else:
                    os._exit(78)
                try:
                    self.controller.dispatch([sys.executable, '-c',
                        f'from pathlib import Path; Path({str(mutation)!r}).touch()'])
                except (BoundaryError, BrokenPipeError):
                    os._exit(0)
                os._exit(77)
            try:
                self.wait_file(registered)
                _, status = os.waitpid(owner, os.WUNTRACED)
                self.assertTrue(os.WIFSTOPPED(status))
                self.independent_watchdog()
                os.kill(owner, signal.SIGCONT)
                _, status = os.waitpid(owner, 0)
                owner = None
                self.assertEqual(os.waitstatus_to_exitcode(status), 0)
                self.assertFalse(mutation.exists())
                self.assertEqual(self.controller.verify_watchdog_cessation()['populated'], 0)
            finally:
                if owner is not None:
                    os.kill(owner, signal.SIGKILL)
                    os.waitpid(owner, 0)

    def test_watchdog_after_registration_before_gate_refuses_delayed_dispatch(self):
        self.paused_watchdog_dispatch('registered')

    def test_watchdog_before_membership_reaps_delayed_frozen_launcher(self):
        self.paused_watchdog_dispatch('membership')

    def test_watchdog_after_final_check_permanent_freeze_denies_gate_race(self):
        self.paused_watchdog_dispatch('gate')

    def test_watchdog_tombstone_survives_stale_owner_journal_replacement(self):
        state, _ = self.controller._read()
        self.independent_watchdog()
        supervisor._persist(self.controller.path, state)
        self.assertFalse(self.controller._read()[0]['revoked'])
        self.assertEqual(self.controller.verify_watchdog_cessation()['populated'], 0)
        with self.assertRaises(BoundaryError):
            self.command('pass')
        with self.assertRaises(BoundaryError):
            FixtureController(self.controller).begin({'first': 'native-first'}, 30)

    def test_watchdog_invalidates_completed_native_and_terminal_receipts(self):
        controller, state = self.make_controller()
        state = self.controller_plan(controller, state, 'first')
        state = controller.commit(controller.prepare(state))
        operation = state['terminal']['operations'][0]
        self.independent_watchdog()
        with self.assertRaises(BoundaryError):
            self.controller.verify_completion(operation)
        with self.assertRaises(BoundaryError):
            controller.verify_terminal(state)

    def test_watchdog_failed_revocation_durability_cannot_mutate_kernel(self):
        _, boundary = self.controller._read()
        original = os.fsync
        calls = []
        def fail_directory(fd):
            calls.append(fd)
            if len(calls) == 2:
                raise OSError('injected independent revocation directory fsync failure')
            original(fd)
        with patch.object(supervisor.os, 'fsync', side_effect=fail_directory):
            with self.assertRaises(OSError):
                self.controller.watchdog_cease()
        self.assertEqual((boundary / 'cgroup.freeze').read_text().strip(), '0')
        with patch.object(supervisor.os, 'fsync', side_effect=OSError('still unavailable')):
            with self.assertRaises(OSError):
                self.controller.watchdog_cease()
        self.assertEqual((boundary / 'cgroup.freeze').read_text().strip(), '0')
        self.independent_watchdog()

    def test_watchdog_changed_identity_never_freezes_another_boundary(self):
        state, boundary = self.controller._read()
        original = json.loads(json.dumps(state))
        state['boundary']['inode'] += 1
        supervisor._persist(self.controller.path, state)
        try:
            with self.assertRaisesRegex(BoundaryError, 'identity changed'):
                self.controller.watchdog_cease()
            self.assertEqual((boundary / 'cgroup.freeze').read_text().strip(), '0')
        finally:
            supervisor._persist(self.controller.path, original)


    def test_watchdog_deadline_monitor_does_not_acquire_live_owner_lock(self):
        self.controller.admit_watchdog(os.getpid(), 0.2)
        child, _ = self.command('import time; time.sleep(60)')
        with self.controller._locked():
            self.independent_watchdog(monitor=True)
        self.assertEqual(child.wait(timeout=5), -signal.SIGKILL)
        with self.assertRaises(BoundaryError):
            self.command('pass')

    def test_watchdog_monitor_restarts_after_admitted_owner_process_loss(self):
        read_fd, write_fd = os.pipe()
        owner = os.fork()
        if owner == 0:
            os.close(write_fd)
            os.read(read_fd, 1)
            os._exit(0)
        os.close(read_fd)
        try:
            self.controller.admit_watchdog(owner, 60)
            child, _ = self.command('import time; time.sleep(60)')
        finally:
            os.close(write_fd)
            os.waitpid(owner, 0)
        self.independent_watchdog(monitor=True)
        self.assertEqual(child.wait(timeout=5), -signal.SIGKILL)
        self.independent_watchdog(monitor=True)

    def test_watchdog_contract_refuses_rebinding_and_changed_boundary(self):
        self.controller.admit_watchdog(os.getpid(), 0.2)
        with self.assertRaisesRegex(BoundaryError, 'unused generation'):
            self.controller.admit_watchdog(os.getpid(), 60)
        path = self.directory / 'watchdog.json'
        contract = json.loads(path.read_text())
        contract['boundary']['inode'] += 1
        supervisor._persist(path, contract)
        _, boundary = self.controller._read()
        with self.assertRaisesRegex(BoundaryError, 'contract identity changed'):
            self.controller.monitor_watchdog()
        self.assertEqual((boundary / 'cgroup.freeze').read_text().strip(), '0')


    def test_watchdog_monitor_pidfd_above_select_descriptor_limit(self):
        self.controller.admit_watchdog(os.getpid(), 0.2)
        original = getattr(os, 'pidfd_open')
        def high_pidfd(pid):
            fd = original(pid)
            try:
                return supervisor.fcntl.fcntl(fd, supervisor.fcntl.F_DUPFD_CLOEXEC, 2048)
            finally:
                os.close(fd)
        with patch.object(supervisor.os, 'pidfd_open', side_effect=high_pidfd):
            self.controller.monitor_watchdog()
        self.controller.verify_watchdog_cessation()

    def test_watchdog_monitor_failure_does_not_abandon_native_workers(self):
        self.controller.admit_watchdog(os.getpid(), 60)
        child, _ = self.command('import time; time.sleep(60)')
        with patch.object(supervisor.select, 'poll', side_effect=OSError('injected poll failure')):
            with self.assertRaises(OSError):
                self.controller.monitor_watchdog()
        self.assertEqual(child.wait(timeout=5), -signal.SIGKILL)
        self.controller.verify_watchdog_cessation()

    def test_watchdog_during_native_completion_persistence_cannot_publish_success(self):
        child, registration = self.command('pass')
        child.wait(timeout=5)
        operation = registration['operation']
        original = supervisor._persist
        def revoke_after_persist(path, state):
            original(path, state)
            self.independent_watchdog()
        with patch.object(supervisor, '_persist', side_effect=revoke_after_persist):
            with self.assertRaisesRegex(BoundaryError, 'cannot complete'):
                self.controller.complete(operation)
        with self.assertRaises(BoundaryError):
            self.controller.verify_completion(operation)

    def test_watchdog_during_terminal_commit_cannot_publish_success(self):
        controller, state = self.make_controller()
        state = self.controller_plan(controller, state, 'first')
        state = controller.prepare(state)
        original = controller_module._persist
        def revoke_after_persist(path, state):
            original(path, state)
            self.independent_watchdog()
        with patch.object(controller_module, '_persist', side_effect=revoke_after_persist):
            with self.assertRaisesRegex(BoundaryError, 'revoked controller publication'):
                controller.commit(state)
        self.assertEqual(controller.recover()['phase'], 'ceased')


    def test_native_watchdog_denies_forward_coordinator_before_any_new_sql(self):
        with self.admission_case() as (admission, state):
            self.independent_watchdog()
            before = admission.database.read_bytes()
            with self.assertRaisesRegex(BoundaryError, 'revoked admission coordinator'):
                admission.apply(state, 'hold')
            self.assertEqual(admission.database.read_bytes(), before)
            self.assertTrue(admission.recover()['consumer_admission_restored'])

    def test_native_watchdog_during_ack_persistence_cannot_publish_success(self):
        with self.admission_case() as (admission, state):
            original = admission_module._persist
            def revoke_after_persist(path, candidate):
                original(path, candidate)
                if candidate.get('observed') and candidate['intent'] is None:
                    self.independent_watchdog()
            with patch.object(admission_module, '_persist', side_effect=revoke_after_persist):
                with self.assertRaisesRegex(BoundaryError, 'revoked native admission publication'):
                    admission.apply(state, 'hold')
            self.assertFalse(self.native_state(admission)['consumers']['first']['admission'])
            self.assertTrue(admission.recover()['consumer_admission_restored'])


    def test_native_watchdog_paused_capture_preserves_prior_until_owned_recovery(self):
        with self.admission_case(linked=True) as (admission, state):
            state = self.advance_admission(admission, state)
            self.publish_native_capture(admission)
            state = admission.apply(state, 'release', 'first')
            reference = state['release_boundary']
            nested = FixtureSupervisor(reference['directory'], reference['root'])
            descendant = admission.results / 'watchdog-nested-descendant'
            child, _ = nested.dispatch([sys.executable, '-c',
                'import os,time; from pathlib import Path; '
                'pid=os.fork(); os._exit(0) if pid else None; os.setsid(); '
                f'Path({str(descendant)!r}).touch(); time.sleep(60)'])
            self.wait_file(descendant)
            self.assertEqual(child.wait(timeout=5), 0)
            locked = admission.results / 'capture-owner-locked'
            owner = os.fork()
            if owner == 0:
                with self.controller._locked():
                    locked.touch()
                    os.kill(os.getpid(), signal.SIGSTOP)
                    os._exit(0)
            try:
                self.wait_file(locked)
                _, status = os.waitpid(owner, os.WUNTRACED)
                self.assertTrue(os.WIFSTOPPED(status))
                self.independent_watchdog()
                self.assertIn('populated 0', (nested._read()[1] / 'cgroup.events').read_text())
                before = self.native_state(admission)
                self.assertTrue(before['consumers']['first']['admission'])
                self.assertFalse(before['consumers']['second']['admission'])
                self.assertFalse(before['consumers']['third']['admission'])
                with self.assertRaisesRegex(BoundaryError, 'lock timeout'):
                    admission.recover()
                self.assertEqual(self.native_state(admission), before)
            finally:
                os.kill(owner, signal.SIGKILL)
                os.waitpid(owner, 0)
            restarted = admission_module.NativeAdmission(
                FixtureSupervisor(self.directory, self.root), admission.database,
                admission.identity, admission.results, capture_consumer='admission')
            recovered = restarted.recover()
            self.assertTrue(recovered['consumer_admission_restored'])
            self.assertFalse(recovered['production_recovery_accepted'])
            for name, prior in recovered['plan']['consumers'].items():
                self.assertEqual(recovered['observed']['consumers'][name]['admission'],
                                 prior['prior_admission'])
            with self.assertRaises(BoundaryError):
                restarted.apply(state, 'release', 'third')
            self.assertEqual(restarted.recover()['observed']['consumers'],
                             recovered['observed']['consumers'])


if __name__ == '__main__':
    unittest.main()
