"""Mixin executed only by the admitted ARC kernel-boundary test harness."""
import contextlib
import copy
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid
from typing import TYPE_CHECKING
from unittest.mock import patch

import kopiur_fixture_supervisor as supervisor
from kopiur_fixture_supervisor import BoundaryError, FixtureSupervisor
from kopiur_fixture_admission import NativeAdmission, create_database, operation
from kopiur_fixture_controller import FixtureController


if TYPE_CHECKING:
    from unittest import TestCase as _Case
else:
    # Runtime discovery must not collect the mixin as a second test class.
    _Case = object


class NativeAdmissionCases(_Case):
    controller: FixtureSupervisor
    root: Path
    directory: Path

    if TYPE_CHECKING:
        def command(self, code: str) -> tuple[subprocess.Popen, dict]: ...
        def wait_file(self, path: Path) -> None: ...

    @contextlib.contextmanager
    def admission_case(self, linked=False):
        with tempfile.TemporaryDirectory() as public:
            os.chmod(public, 0o777)
            database = Path(public) / (str(uuid.uuid4()) + '.sqlite')
            identity = create_database(database, {
                'first': {'identity': 'native-first', 'admission': True},
                'second': {'identity': 'native-second', 'admission': False},
                'third': {'identity': 'native-third', 'admission': True}})
            if linked:
                FixtureController(self.controller).begin({'admission': database.stem, 'capture': 'native-records'}, 30)
            admission = NativeAdmission(self.controller, database, identity, public,
                                        capture_consumer='admission' if linked else None)
            try:
                yield admission, admission.begin()
            finally:
                if admission.path.exists():
                    reference = admission.snapshot()['release_boundary']
                    if reference:
                        owned = FixtureSupervisor(reference['directory'], reference['root'])
                        proof = owned.cease()
                        _, boundary = owned._read()
                        boundary.rmdir()
                        print(json.dumps({'native_release_boundary': proof,
                                          'owned_boundary_removed': True}), flush=True)
                    for directory in admission.snapshot()['boundaries']:
                        owned = FixtureSupervisor(directory, self.root)
                        proof = owned.cease()
                        _, boundary = owned._read()
                        boundary.rmdir()
                        print(json.dumps({'native_recovery_boundary': proof,
                                          'owned_boundary_removed': True}), flush=True)

    def native_state(self, admission):
        # Trusted test-only readback/fault injection, not controller runtime I/O.
        return operation(admission.database, admission.identity, None, 'observe')

    def advance_admission(self, admission, state):
        for action in ('hold', 'prepare', 'commit'):
            state = admission.apply(state, action)
        return state

    def test_native_commit_stays_held_and_database_writer_is_denied(self):
        with self.admission_case() as (admission, state):
            state = self.advance_admission(admission, state)
            actual = self.native_state(admission)
            self.assertEqual(actual['phase'], 'committed')
            self.assertTrue(all(v['admission'] is False for v in actual['consumers'].values()))
            with self.assertRaisesRegex(BoundaryError, 'writer admission closed'):
                operation(admission.database, admission.identity, None, 'write', 'first', 'blocked')
            for name in state['plan']['consumers']:
                state = admission.apply(state, 'release', name)
            self.assertEqual(state['phase'], 'released')
            actual = self.native_state(admission)
            self.assertTrue(actual['consumers']['first']['admission'])
            self.assertFalse(actual['consumers']['second']['admission'])
            operation(admission.database, admission.identity, None, 'write', 'first', 'admitted')
            connection = sqlite3.connect(admission.database)
            try:
                self.assertEqual(connection.execute('SELECT * FROM records').fetchall(), [('first', 'admitted')])
            finally:
                connection.close()

    def test_native_partial_release_recovery_restores_entire_prior_cohort(self):
        with self.admission_case() as (admission, state):
            state = self.advance_admission(admission, state)
            state = admission.apply(state, 'release', 'first')
            actual = self.native_state(admission)
            self.assertTrue(actual['consumers']['first']['admission'])
            self.assertFalse(actual['consumers']['third']['admission'])
            restarted = NativeAdmission(FixtureSupervisor(self.directory, self.root),
                                        admission.database, admission.identity, admission.results)
            recovered = restarted.recover()
            self.assertTrue(recovered['consumer_admission_restored'])
            self.assertFalse(recovered['production_recovery_accepted'])
            self.assertEqual(recovered['observed']['phase'], 'rolled_back')
            self.assertEqual([v['admission'] for v in recovered['observed']['consumers'].values()],
                             [v['prior_admission'] for v in recovered['plan']['consumers'].values()])
            with self.assertRaisesRegex(BoundaryError, 'queued start denied'):
                self.command('raise RuntimeError("late release")')
            again = restarted.recover()
            self.assertEqual(again['observed']['consumers'], recovered['observed']['consumers'])

    def test_native_never_held_intent_recovers_without_reopening_closed_member(self):
        with self.admission_case() as (admission, state):
            result = admission.recover()
            self.assertEqual(result['observed']['phase'], 'rolled_back')
            self.assertFalse(result['observed']['consumers']['second']['admission'])

    def test_native_lost_release_acknowledgement_is_recoverable_after_restart(self):
        with self.admission_case() as (admission, state):
            state = self.advance_admission(admission, state)
            original = admission._save
            def lost_ack(candidate):
                result = original(candidate)
                if candidate.get('observed', {}).get('consumers', {}).get('first', {}).get('released'):
                    raise OSError('lost native release acknowledgement')
                return result
            with patch.object(admission, '_save', side_effect=lost_ack):
                with self.assertRaises(OSError):
                    admission.apply(state, 'release', 'first')
            self.assertTrue(self.native_state(admission)['consumers']['first']['admission'])
            recovered = admission.recover()
            self.assertTrue(recovered['consumer_admission_restored'])
            self.assertTrue(recovered['observed']['consumers']['third']['admission'])

    def test_native_recovery_fence_denies_all_late_forward_mutations(self):
        with self.admission_case() as (admission, state):
            state = self.advance_admission(admission, state)
            admission.recover()
            original = admission.database.read_bytes()
            for action in ('hold', 'prepare', 'commit', 'release'):
                with self.assertRaises(BoundaryError):
                    operation(admission.database, admission.identity, state['plan'], action, 'first')
                self.assertEqual(admission.database.read_bytes(), original)

    def test_native_foreign_member_blocks_whole_cohort_before_first_restore(self):
        with self.admission_case() as (admission, state):
            state = admission.apply(state, 'hold')
            changed = self.native_state(admission)
            changed['consumers']['third']['identity'] = 'foreign-resource'
            connection = sqlite3.connect(admission.database)
            try:
                connection.execute('UPDATE state SET body=? WHERE id=1', (json.dumps(changed),))
                connection.commit()
            finally:
                connection.close()
            with self.assertRaises(BoundaryError):
                admission.recover()
            actual = self.native_state(admission)
            self.assertFalse(actual['consumers']['first']['admission'])
            self.assertFalse(actual['consumers']['second']['admission'])

    def test_native_wrong_prior_plan_cannot_reopen_originally_closed_member(self):
        with self.admission_case() as (admission, state):
            state = admission.apply(state, 'hold')
            changed = copy.deepcopy(state['plan'])
            changed['consumers']['second']['prior_admission'] = True
            original = admission.database.read_bytes()
            with self.assertRaisesRegex(BoundaryError, 'immutable prior'):
                operation(admission.database, admission.identity, changed, 'fence')
            self.assertEqual(admission.database.read_bytes(), original)
            recovered = admission.recover()
            self.assertFalse(recovered['observed']['consumers']['second']['admission'])

    def test_native_partial_rollback_lost_ack_retries_entire_cohort(self):
        with self.admission_case() as (admission, state):
            admission.apply(state, 'hold')
            original = admission._worker
            def lost_ack(command_supervisor, plan, action, consumer=None, value=None):
                observed = original(command_supervisor, plan, action, consumer, value)
                if action == 'rollback' and consumer == 'first':
                    raise OSError('lost first rollback acknowledgement')
                return observed
            with patch.object(admission, '_worker', side_effect=lost_ack):
                with self.assertRaises(OSError):
                    admission.recover()
            actual = self.native_state(admission)
            self.assertTrue(actual['consumers']['first']['restored'])
            self.assertFalse(actual['consumers']['third']['restored'])
            recovered = admission.recover()
            self.assertTrue(all(v['restored'] for v in recovered['observed']['consumers'].values()))

    def test_native_sql_busy_is_bounded_and_does_not_hold_controller_lock(self):
        with self.admission_case() as (admission, state):
            connection = sqlite3.connect(admission.database, timeout=0)
            connection.execute('BEGIN IMMEDIATE')
            try:
                start = time.monotonic()
                with self.assertRaises(BoundaryError):
                    admission.apply(state, 'hold')
                self.assertLess(time.monotonic() - start, 5)
                self.assertEqual(self.controller.verify_cessation()['populated'], 0)
            finally:
                connection.rollback()
                connection.close()
            self.assertTrue(admission.recover()['consumer_admission_restored'])

    def test_native_recovery_denies_restore_until_complete_descendant_cessation(self):
        with self.admission_case() as (admission, state):
            admission.apply(state, 'hold')
            marker = admission.results / 'descendant'
            child, _ = self.command('import os,time; from pathlib import Path; '
                f'pid=os.fork(); os._exit(0) if pid else None; os.setsid(); '
                f'Path({str(marker)!r}).write_text(str(os.getpid())); time.sleep(60)')
            self.wait_file(marker)
            self.assertEqual(child.wait(timeout=5), 0)
            _, boundary = self.controller._read()
            self.assertIn('populated 1', (boundary / 'cgroup.events').read_text())
            original = admission._worker
            def assert_ceased(command_supervisor, plan, action, consumer=None, value=None):
                if action in ('fence', 'rollback'):
                    self.assertEqual(self.controller.verify_cessation()['populated'], 0)
                    self.assertIn('populated 0', (boundary / 'cgroup.events').read_text())
                return original(command_supervisor, plan, action, consumer, value)
            with patch.object(admission, '_worker', side_effect=assert_ceased):
                self.assertTrue(admission.recover()['consumer_admission_restored'])

    def test_native_queued_forward_snapshot_cannot_survive_recovery(self):
        with self.admission_case() as (admission, state):
            admission.recover()
            before = self.native_state(admission)
            with self.assertRaises(BoundaryError):
                admission.apply(state, 'hold')
            self.assertEqual(self.native_state(admission), before)

    def publish_native_capture(self, admission):
        publisher = FixtureController(self.controller)
        output = admission.results / 'capture-records'
        code = ('import json,os,sqlite3; from pathlib import Path; '
                f'connection=sqlite3.connect({str(admission.database)!r}); '
                'rows=connection.execute("SELECT * FROM records ORDER BY consumer,value").fetchall(); '
                f'fd=os.open({str(output)!r},os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600); '
                'stream=os.fdopen(fd,"w"); json.dump(rows,stream); stream.flush(); '
                'os.fsync(stream.fileno()); stream.close(); '
                f'fd=os.open({str(output.parent)!r},os.O_RDONLY|os.O_DIRECTORY); '
                'os.fsync(fd); os.close(fd); connection.close()')
        state = publisher.stage(publisher.snapshot(), 'capture', [sys.executable, '-c', code])
        plan_id = state['plans'][-1]['id']
        child, running = publisher.dispatch(state, plan_id)
        try:
            state = publisher.complete(running, plan_id)
        finally:
            child.wait(timeout=5)
        state = publisher.commit(publisher.prepare(state))
        publisher.verify_terminal(state)
        return state, json.loads(output.read_text())

    def test_native_capture_publication_precedes_guarded_nested_admission_release(self):
        with self.admission_case(linked=True) as (admission, state):
            operation(admission.database, admission.identity, None, 'write', 'first', 'record-1')
            state = self.advance_admission(admission, state)
            before = self.native_state(admission)
            with self.assertRaises(BoundaryError):
                admission.apply(state, 'release', 'first')
            self.assertEqual(self.native_state(admission), before)
            terminal, rows = self.publish_native_capture(admission)
            self.assertEqual(rows, [['first', 'record-1']])
            for name in state['plan']['consumers']:
                state = admission.apply(state, 'release', name)
            self.assertEqual(state['phase'], 'released')
            reference = state['release_boundary']
            self.assertEqual(reference['root'], str(self.controller._read()[1]))
            FixtureController(self.controller).verify_terminal(terminal)
            with self.assertRaises(BoundaryError):
                self.command('raise RuntimeError("late capture")')

    def test_native_committed_capture_partial_release_restart_restores_prior_admission(self):
        with self.admission_case(linked=True) as (admission, state):
            state = self.advance_admission(admission, state)
            self.publish_native_capture(admission)
            state = admission.apply(state, 'release', 'first')
            restarted = NativeAdmission(FixtureSupervisor(self.directory, self.root),
                admission.database, admission.identity, admission.results, capture_consumer='admission')
            recovered = restarted.recover()
            self.assertTrue(recovered['consumer_admission_restored'])
            self.assertEqual(FixtureController(self.controller).snapshot()['phase'], 'ceased')
            self.assertFalse(recovered['observed']['consumers']['second']['admission'])
            self.assertTrue(recovered['observed']['consumers']['third']['admission'])

    def test_native_parent_cessation_contains_live_nested_release_descendant(self):
        with self.admission_case(linked=True) as (admission, state):
            state = self.advance_admission(admission, state)
            self.publish_native_capture(admission)
            state = admission.apply(state, 'release', 'first')
            reference = state['release_boundary']
            nested = FixtureSupervisor(reference['directory'], reference['root'])
            marker = admission.results / 'nested-descendant'
            code = ('import os,time; from pathlib import Path; '
                    'pid=os.fork(); os._exit(0) if pid else None; os.setsid(); '
                    f'Path({str(marker)!r}).write_text(str(os.getpid())); time.sleep(60)')
            child, _ = nested.dispatch([sys.executable, '-c', code])
            try:
                self.wait_file(marker)
                self.assertEqual(child.wait(timeout=5), 0)
                with self.assertRaises(BoundaryError):
                    FixtureController(self.controller).verify_terminal(FixtureController(self.controller).snapshot())
                self.controller.cease()
                self.assertIn('populated 0', (nested._read()[1] / 'cgroup.events').read_text())
                self.assertFalse(self.native_state(admission)['consumers']['third']['admission'])
                self.assertTrue(admission.recover()['consumer_admission_restored'])
                self.assertEqual(nested.verify_cessation()['populated'], 0)
            finally:
                nested.cease()
                child.wait(timeout=5)

    def test_native_revoked_capture_cannot_queue_nested_release(self):
        with self.admission_case(linked=True) as (admission, state):
            state = self.advance_admission(admission, state)
            self.publish_native_capture(admission)
            self.controller.revoke()
            before = self.native_state(admission)
            with self.assertRaises(BoundaryError):
                admission.apply(state, 'release', 'first')
            self.assertEqual(self.native_state(admission), before)
            self.assertTrue(admission.recover()['consumer_admission_restored'])

    def test_native_linked_paused_release_cannot_dispatch_after_recovery(self):
        with self.admission_case(linked=True) as (admission, state):
            self.advance_admission(admission, state)
            self.publish_native_capture(admission)
            ready, go = admission.results / 'ready', admission.results / 'go'
            code = self.native_process_code(admission) + f'''original=a._worker
def paused(*args,**kwargs):
    Path({str(ready)!r}).touch()
    while not Path({str(go)!r}).exists(): time.sleep(.01)
    return original(*args,**kwargs)
a._worker=paused
try:
    a.apply(a.snapshot(),'release','first')
except BoundaryError:
    pass
else:
    raise RuntimeError('retired capture released')
'''
            child = subprocess.Popen([sys.executable, '-c', code],
                env={'PYTHONPATH': str(Path(supervisor.__file__).parent)})
            try:
                self.wait_file(ready)
                self.assertTrue(admission.recover()['consumer_admission_restored'])
                go.touch()
                self.assertEqual(child.wait(timeout=10), 0)
                self.assertEqual(self.native_state(admission)['phase'], 'rolled_back')
            finally:
                if child.poll() is None:
                    child.kill()
                child.wait(timeout=5)

    def test_native_lost_nested_registration_ack_preserves_dispatch_ownership(self):
        with self.admission_case(linked=True) as (admission, state):
            state = self.advance_admission(admission, state)
            self.publish_native_capture(admission)
            original = admission._save
            def lost_ack(candidate):
                result = original(candidate)
                if candidate['release_boundary']:
                    raise OSError('lost nested registration directory-fsync acknowledgement')
                return result
            with patch.object(admission, '_save', side_effect=lost_ack):
                with self.assertRaises(OSError):
                    admission.apply(state, 'release', 'first')
            reference = admission.snapshot()['release_boundary']
            owned = FixtureSupervisor(reference['directory'], reference['root'])
            self.assertEqual(owned._read()[0]['commands'], [])
            self.assertFalse(self.native_state(admission)['consumers']['first']['admission'])
            self.assertTrue(admission.recover()['consumer_admission_restored'])
            self.assertEqual(owned.verify_cessation()['populated'], 0)

    def test_native_nested_identity_drift_blocks_release_and_whole_cohort_restore(self):
        with self.admission_case(linked=True) as (admission, state):
            state = self.advance_admission(admission, state)
            self.publish_native_capture(admission)
            state = admission.apply(state, 'release', 'first')
            reference = copy.deepcopy(state['release_boundary'])
            changed = copy.deepcopy(state)
            changed['release_boundary']['boundary']['inode'] += 1
            supervisor._persist(admission.path, changed)
            before = self.native_state(admission)
            try:
                with self.assertRaisesRegex(BoundaryError, 'identity changed'):
                    admission.apply(changed, 'release', 'third')
                with self.assertRaisesRegex(BoundaryError, 'identity changed'):
                    admission.recover()
                self.assertEqual(self.native_state(admission), before)
            finally:
                fixed = admission.snapshot()
                fixed['release_boundary'] = reference
                supervisor._persist(admission.path, fixed)
            self.assertTrue(admission.recover()['consumer_admission_restored'])

    def test_native_nested_reference_cannot_target_unrelated_supervisor(self):
        with self.admission_case(linked=True) as (admission, state):
            state = self.advance_admission(admission, state)
            self.publish_native_capture(admission)
            state = admission.apply(state, 'release', 'first')
            changed = copy.deepcopy(state['release_boundary'])
            changed['root'] = str(self.root)
            with self.assertRaisesRegex(BoundaryError, 'outside original owned'):
                admission._release_reference(changed)
            self.assertFalse(self.controller._read()[0]['revoked'])
            self.assertTrue(admission.recover()['consumer_admission_restored'])

    def test_native_lost_linked_release_ack_recovers_after_partial_publication(self):
        with self.admission_case(linked=True) as (admission, state):
            state = self.advance_admission(admission, state)
            self.publish_native_capture(admission)
            original = admission._save
            def lost_ack(candidate):
                result = original(candidate)
                if candidate.get('observed', {}).get('consumers', {}).get('first', {}).get('released'):
                    raise OSError('lost linked release acknowledgement')
                return result
            with patch.object(admission, '_save', side_effect=lost_ack):
                with self.assertRaises(OSError):
                    admission.apply(state, 'release', 'first')
            self.assertTrue(self.native_state(admission)['consumers']['first']['admission'])
            self.assertFalse(self.native_state(admission)['consumers']['third']['admission'])
            self.assertTrue(admission.recover()['consumer_admission_restored'])

    def native_process_code(self, admission):
        return ('import os,time; from pathlib import Path; '
                'from kopiur_fixture_supervisor import FixtureSupervisor,BoundaryError; '
                'from kopiur_fixture_admission import NativeAdmission; '
                f'a=NativeAdmission(FixtureSupervisor({str(self.directory)!r}, {str(self.root)!r}), '
                f'{str(admission.database)!r}, {admission.identity!r}, {str(admission.results)!r}, '
                f'capture_consumer={admission.capture_consumer!r})\n')

    def test_native_actual_process_loss_after_partial_release_recovers(self):
        with self.admission_case() as (admission, state):
            self.advance_admission(admission, state)
            code = self.native_process_code(admission) + '''original=a._save
def crash(candidate):
    result=original(candidate)
    if candidate.get('observed',{}).get('consumers',{}).get('first',{}).get('released'):
        os._exit(23)
    return result
a._save=crash
a.apply(a.snapshot(),'release','first')
'''
            result = subprocess.run([sys.executable, '-c', code], timeout=10,
                env={'PYTHONPATH': str(Path(supervisor.__file__).parent)})
            self.assertEqual(result.returncode, 23)
            self.assertTrue(self.native_state(admission)['consumers']['first']['admission'])
            self.assertTrue(admission.recover()['consumer_admission_restored'])

    def test_native_paused_forward_owner_cannot_dispatch_after_independent_recovery(self):
        with self.admission_case() as (admission, state):
            ready, go = admission.results / 'ready', admission.results / 'go'
            code = self.native_process_code(admission) + f'''original=a._worker
def paused(*args,**kwargs):
    Path({str(ready)!r}).touch()
    while not Path({str(go)!r}).exists(): time.sleep(.01)
    return original(*args,**kwargs)
a._worker=paused
try:
    a.apply(a.snapshot(),'hold')
except BoundaryError:
    pass
else:
    raise RuntimeError('late hold admitted')
'''
            child = subprocess.Popen([sys.executable, '-c', code],
                env={'PYTHONPATH': str(Path(supervisor.__file__).parent)})
            try:
                self.wait_file(ready)
                self.assertTrue(admission.recover()['consumer_admission_restored'])
                go.touch()
                self.assertEqual(child.wait(timeout=10), 0)
                self.assertEqual(self.native_state(admission)['phase'], 'rolled_back')
            finally:
                if child.poll() is None:
                    child.kill()
                child.wait(timeout=5)

    def test_native_overlapping_recovery_owner_cannot_use_its_retired_boundary(self):
        with self.admission_case() as (admission, state):
            admission.apply(state, 'hold')
            ready, go = admission.results / 'ready', admission.results / 'go'
            code = self.native_process_code(admission) + f'''original=a._worker
def paused(*args,**kwargs):
    Path({str(ready)!r}).touch()
    while not Path({str(go)!r}).exists(): time.sleep(.01)
    return original(*args,**kwargs)
a._worker=paused
try:
    a.recover()
except BoundaryError:
    pass
else:
    raise RuntimeError('stale recovery succeeded')
'''
            child = subprocess.Popen([sys.executable, '-c', code],
                env={'PYTHONPATH': str(Path(supervisor.__file__).parent)})
            try:
                self.wait_file(ready)
                old_directory = admission.snapshot()['boundaries'][-1]
                recovered = admission.recover()
                self.assertTrue(recovered['consumer_admission_restored'])
                self.assertEqual(FixtureSupervisor(old_directory, self.root).verify_cessation()['populated'], 0)
                go.touch()
                self.assertEqual(child.wait(timeout=10), 0)
            finally:
                if child.poll() is None:
                    child.kill()
                child.wait(timeout=5)

    def test_native_lost_recovery_registration_ack_retries_without_unowned_dispatch(self):
        with self.admission_case() as (admission, state):
            admission.apply(state, 'hold')
            original = admission._save
            def lost_ack(candidate):
                result = original(candidate)
                if candidate['boundaries']:
                    raise OSError('lost recovery registration acknowledgement')
                return result
            with patch.object(admission, '_save', side_effect=lost_ack):
                with self.assertRaises(OSError):
                    admission.recover()
            old = FixtureSupervisor(admission.snapshot()['boundaries'][0], self.root)
            self.assertEqual(old._read()[0]['commands'], [])
            self.assertTrue(admission.recover()['consumer_admission_restored'])
            self.assertEqual(old.verify_cessation()['populated'], 0)

    def test_revocation_retry_repeats_durability_before_killing_or_restoring(self):
        self.command('import time; time.sleep(60)')
        original = supervisor._persist
        def lost_ack(path, state):
            original(path, state)
            raise OSError('lost revocation directory-fsync acknowledgement')
        with patch.object(supervisor, '_persist', side_effect=lost_ack):
            with self.assertRaises(OSError):
                self.controller.revoke()
        self.assertTrue(self.controller._read()[0]['revoked'])
        with patch.object(supervisor.os, 'fsync', side_effect=OSError('still unavailable')):
            with self.assertRaises(OSError):
                self.controller.cease()
        self.assertIn('populated 1', (self.controller._read()[1] / 'cgroup.events').read_text())
        self.assertEqual(self.controller.cease()['populated'], 0)
