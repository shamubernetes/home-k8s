"""Real-shell runtime escrow validation, no production credentials or Docker."""
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
HELPERS = ROOT / 'kubernetes/apps/media/grimmory/app'
GENERATION = '11111111-1111-4111-8111-111111111111'
FILES = ('application.env0', 'application.argv0', 'app-data.tar',
         'process-start.txt', 'runtime.txt')


class EscrowTests(unittest.TestCase):
    def setUp(self):
        parent = Path(os.environ.get('TMPDIR', str(Path.home() / '.hermes/cache/scratch')))
        parent.mkdir(parents=True, exist_ok=True)
        self.root = Path(tempfile.mkdtemp(prefix='k92-grimmory-unit-', dir=parent))
        self.runtime = self.root / 'runtime'
        self.runtime.mkdir()
        payloads = {
            'application.env0': b'DATABASE_PASSWORD=synthetic-only\0',
            'application.argv0': b'java\0app.jar\0',
            'app-data.tar': b'synthetic-file',
            'process-start.txt': b'123\n',
            'runtime.txt': ('format=grimmory-runtime-v1\ngeneration=' + GENERATION
                            + '\ncaptured_epoch=1791738600\n').encode(),
        }
        for name, data in payloads.items():
            (self.runtime / name).write_bytes(data)
        self.hashes()

    def tearDown(self):
        shutil.rmtree(self.root)

    def hashes(self):
        (self.runtime / 'runtime.sha256').write_text(''.join(
            hashlib.sha256((self.runtime / name).read_bytes()).hexdigest()
            + '  ' + name + '\n' for name in FILES))

    def validate(self):
        return subprocess.run(['sh', str(HELPERS / 'runtime.sh'), '--bounded',
                               'validate', str(self.runtime)], capture_output=True).returncode

    def test_valid(self):
        self.assertEqual(self.validate(), 0)

    def test_missing_env(self):
        (self.runtime / 'application.env0').unlink()
        self.assertNotEqual(self.validate(), 0)

    def test_empty_env(self):
        (self.runtime / 'application.env0').write_bytes(b'')
        self.hashes()
        self.assertNotEqual(self.validate(), 0)

    def test_roster_reduced(self):
        (self.runtime / 'runtime.sha256').write_text(
            hashlib.sha256((self.runtime / 'runtime.txt').read_bytes()).hexdigest()
            + '  runtime.txt\n')
        self.assertNotEqual(self.validate(), 0)

    def test_duplicate(self):
        path = self.runtime / 'runtime.sha256'
        path.write_text(path.read_text() + path.read_text().splitlines()[0] + '\n')
        self.assertNotEqual(self.validate(), 0)

    def test_symlink(self):
        path = self.runtime / 'application.env0'
        path.rename(self.root / 'env')
        path.symlink_to(self.root / 'env')
        self.assertNotEqual(self.validate(), 0)

    def test_bad_generation(self):
        path = self.runtime / 'runtime.txt'
        path.write_text(path.read_text().replace(GENERATION, 'invalid'))
        self.hashes()
        self.assertNotEqual(self.validate(), 0)

    def test_bad_birth(self):
        (self.runtime / 'process-start.txt').write_text('bad\n')
        self.hashes()
        self.assertNotEqual(self.validate(), 0)

    def test_tamper(self):
        (self.runtime / 'application.argv0').write_bytes(b'other\0')
        self.assertNotEqual(self.validate(), 0)


if __name__ == '__main__':
    unittest.main(verbosity=2)
