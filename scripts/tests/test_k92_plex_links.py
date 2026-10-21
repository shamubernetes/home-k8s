"""Plex metadata-link regression, using the production shell function."""
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile
import unittest


REPO = Path(__file__).resolve().parents[2]
SOURCE = REPO / 'kubernetes/apps/media/plex/app/capture.sh'


class PlexLinks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve() / 'config'
        self.root.mkdir()
        (self.root / '.kopiur-native').mkdir()
        (self.root / 'Metadata').mkdir()
        (self.root / 'Metadata/original').write_bytes(b'original metadata')
        self.link = self.root / 'Metadata/link'
        source = SOURCE.read_text()
        function = source.split('state_inventory() {', 1)[1].split('\n}\nstate_inventory', 1)[0]
        self.command = '\n'.join([
            'set -euo pipefail',
            'find() { command ' + shlex.quote(shutil.which('gfind') or 'find') + ' "$@"; }',
            'readlink() { command ' + shlex.quote(shutil.which('greadlink') or 'readlink') + ' "$@"; }',
            'state_inventory() {' + function + '\n}',
            'state_inventory',
        ])

    def inventory(self):
        return subprocess.run(['bash', '-c', self.command], capture_output=True, timeout=10,
                              env=dict(os.environ, root=str(self.root),
                                       output=str(self.root / '.kopiur-native'),
                                       databases='Plug-in Support/Databases'))

    def test_relative_regular_target_is_preserved(self):
        self.link.symlink_to('original')
        result = self.inventory()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(b'/original\0', result.stdout)
        self.assertEqual(self.link.readlink(), Path('original'))

    def test_parent_relative_target_inside_capture_is_supported(self):
        (self.root / 'Metadata/sub').mkdir()
        (self.root / 'Metadata/sub/link').symlink_to('../original')
        self.assertEqual(self.inventory().returncode, 0)

    def test_absolute_target_is_rejected_even_inside_capture(self):
        self.link.symlink_to(self.root / 'Metadata/original')
        self.assertNotEqual(self.inventory().returncode, 0)

    def test_escape_target_is_rejected(self):
        (self.root.parent / 'outside').write_bytes(b'not captured')
        self.link.symlink_to('../../outside')
        self.assertNotEqual(self.inventory().returncode, 0)

    def test_dangling_link_is_rejected(self):
        self.link.symlink_to('missing')
        self.assertNotEqual(self.inventory().returncode, 0)

    def test_excluded_target_is_rejected(self):
        (self.root / 'Cache').mkdir()
        (self.root / 'Cache/file').write_bytes(b'regeneratable')
        self.link.symlink_to('../Cache/file')
        self.assertNotEqual(self.inventory().returncode, 0)

    def test_directory_target_is_rejected(self):
        self.link.symlink_to('.')
        self.assertNotEqual(self.inventory().returncode, 0)

    def test_special_file_is_rejected(self):
        os.mkfifo(self.root / 'Metadata/pipe')
        self.assertNotEqual(self.inventory().returncode, 0)


if __name__ == '__main__':
    unittest.main()
