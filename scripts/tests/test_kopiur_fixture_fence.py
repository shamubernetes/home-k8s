"""Real process-interruption tests for the non-production fixture journal."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from kopiur_fixture_fence import FixtureFence

SOURCE = 'k8s92-nonrel-' + 'a' * 32 + '-source'


class FenceTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'fence.json'

    def test_release_only_after_acknowledgement(self):
        with FixtureFence(self.path, SOURCE) as journal:
            journal.acquire(lambda: None)
            def failure():
                raise RuntimeError('release failed')
            with self.assertRaises(RuntimeError):
                journal.recover(failure)
            self.assertEqual(journal.read(), 'fenced')
            self.assertTrue(journal.recover(lambda: None))
            self.assertFalse(journal.recover(lambda: self.fail('duplicate release')))

    def test_exclusive_owner(self):
        with FixtureFence(self.path, SOURCE):
            with self.assertRaises(RuntimeError):
                with FixtureFence(self.path, SOURCE):
                    self.fail('second owner admitted')

    def test_cross_source_recovery_denied(self):
        with FixtureFence(self.path, SOURCE) as journal:
            journal.acquire(lambda: None)
        with FixtureFence(self.path, SOURCE.replace('a', 'b', 1)) as journal:
            with self.assertRaises(RuntimeError):
                journal.recover(lambda: self.fail('wrong boundary released'))

    def test_corrupt_journal_denied(self):
        self.path.write_text('{"source":"unexpected","phase":"fenced"}')
        with FixtureFence(self.path, SOURCE) as journal:
            with self.assertRaises(RuntimeError):
                journal.recover(lambda: self.fail('corrupt boundary released'))

    def test_production_source_denied(self):
        with self.assertRaises(ValueError):
            FixtureFence(self.path, 'elasticsearch-production')

    def test_native_admission_persisted_before_first_mutation(self):
        admission: dict[str, str | None] = {'index_uuid': 'original-index', 'write_block': 'true'}
        with FixtureFence(self.path, SOURCE, admission=admission) as journal:
            admission['write_block'] = None
            def block():
                state = json.loads(self.path.read_text())
                self.assertEqual(state['phase'], 'intent')
                self.assertEqual(state['admission'], {'index_uuid': 'original-index', 'write_block': 'true'})
            journal.acquire(block)
        with FixtureFence(self.path, SOURCE, admission={'index_uuid': 'original-index', 'write_block': 'true'}) as journal:
            self.assertTrue(journal.recover(lambda: None))

    def test_replaced_index_or_changed_prior_admission_denies_release(self):
        original = {'index_uuid': 'original-index', 'write_block': None}
        with FixtureFence(self.path, SOURCE, admission=original) as journal:
            journal.acquire(lambda: None)
        before = self.path.read_bytes()
        for changed in ({'index_uuid': 'replacement-index', 'write_block': None},
                        {'index_uuid': 'original-index', 'write_block': 'false'},
                        {'index_uuid': 'original-index', 'write_block': 'true'}):
            with self.subTest(changed=changed), FixtureFence(self.path, SOURCE, admission=changed) as journal:
                with self.assertRaises(RuntimeError):
                    journal.recover(lambda: self.fail('changed native boundary released'))
                self.assertEqual(self.path.read_bytes(), before)

    def test_legacy_journal_cannot_be_upgraded_or_downgraded_implicitly(self):
        admission = {'index_uuid': 'original-index', 'write_block': None}
        with FixtureFence(self.path, SOURCE) as journal:
            journal.acquire(lambda: None)
        with FixtureFence(self.path, SOURCE, admission=admission) as journal:
            with self.assertRaises(RuntimeError):
                journal.recover(lambda: self.fail('unbound journal admitted'))
        self.path.unlink()
        with FixtureFence(self.path, SOURCE, admission=admission) as journal:
            journal.acquire(lambda: None)
        with FixtureFence(self.path, SOURCE) as journal:
            with self.assertRaises(RuntimeError):
                journal.recover(lambda: self.fail('native binding bypassed'))

    def test_native_lost_release_ack_retains_exact_prior_block(self):
        for block in (None, 'false', 'true'):
            with self.subTest(block=block):
                self.path.unlink(missing_ok=True)
                admission = {'index_uuid': 'original-index', 'write_block': block}
                with FixtureFence(self.path, SOURCE, admission=admission) as journal:
                    journal.acquire(lambda: None)
                    def lost_ack():
                        raise RuntimeError('lost acknowledgement')
                    with self.assertRaises(RuntimeError):
                        journal.recover(lost_ack)
                    self.assertEqual(journal.read(), 'fenced')
                with FixtureFence(self.path, SOURCE, admission=admission) as restarted:
                    self.assertTrue(restarted.recover(lambda: None))
                    self.assertFalse(restarted.recover(lambda: self.fail('duplicate release')))
                self.assertEqual(json.loads(self.path.read_text())['admission'], admission)

    def test_malformed_native_admission_refused(self):
        for admission in ({}, {'index_uuid': '', 'write_block': None},
                          {'index_uuid': 'index', 'write_block': False},
                          {'index_uuid': 'index', 'write_block': 'unknown'}):
            with self.subTest(admission=admission), self.assertRaises(ValueError):
                FixtureFence(self.path, SOURCE, admission=admission)

    def test_native_restart_loads_prior_not_current_block(self):
        for prior in (None, 'false', 'true'):
            with self.subTest(prior=prior):
                self.path.unlink(missing_ok=True)
                admission = {'index_uuid': 'original-index', 'write_block': prior}
                with FixtureFence(self.path, SOURCE, admission=admission) as journal:
                    journal.acquire(lambda: None)
                observed = {'index_uuid': 'original-index', 'write_block': 'true'}
                with FixtureFence.restart_native(self.path, SOURCE, lambda: observed) as (journal, saved):
                    self.assertEqual(saved, admission)
                    saved['write_block'] = 'false'
                    self.assertEqual(json.loads(journal._admission), admission)
                    self.assertTrue(journal.recover(lambda: None))
                self.assertEqual(json.loads(self.path.read_text())['admission'], admission)

    def test_native_restart_refuses_replacement_and_legacy_without_write(self):
        admission = {'index_uuid': 'original-index', 'write_block': None}
        with FixtureFence(self.path, SOURCE, admission=admission) as journal:
            journal.acquire(lambda: None)
        before = self.path.read_bytes()
        with self.assertRaises(RuntimeError):
            with FixtureFence.restart_native(self.path, SOURCE, lambda: {
                    'index_uuid': 'replacement', 'write_block': 'true'}):
                self.fail('replacement admitted')
        self.assertEqual(self.path.read_bytes(), before)
        self.path.unlink()
        with FixtureFence(self.path, SOURCE) as journal:
            journal.acquire(lambda: None)
        before = self.path.read_bytes()
        with self.assertRaises(RuntimeError):
            with FixtureFence.restart_native(self.path, SOURCE, lambda: self.fail('legacy observed')):
                self.fail('legacy admitted')
        self.assertEqual(self.path.read_bytes(), before)

    def test_durable_admission_reads_exact_persisted_state(self):
        for prior in (None, 'false', 'true'):
            with self.subTest(prior=prior):
                self.path.unlink(missing_ok=True)
                admission = {'index_uuid': 'original-index', 'write_block': prior}
                with FixtureFence(self.path, SOURCE, admission=admission) as journal:
                    journal.acquire(lambda: None)
                self.assertEqual(
                    json.loads(FixtureFence.durable_admission(self.path, SOURCE)), admission)

    def test_durable_admission_denies_absent_foreign_and_legacy(self):
        admission = {'index_uuid': 'original-index', 'write_block': None}
        missing = self.path.with_name('missing.json')
        with self.assertRaises(RuntimeError):
            FixtureFence.durable_admission(missing, SOURCE)
        with FixtureFence(self.path, SOURCE, admission=admission) as journal:
            journal.acquire(lambda: None)
        with self.assertRaises(RuntimeError):
            FixtureFence.durable_admission(self.path, SOURCE.replace('a', 'b', 1))
        self.path.unlink()
        with FixtureFence(self.path, SOURCE) as legacy:
            legacy.acquire(lambda: None)
        with self.assertRaises(RuntimeError):
            FixtureFence.durable_admission(self.path, SOURCE)
        self.assertNotIn('admission', json.loads(self.path.read_text()))

    def test_native_restart_observer_runs_only_while_exclusively_owned(self):
        admission = {'index_uuid': 'original-index', 'write_block': None}
        with FixtureFence(self.path, SOURCE, admission=admission) as journal:
            journal.acquire(lambda: None)
            with self.assertRaises(RuntimeError):
                with FixtureFence.restart_native(self.path, SOURCE, lambda: self.fail('live owner observed')):
                    self.fail('live owner bypassed')
        def observe():
            with self.assertRaises(RuntimeError):
                with FixtureFence(self.path, SOURCE):
                    self.fail('observer outside ownership')
            return admission
        with FixtureFence.restart_native(self.path, SOURCE, observe) as (journal, saved):
            self.assertEqual(saved, admission)
            self.assertEqual(journal.read(), 'fenced')

    def test_real_process_loss_before_and_after_fence_ack(self):
        for point in ('intent', 'fenced', 'released-before-journal'):
            with self.subTest(point=point):
                self.path.unlink(missing_ok=True)
                boundary = self.path.with_suffix('.boundary')
                code = '''import os,sys
from pathlib import Path
from kopiur_fixture_fence import FixtureFence
path,source,point=sys.argv[1:]
boundary=Path(path).with_suffix('.boundary')
with FixtureFence(path,source) as journal:
 def block():
  boundary.write_text('fenced')
  if point=='intent': os._exit(73)
 journal.acquire(block)
 if point=='released-before-journal':
  def release():
   boundary.write_text('released')
   os._exit(73)
  journal.recover(release)
 os._exit(73)
'''
                env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1]))
                result = subprocess.run([sys.executable, '-c', code, str(self.path), SOURCE, point],
                                        env=env, capture_output=True, timeout=10)
                self.assertEqual(result.returncode, 73, result.stderr)
                with FixtureFence(self.path, SOURCE) as journal:
                    self.assertTrue(journal.recover(lambda: boundary.write_text('released')))
                    self.assertEqual(journal.read(), 'released')
                    self.assertEqual(boundary.read_text(), 'released')
                self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)


if __name__ == '__main__':
    unittest.main()
