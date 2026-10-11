"""Remote Linux kernel/process regressions, not simulated watchdog responses.

The tiny collector/supervisor below are explicit unit fixtures. Pinned native
SQLite/ES/Dragonfly/media qualification runs separately in the same ARC suite.
"""
import asyncio
import importlib.util
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest

APP = Path(__file__).resolve().parents[2] / 'kubernetes/apps/media/tubearchivist/app'
SPEC = importlib.util.spec_from_file_location('coordination', APP / 'coordination.py')
GATE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(GATE)
COLLECTOR = """import json,sys,time
from pathlib import Path
p=Path(sys.argv[-1]); p.mkdir(parents=True)
(p/'partial').write_bytes(b'fixture-content')
time.sleep(float(sys.argv[-2]))
(p/'manifest.json').write_text(json.dumps({'fixture':True,'complete':True}))
"""
SUPERVISOR = """import time,coordination as g
from pathlib import Path
g.atomic(g.ROOT/'unit-supervisor.json', g.identity())
while True:
    s=g.current()
    if s and s.get('admission')=='CLOSED':
        g.atomic(g.ROOT/'drained.json', {'generation':s['generation'],'beat_clean':True,'supervisor':g.identity()})
    time.sleep(.01)
"""


class KernelCoordinationTests(unittest.TestCase):
    def setUp(self):
        if sys.platform != 'linux':
            self.skipTest('run on existing Linux ARC only')
        self.tmp = tempfile.TemporaryDirectory(prefix='coordination-', dir=os.environ.get('TMPDIR'))
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.env = dict(os.environ, TA_COORDINATION_DIR=str(self.root / 'journal'), PYTHONPATH=str(APP))
        self.supervisor = self.process(SUPERVISOR)
        self.wait(lambda: (self.root / 'journal/unit-supervisor.json').exists())
        self.original_root = GATE.ROOT
        GATE.ROOT = self.root / 'journal'
        self.addCleanup(setattr, GATE, 'ROOT', self.original_root)

    def process(self, code, *args):
        process = subprocess.Popen([sys.executable, '-c', code, *args], env=self.env,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        def cleanup():
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=5)
        self.addCleanup(cleanup)
        return process

    def wait(self, condition, seconds=5):
        until = time.monotonic() + seconds
        while time.monotonic() < until:
            if condition():
                return
            time.sleep(.01)
        self.fail('unit fixture readiness deadline')

    def tearDown(self):
        timings = []
        for path in sorted((self.root / 'journal/history').glob('*.json')):
            state = json.loads(path.read_text())
            if 'resumed_at' in state:
                timings.append({'phase':state['phase'],
                                'closed_seconds':state['resumed_at']-state['started_at'],
                                'exclusive_seconds':state['resumed_at']-state.get('frozen_at',state['resumed_at'])})
        print(json.dumps({'kernel_fixture_timing':self._testMethodName,'attempts':timings}), flush=True)

    def state(self):
        path = self.root / 'journal/current.json'
        return json.loads(path.read_text()) if path.exists() else {}

    def attempt(self, name='bundle', delay=0, hold=1.5, drain=1, injection='', collector_code=COLLECTOR):
        command = [sys.executable, '-c', collector_code, str(delay)]
        code = ("from pathlib import Path\nimport coordination as g\n" + injection +
                f"g.coordinate(Path({str(self.root / name)!r}), {command!r}, {drain}, {hold})\n")
        return self.process(code)

    def finished(self, process, success=True):
        out, err = process.communicate(timeout=8)
        if success:
            self.assertEqual(process.returncode, 0, err)
        else:
            self.assertNotEqual(process.returncode, 0)
        self.wait(lambda: self.state().get('admission') == 'OPEN')
        return out, err

    def test_success_resumes_before_upload_failure_and_retry_preserves_generation(self):
        self.finished(self.attempt())
        commit = GATE.verify_generation(self.root / 'bundle')
        before = (self.root / 'bundle/generation.json').read_bytes()
        self.assertEqual(self.state()['phase'], 'RESUMED')
        failure = subprocess.run([sys.executable, '-c', 'raise SystemExit(7)'], env=self.env)
        self.assertEqual(failure.returncode, 7)
        with GATE.writer():
            self.assertEqual(self.state()['admission'], 'OPEN')
        self.finished(self.attempt('retry'))
        self.assertEqual((self.root / 'bundle/generation.json').read_bytes(), before)
        self.assertNotEqual(commit['generation'], GATE.verify_generation(self.root / 'retry')['generation'])

    def test_capture_timeout_revokes_partial_and_bounds_resume(self):
        started = time.monotonic()
        self.finished(self.attempt(delay=10, hold=.25), success=False)
        self.assertLess(time.monotonic() - started, 2)
        self.assertEqual(self.state()['phase'], 'REVOKED')
        self.assertFalse((self.root / 'bundle').exists())
        self.assertFalse(any(self.root.glob('.generation-*/generation.json')))
        self.finished(self.attempt('retry'))

    def test_controller_sigkill_independent_watchdog_resumes(self):
        process = self.attempt(delay=10)
        self.wait(lambda: self.state().get('phase') == 'CAPTURING')
        process.kill()
        process.communicate(timeout=5)
        self.wait(lambda: self.state().get('admission') == 'OPEN', 2)
        self.assertEqual(self.state()['phase'], 'REVOKED')
        self.assertFalse((self.root / 'bundle').exists())
        self.finished(self.attempt('retry'))

    def test_collector_crash_cannot_publish_and_retry_is_new_generation(self):
        process = self.attempt(delay=10)
        self.wait(lambda: self.state().get('collector'))
        owner = self.state()['collector']
        GATE.terminate(owner)
        self.finished(process, success=False)
        self.assertFalse((self.root / 'bundle').exists())
        self.finished(self.attempt('retry'))

    def test_sigkill_before_collector_registration_never_runs_untracked_code(self):
        injection = """original=g.subprocess.Popen
def pause_before_registration(*args,**kwargs):
    child=original(*args,**kwargs)
    if 'collector' in args[0]:
        g.atomic(g.ROOT/'before-registration.json',g.identity(child.pid))
        g.time.sleep(10)
    return child
g.subprocess.Popen=pause_before_registration
"""
        process = self.attempt(delay=10, injection=injection)
        marker = self.root / 'journal/before-registration.json'
        self.wait(marker.exists)
        collector = json.loads(marker.read_text())
        self.assertEqual(self.state()['phase'], 'DRAINING')
        self.assertNotIn('collector', self.state())
        process.kill()
        process.communicate(timeout=5)
        self.wait(lambda: not GATE.alive(collector), 2)
        self.wait(lambda: self.state().get('admission') == 'OPEN', 2)
        self.assertFalse(any(self.root.glob('.generation-*')))
        self.finished(self.attempt('retry'))

    def test_sigkill_kills_collector_descendants_and_closes_inherited_pipes(self):
        code = """import subprocess,sys,time,coordination as g
from pathlib import Path
p=Path(sys.argv[-1]);p.mkdir(parents=True)
child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(10)'])
g.atomic(g.ROOT/'descendant.json',g.identity(child.pid))
time.sleep(10)
"""
        process = self.attempt(collector_code=code)
        marker = self.root / 'journal/descendant.json'
        self.wait(marker.exists)
        child = json.loads(marker.read_text())
        collector = self.state()['collector']
        process.kill()
        process.communicate(timeout=5)
        self.wait(lambda: self.state().get('admission') == 'OPEN', 2)
        self.assertFalse(GATE.alive(child))
        self.assertFalse(GATE.alive(collector))
        self.assertFalse((self.root / 'bundle').exists())
        self.finished(self.attempt('retry'))

    def test_collector_leader_crash_cleans_descendants_before_reaping(self):
        code = """import subprocess,sys,time,coordination as g
from pathlib import Path
p=Path(sys.argv[-1]);p.mkdir(parents=True)
child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(10)'])
g.atomic(g.ROOT/'descendant.json',g.identity(child.pid))
time.sleep(10)
"""
        process = self.attempt(collector_code=code)
        marker = self.root / 'journal/descendant.json'
        self.wait(marker.exists)
        child = json.loads(marker.read_text())
        GATE.terminate(self.state()['collector'])
        self.finished(process, success=False)
        self.assertFalse(GATE.alive(child))
        self.finished(self.attempt('retry'))

    def test_registered_collector_identity_is_preserved_through_exec(self):
        code = """import sys,json,coordination as g
from pathlib import Path
assert g.current()['collector']==g.identity()
p=Path(sys.argv[-1]);p.mkdir(parents=True)
(p/'manifest.json').write_text(json.dumps({'fixture':True,'complete':True}))
"""
        self.finished(self.attempt(collector_code=code))
        GATE.verify_generation(self.root / 'bundle')

    def test_returned_collector_cannot_leave_descendants_holding_output(self):
        code = """import subprocess,sys,json,coordination as g
from pathlib import Path
p=Path(sys.argv[-1]);p.mkdir(parents=True)
child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(10)'])
g.atomic(g.ROOT/'descendant.json',g.identity(child.pid))
(p/'manifest.json').write_text(json.dumps({'fixture':True,'complete':True}))
"""
        self.finished(self.attempt(collector_code=code))
        child = json.loads((self.root / 'journal/descendant.json').read_text())
        self.assertFalse(GATE.alive(child))
        GATE.verify_generation(self.root / 'bundle')

    def test_terminal_journal_waits_for_actual_attempt_lock_cleanup(self):
        holder = self.process("""import time,coordination as g
with g.lock('attempt.lock'):
    g.atomic(g.ROOT/'current.json',{'generation':'terminal-unit','phase':'REVOKED','admission':'OPEN'})
    (g.ROOT/'holder-ready').touch()
    time.sleep(.25)
""")
        self.wait(lambda: (self.root / 'journal/holder-ready').exists())
        self.finished(self.attempt())
        holder.communicate(timeout=5)
        self.assertEqual(holder.returncode, 0)

    def test_failed_resume_publication_recovered_without_invalidating_closed_bytes(self):
        injection = """original=g.atomic
def fail_resume(path,value):
    if value.get('phase')=='RESUMED': raise OSError('unit resume write failure')
    return original(path,value)
g.atomic=fail_resume
"""
        self.finished(self.attempt(injection=injection), success=False)
        # Its generation was committed while all writers were excluded. It is a
        # valid point even though the hook failed. The watchdog reopened admission.
        GATE.verify_generation(self.root / 'bundle')
        self.assertEqual(self.state()['phase'], 'REVOKED')
        self.finished(self.attempt('retry'))

    def test_writer_lease_drain_timeout_does_not_kill_admitted_writer(self):
        writer = self.process("import coordination as g,time; from pathlib import Path\n"
                              "with g.writer():\n    (g.ROOT/'writer-active').touch(); time.sleep(2)\n")
        self.wait(lambda: (self.root / 'journal/writer-active').exists())
        started = time.monotonic()
        self.finished(self.attempt(drain=.2), success=False)
        self.assertLess(time.monotonic() - started, 1.5)
        self.assertIsNone(writer.poll())
        writer.communicate(timeout=5)
        self.finished(self.attempt('retry'))

    def test_killed_watchdog_expiry_recovery_by_independent_writer(self):
        process = self.attempt(delay=10, hold=.3)
        self.wait(lambda: self.state().get('collector'))
        # Find only this controller's actual detached watchdog, by parent and
        # reviewed script argv. No arbitrary process or worker is signalled.
        watcher = None
        for path in Path('/proc').iterdir():
            if not path.name.isdigit():
                continue
            try:
                status = (path / 'stat').read_text().rsplit(')', 1)[1].split()
                argv = (path / 'cmdline').read_bytes().split(b'\0')
                if int(status[1]) == process.pid and b'watchdog' in argv:
                    watcher = int(path.name)
            except FileNotFoundError:
                continue
        self.assertIsNotNone(watcher)
        os.kill(watcher, signal.SIGKILL)
        # Wait for actual monotonic expiry, then exercise the writer-side fallback.
        self.wait(lambda: time.monotonic() > self.state()['deadline'])
        with GATE.writer():
            self.assertEqual(self.state()['admission'], 'OPEN')
        process.communicate(timeout=5)
        self.assertNotEqual(process.returncode, 0)
        self.assertFalse((self.root / 'bundle').exists())
        self.finished(self.attempt('retry'))

    def test_stale_boot_or_reused_collector_pid_recovers_without_signalling_heir(self):
        for stale in (dict(GATE.identity(), boot='previous-boot'),
                      dict(GATE.identity(), start='0')):
            state = {'format':'writer-generation-v1', 'generation':'unit-stale',
                     'owner':GATE.identity(), 'collector':stale,
                     'phase':'REVOKED', 'admission':'CLOSED',
                     'started_at':time.monotonic(), 'deadline':time.monotonic()-1}
            GATE.atomic(GATE.ROOT / 'current.json', state)
            GATE.recover()
            self.assertEqual(self.state()['admission'], 'OPEN')
            self.assertTrue(GATE.alive(GATE.identity()))

    def test_watchdog_cleanup_waits_until_controller_has_reaped_owned_leader(self):
        injection = """original=g._terminate_collector
def paused_cleanup(owner):
    original(owner)
    (g.ROOT/'cleanup-before-reap').touch()
    g.time.sleep(.25)
g._terminate_collector=paused_cleanup
"""
        process = self.attempt(injection=injection)
        self.wait(lambda: (self.root / 'journal/cleanup-before-reap').exists())
        owner = self.state()['collector']
        observer = self.process("import coordination as g\n"
                                f"with g.lock('collector-cleanup.lock'):\n"
                                f"    assert g.identity({owner['pid']},include_zombie=True) is None\n")
        self.finished(process)
        _, error = observer.communicate(timeout=5)
        self.assertEqual(observer.returncode, 0, error)

    def test_restart_recovers_revoked_journal_left_closed_mid_resume(self):
        state = {'format':'writer-generation-v1', 'generation':'unit-revoked',
                 'owner': GATE.identity(), 'admission':'CLOSED', 'phase':'REVOKED',
                 'started_at':time.monotonic(), 'deadline':time.monotonic()-1}
        GATE.atomic(GATE.ROOT / 'current.json', state)
        GATE.recover()
        self.assertEqual(self.state()['admission'], 'OPEN')
        with GATE.writer():
            pass

    def test_overlapping_attempt_fails_without_replacing_active_owner(self):
        first = self.attempt(delay=.25)
        self.wait(lambda: self.state().get('collector'))
        generation = self.state()['generation']
        second = self.attempt('other')
        _, error = second.communicate(timeout=5)
        self.assertNotEqual(second.returncode, 0, error)
        self.assertEqual(self.state()['generation'], generation)
        self.finished(first)

    def test_expired_or_missing_generation_and_tampered_manifest_refused(self):
        self.finished(self.attempt())
        (self.root / 'bundle/manifest.json').write_text('{}')
        with self.assertRaisesRegex(RuntimeError, 'manifest differs'):
            GATE.verify_generation(self.root / 'bundle')
        with self.assertRaisesRegex(RuntimeError, 'missing'):
            GATE.verify_generation(self.root / 'missing')

    def test_cancelled_asgi_admission_does_not_leak_lease(self):
        async def exercise():
            async def native(scope, receive, send):
                await asyncio.sleep(10)
            task = asyncio.create_task(GATE.admitted(native, {}, None, None))
            await asyncio.sleep(.05)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            with GATE.lock('writers.lock', blocking=False):
                pass
        asyncio.run(exercise())


if __name__ == '__main__':
    unittest.main()
