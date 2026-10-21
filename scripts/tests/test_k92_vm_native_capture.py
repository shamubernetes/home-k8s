"""Filesystem unit tests for VM's same-volume native snapshot export."""
import importlib.util
import os
from pathlib import Path
import tempfile
import unittest

MODULE = Path(__file__).resolve().parents[2] / 'kubernetes/apps/observability/victoria-metrics-ha/app/capture-native.py'
spec = importlib.util.spec_from_file_location('vm_native_capture', MODULE)
assert spec is not None and spec.loader is not None
capture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(capture)


class NativeLinks(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR'))
        self.root = Path(self.directory.name) / 'capture'
        self.root.mkdir()
        setattr(capture, 'ROOT', self.root)

    def tearDown(self):
        self.directory.cleanup()

    def test_materializes_contained_native_links_without_inode_mutation(self):
        point = '20260101000000-0123456789ABCDEF'
        native = self.root / 'snapshots' / point
        (native / 'data').mkdir(parents=True)
        source = self.root / 'data/small/snapshots' / point
        source.mkdir(parents=True)
        part = source / 'fixture-part'
        part.write_bytes(b'unit-fixture-only')
        part.chmod(0o600)
        (native / 'data/small').symlink_to('../../../data/small/snapshots/' + point)
        before = part.stat()
        target = self.root / 'private-export'
        count, size = capture.link_native(native, target)
        copied = target / 'data/small/fixture-part'
        self.assertEqual((count, size), (1, len(b'unit-fixture-only')))
        self.assertEqual(copied.read_bytes(), part.read_bytes())
        self.assertEqual(copied.stat().st_ino, before.st_ino)
        self.assertEqual((part.stat().st_mode, part.stat().st_uid, part.stat().st_gid),
                         (before.st_mode, before.st_uid, before.st_gid))

    def test_rejects_link_outside_original_volume(self):
        outside = self.root.parent / 'outside'
        outside.write_text('unit fixture')
        source = self.root / 'escape'
        source.symlink_to(outside)
        with self.assertRaisesRegex(AssertionError, 'escapes source volume'):
            capture.link_native(source, self.root / 'export')

    def test_rejects_native_link_cycle(self):
        source = self.root / 'native'
        source.mkdir()
        (source / 'cycle').symlink_to('.')
        with self.assertRaisesRegex(AssertionError, 'link cycle'):
            capture.link_native(source, self.root / 'export')


if __name__ == '__main__':
    unittest.main()
