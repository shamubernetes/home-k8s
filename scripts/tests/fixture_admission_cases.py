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
from unittest.mock import patch

import kopiur_fixture_supervisor as supervisor
from kopiur_fixture_supervisor import BoundaryError, FixtureSupervisor
from kopiur_fixture_admission import NativeAdmission, create_database, operation


class NativeAdmissionCases:
    @contextlib.contextmanager
    def admission_case(self):
        with tempfile.TemporaryDirectory() as public:
            os.chmod(public, 0o777)
            database = Path(public) / (str(uuid.uuid4()) + '.sqlite')
            identity = create_database(database, {
                'first': {'identity': 'native-first', 'admission': True},
                'second': {'identity': 'native-second', 'admission': False},
                'third': {'identity': 'native-third', 'admission': True}})
            admission = NativeAdmission(self.controller, database, identity, public)
            try:
                yield admission, admission.begin()
            finally:
                if admission.path.exists():
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

    def native_process_code(self, admission):
        return ('import os,time; from pathlib import Path; '
                'from kopiur_fixture_supervisor import FixtureSupervisor,BoundaryError; '
                'from kopiur_fixture_admission import NativeAdmission; '
                f'a=NativeAdmission(FixtureSupervisor({str(self.directory)!r}, {str(self.root)!r}), '
                f'{str(admission.database)!r}, {admission.identity!r}, {str(admission.results)!r})\n')

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
