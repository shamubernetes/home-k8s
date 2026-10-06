"""Real process-interruption tests for the non-production fixture journal."""
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
