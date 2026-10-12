"""Exercise the exact shared-tree shell helper without production inputs."""
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import unittest

HERE = Path(__file__).resolve().parent


class BooksTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR'))
        self.root = Path(self.temp.name)
        self.books = self.root / 'books'
        self.books.mkdir()
        (self.books / 'bookdrop').mkdir()
        (self.books / 'a file.txt').write_bytes(b'original physical payload')
        self.output = self.root / 'books.tar'
        self.env = dict(os.environ, GRIMMORY_BOOKS=str(self.books),
                        GRIMMORY_BOOKDROP=str(self.books / 'bookdrop'))

    def tearDown(self):
        self.temp.cleanup()

    def run_helper(self):
        return subprocess.run(['sh', str(HERE / 'books.sh'), str(self.output)],
                              env=self.env, capture_output=True, timeout=10)

    def test_original_tree_and_empty_bookdrop(self):
        self.books.chmod(0o2775)
        result = self.run_helper()
        self.assertEqual(result.returncode, 0, result.stderr)
        with tarfile.open(self.output) as archive:
            members = archive.getmembers()
            self.assertEqual(len(members), 3)
            self.assertEqual(members[0].mode, 0o2775)
            payload = archive.extractfile('./a file.txt')
            assert payload is not None
            self.assertEqual(payload.read(), b'original physical payload')
            self.assertTrue(archive.getmember('./bookdrop').isdir())
        self.assertEqual(list(self.root.glob('.books.*')), [])

    def test_symlink_rejected_and_path_not_logged(self):
        (self.books / 'private-title').symlink_to(self.root / 'outside')
        result = self.run_helper()
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn(b'private-title', result.stdout + result.stderr)
        self.assertFalse(self.output.exists())

    def test_hardlink_rejected(self):
        os.link(self.books / 'a file.txt', self.books / 'hardlink')
        self.assertNotEqual(self.run_helper().returncode, 0)

    def test_fifo_rejected(self):
        os.mkfifo(self.books / 'pipe')
        self.assertNotEqual(self.run_helper().returncode, 0)

    def test_privileged_file_rejected(self):
        for mode in (0o4640, 0o2640):
            (self.books / 'a file.txt').chmod(mode)
            self.assertNotEqual(self.run_helper().returncode, 0)

    def test_wrong_bookdrop_mount_rejected(self):
        other = self.root / 'other'
        other.mkdir()
        self.env['GRIMMORY_BOOKDROP'] = str(other)
        self.assertNotEqual(self.run_helper().returncode, 0)

    def test_existing_output_never_replaced(self):
        self.output.write_bytes(b'prior-original')
        self.assertNotEqual(self.run_helper().returncode, 0)
        self.assertEqual(self.output.read_bytes(), b'prior-original')

    def test_between_reads_mutation_rejected(self):
        binary = self.root / 'bin'
        binary.mkdir()
        tar = shutil.which('tar')
        assert tar is not None
        wrapper = binary / 'tar'
        wrapper.write_text('#!/bin/sh\nset -eu\n'
                           'if [ -e "$MUTATION_MARKER" ]; then\n'
                           ' printf changed >"$GRIMMORY_BOOKS/a file.txt"\n'
                           'else : >"$MUTATION_MARKER"; fi\n'
                           'exec ' + tar + ' "$@"\n')
        wrapper.chmod(0o700)
        self.env.update(PATH=str(binary) + os.pathsep + self.env['PATH'],
                        MUTATION_MARKER=str(self.root / 'marker'))
        self.assertNotEqual(self.run_helper().returncode, 0)
        self.assertFalse(self.output.exists())

    def test_runtime_v2_requires_complete_exact_roster(self):
        runtime = self.root / 'runtime'
        runtime.mkdir()
        self.assertEqual(self.run_helper().returncode, 0)
        (runtime / 'books.tar').write_bytes(self.output.read_bytes())
        (runtime / 'books.txt').write_text('format=grimmory-books-v1\nsource=/books\n'
            'bookdrop_subpath=bookdrop\nconsistency=repeated-byte-metadata-stability-not-writer-fenced\n')
        payloads = {'application.env0': b'ENV=synthetic\0', 'application.argv0': b'java\0',
                    'app-data.tar': b'synthetic', 'process-start.txt': b'123\n',
                    'runtime.txt': b'format=grimmory-runtime-books-v2\ngeneration=11111111-1111-4111-8111-111111111111\ncaptured_epoch=1791738600\n'}
        for name, data in payloads.items():
            (runtime / name).write_bytes(data)
        roster = list(payloads) + ['books.tar', 'books.txt']
        checks = ''.join(hashlib.sha256((runtime / n).read_bytes()).hexdigest() + '  ' + n + '\n' for n in roster)
        (runtime / 'runtime.sha256').write_text(checks)
        def validate():
            return subprocess.run(['sh', str(HERE / 'runtime.sh'), '--bounded', 'validate', str(runtime)],
                                  capture_output=True, timeout=10).returncode
        self.assertEqual(validate(), 0)
        (runtime / 'books.tar').write_bytes(b'changed')
        self.assertNotEqual(validate(), 0)
        (runtime / 'books.tar').write_bytes(self.output.read_bytes())
        (runtime / 'runtime.sha256').write_text(''.join(checks.splitlines(True)[:-1]))
        self.assertNotEqual(validate(), 0)


if __name__ == '__main__':
    unittest.main(verbosity=2)
