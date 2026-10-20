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
    def test_helpers_hot_update_without_application_reload(self):
        contract = (HERE / 'kustomization.yaml').read_text().split('configMapGenerator:\n', 1)[1]
        self.assertIn('name: grimmory-backup-contract', contract)
        self.assertIn('disableNameSuffixHash: true', contract)
        self.assertIn('reloader.stakater.com/ignore: "true"', contract)
        self.assertIn('kustomize.toolkit.fluxcd.io/substitute: disabled', contract)
        mount = (HERE / 'helmrelease.yaml').read_text().split('      backup-contract:\n', 1)[1].split('      config:\n', 1)[0]
        self.assertIn('name: grimmory-backup-contract', mount)
        self.assertIn('path: /backup-contract', mount)
        self.assertNotIn('subPath:', mount)

    def test_snapshot_excludes_only_transient_runtime_control(self):
        policy = (HERE / 'kopiur-policy.yaml').read_text()
        block = policy.split('  files:\n', 1)[1].split('  errorHandling:\n', 1)[0]
        rules = [line.strip()[2:].strip("'\"") for line in block.splitlines()
                 if line.startswith('    - ')]
        self.assertEqual(rules, ['/lost+found', 'System Volume Information',
                                '$RECYCLE.BIN', '@eaDir', '.snapshot',
                                '/grimmory-runtime/fence', '/grimmory-runtime/.capture.*'])
        errors = policy.split('  errorHandling:\n', 1)[1].split('  hooks:\n', 1)[0]
        self.assertEqual({line.strip() for line in errors.splitlines()},
                         {'ignoreDirErrors: false', 'ignoreFileErrors: false',
                          'ignoreUnknownTypes: false'})
        self.assertIn('runAsUser: 568', policy)
        self.assertIn('continueOnFailure: false', policy)

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

    def test_absent_default_alias_uses_complete_canonical_subtree(self):
        if Path('/bookdrop').exists() or Path('/bookdrop').is_symlink():
            self.skipTest('runner has a real default alias')
        self.env.pop('GRIMMORY_BOOKDROP')
        (self.books / 'bookdrop' / 'pending.txt').write_bytes(b'pending physical payload')
        result = self.run_helper()
        self.assertEqual(result.returncode, 0, result.stderr)
        with tarfile.open(self.output) as archive:
            payload = archive.extractfile('./bookdrop/pending.txt')
            if payload is None:
                self.fail('canonical bookdrop payload missing')
            self.assertEqual(payload.read(), b'pending physical payload')

    def test_explicit_missing_alias_is_not_replaced(self):
        self.env['GRIMMORY_BOOKDROP'] = str(self.root / 'missing')
        result = self.run_helper()
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.output.exists())

    def test_absent_canonical_subtree_is_not_omitted(self):
        (self.books / 'bookdrop').rmdir()
        result = self.run_helper()
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.output.exists())

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


# Synthetic SQL/procfs exercise the shell chain, not native recovery or host locks.
GENERATION = '11111111-1111-4111-8111-111111111111'


class OnlineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR'))
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        self.runtime = self.root / 'escrow'
        self.bundle = self.root / 'bundle'
        self.data = self.root / 'data'
        self.books = self.root / 'books'
        for directory in (self.runtime, self.bundle, self.data, self.books):
            directory.mkdir()
        (self.books / 'bookdrop').mkdir()
        (self.books / 'bookdrop' / 'incoming').write_text('synthetic incoming book')
        (self.books / 'book').write_text('synthetic library book')
        (self.data / 'config').write_text('synthetic config')
        self.proc = self.root / 'proc'
        self.java = self.proc / '42'
        self.java.mkdir(parents=True)
        (self.java / 'comm').write_text('java\n')
        (self.java / 'status').write_text('Uid:\t568\t568\t568\t568\n')
        # Fields after comm: state is 1, birth tick is 20.
        (self.java / 'stat').write_text('42 (java) S ' + '0 ' * 18 + '123\n')
        (self.java / 'environ').write_bytes(b'ENV=synthetic\0')
        (self.java / 'cmdline').write_bytes(b'java\0synthetic.jar\0')
        uuid = self.proc / 'sys/kernel/random/uuid'
        uuid.parent.mkdir(parents=True)
        uuid.write_text(GENERATION + '\n')
        for name in ('runtime.sh', 'backup.sh', 'books.sh'):
            source = (HERE / name).read_text().replace(
                'root=/runtime-escrow', 'root=' + str(self.runtime)).replace(
                'runtime=/runtime-escrow', 'runtime=' + str(self.runtime))
            (self.root / name).write_text(source)
        # Any accidental dependency on a writer-fence marker fails this fixture.
        (self.root / 'fence.sh').write_text('#!/bin/sh\nexit 99\n')
        self.tool('chown', 'exit 0\n')
        self.tool('flock', 'exit 0\n')  # No host flock; concurrency lock is not qualified here.
        self.tool('su-exec', 'shift; exec "$@"\n')
        self.tool('mariadb', '''case "$*" in
  *VERSION*) echo 11.8.8-synthetic ;;
  *"engine <>"*) echo "${TEST_ENGINES:-0}" ;;
  *) echo 73 ;;
esac
''')
        self.tool('mariadb-dump', '''case "$*" in
  *--no-data*)
    if [ -f "$TEST_ROOT/dumped" ] && [ "${TEST_MUTATE:-}" = schema ]; then
      echo 'CREATE TABLE changed'; else echo 'CREATE TABLE synthetic'; fi ;;
  *)
    [ "${TEST_MUTATE:-}" != dump-fail ] || exit 1
    : >"$TEST_ROOT/dumped"
    case "${TEST_MUTATE:-}" in
      books) echo changed >"$GRIMMORY_BOOKS/book" ;;
      bookdrop) echo changed >"$GRIMMORY_BOOKDROP/incoming" ;;
      data) echo changed >"$GRIMMORY_APP_DATA/config" ;;
      env) echo changed >"$GRIMMORY_PROC_ROOT/42/environ" ;;
      birth) echo '42 (java) S 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 124' >"$GRIMMORY_PROC_ROOT/42/stat" ;;
      generation) echo '22222222-2222-4222-8222-222222222222' >"$TEST_ROOT/escrow/COMPLETE" ;;
    esac
    echo 'synthetic transactional SQL' ;;
esac
''')
        self.env = dict(os.environ, PATH=str(self.bin) + os.pathsep + os.environ['PATH'],
                        TEST_ROOT=str(self.root), GRIMMORY_PROC_ROOT=str(self.proc),
                        GRIMMORY_APP_DATA=str(self.data), GRIMMORY_BOOKS=str(self.books),
                        GRIMMORY_BOOKDROP=str(self.books / 'bookdrop'),
                        GRIMMORY_DB_BUNDLE=str(self.bundle), GRIMMORY_BACKUP_DIR=str(self.bundle),
                        MYSQL_USER='synthetic', MYSQL_PASSWORD='synthetic')

    def tool(self, name, body):
        path = self.bin / name
        path.write_text('#!/bin/sh\nset -eu\n' + body)
        path.chmod(0o700)

    def run_shell(self, name, mode=None):
        args = ['bash' if name == 'backup.sh' else 'sh', str(self.root / name), '--bounded']
        if mode:
            args.append(mode)
        return subprocess.run(args, env=self.env, capture_output=True, timeout=20)

    def capture(self):
        result = self.run_shell('runtime.sh', 'capture')
        self.assertEqual(result.returncode, 0, result.stderr)

    def dump(self):
        result = self.run_shell('backup.sh')
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_hot_chain_without_fence_markers(self):
        self.capture()
        self.dump()
        result = self.run_shell('runtime.sh', 'verify')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.runtime / 'COMPLETE').read_text(), GENERATION + '\n')
        self.assertEqual((self.bundle / 'COMPLETE').read_text(), GENERATION + '\n')
        for name in ('application.env0', 'application.argv0', 'app-data.tar', 'books.tar',
                     'books.txt', 'process-start.txt', 'runtime.txt', 'runtime.sha256'):
            self.assertEqual((self.runtime / 'current' / name).stat().st_mode & 0o777, 0o640)
        self.assertEqual((self.runtime / 'current').stat().st_mode & 0o777, 0o750)
        self.assertEqual((self.bundle / 'grimmory.sql').stat().st_mode & 0o777, 0o640)
        self.assertEqual(list(self.runtime.glob('.capture.*')), [])

    def test_mutation_across_sql_invalidates_pair(self):
        for mutation in ('books', 'bookdrop', 'data', 'env', 'birth'):
            with self.subTest(mutation=mutation):
                self.capture()
                self.env['TEST_MUTATE'] = mutation
                self.dump()
                result = self.run_shell('runtime.sh', 'verify')
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse((self.runtime / 'COMPLETE').exists())
        self.env.pop('TEST_MUTATE')

    def test_dump_failures_never_publish_complete(self):
        for mutation in ('schema', 'dump-fail', 'generation'):
            with self.subTest(mutation=mutation):
                self.capture()
                self.env['TEST_MUTATE'] = mutation
                result = self.run_shell('backup.sh')
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse((self.bundle / 'COMPLETE').exists())
        self.env.pop('TEST_MUTATE')

    def test_nontransactional_tables_rejected(self):
        self.capture()
        self.env['TEST_ENGINES'] = '1'
        self.assertNotEqual(self.run_shell('backup.sh').returncode, 0)
        self.assertFalse((self.bundle / 'COMPLETE').exists())

    def test_killed_verifier_cannot_leave_runtime_complete(self):
        self.capture()
        self.dump()
        self.tool('su-exec', 'kill -KILL "$PPID"\nexit 1\n')
        self.assertNotEqual(self.run_shell('runtime.sh', 'verify').returncode, 0)
        self.assertFalse((self.runtime / 'COMPLETE').exists())

    def test_replacement_pid_with_same_birth_tick_rejected(self):
        self.capture()
        self.dump()
        self.java.rename(self.proc / '43')
        self.assertNotEqual(self.run_shell('runtime.sh', 'verify').returncode, 0)
        self.assertFalse((self.runtime / 'COMPLETE').exists())

    def test_stale_runtime_refuses_sql(self):
        self.capture()
        current = self.runtime / 'current'
        runtime = current / 'runtime.txt'
        lines = runtime.read_text().splitlines()
        runtime.write_text('\n'.join('captured_epoch=1' if line.startswith('captured_epoch=')
                                    else line for line in lines) + '\n')
        roster = [line.split()[1] for line in (current / 'runtime.sha256').read_text().splitlines()]
        (current / 'runtime.sha256').write_text(''.join(
            hashlib.sha256((current / name).read_bytes()).hexdigest() + '  ' + name + '\n'
            for name in roster))
        self.assertNotEqual(self.run_shell('backup.sh').returncode, 0)
        self.assertFalse((self.bundle / 'COMPLETE').exists())

    def test_generation_and_manifest_binding(self):
        for target in ('COMPLETE', 'runtime/runtime.txt', 'manifest.txt', 'grimmory.sql'):
            with self.subTest(target=target):
                self.capture()
                self.dump()
                path = self.bundle / target
                if target == 'manifest.txt':
                    path.write_text(path.read_text().replace('sql_sha256=', 'sql_sha256=bad'))
                else:
                    path.write_bytes(path.read_bytes() + b'tampered\n')
                self.assertNotEqual(self.run_shell('runtime.sh', 'verify').returncode, 0)
                self.assertFalse((self.runtime / 'COMPLETE').exists())

    def test_missing_ambiguous_wrong_uid_and_stopped_java_fail_closed(self):
        for invalid in ('missing', 'ambiguous', 'uid', 'stopped'):
            with self.subTest(invalid=invalid):
                self.capture()
                comm = self.java / 'comm'
                status = self.java / 'status'
                stat = self.java / 'stat'
                originals = {p: p.read_bytes() for p in (comm, status, stat)}
                if invalid == 'missing':
                    comm.write_text('not-java\n')
                elif invalid == 'ambiguous':
                    shutil.copytree(self.java, self.proc / '43')
                elif invalid == 'uid':
                    status.write_text('Uid:\t0\t0\t0\t0\n')
                else:
                    stat.write_text(stat.read_text().replace(') S ', ') T '))
                self.assertNotEqual(self.run_shell('runtime.sh', 'capture').returncode, 0)
                self.assertFalse((self.runtime / 'COMPLETE').exists())
                for path, payload in originals.items():
                    path.write_bytes(payload)
                shutil.rmtree(self.proc / '43', ignore_errors=True)

    def test_during_archive_mutations_rejected(self):
        real_tar = shutil.which('tar')
        if real_tar is None:
            self.fail('tar is required')
        self.tool('tar', '''count=0
[ ! -f "$TEST_ROOT/tar-count" ] || count=$(cat "$TEST_ROOT/tar-count")
count=$((count+1)); echo "$count" >"$TEST_ROOT/tar-count"
case "$count:${TEST_MUTATE:-}" in
  2:data) echo changed >"$GRIMMORY_APP_DATA/config" ;;
  4:books) echo changed >"$GRIMMORY_BOOKS/book" ;;
  4:env) echo changed >"$GRIMMORY_PROC_ROOT/42/environ" ;;
esac
exec ''' + real_tar + ' "$@"\n')
        for mutation in ('data', 'books', 'env'):
            with self.subTest(mutation=mutation):
                (self.root / 'tar-count').unlink(missing_ok=True)
                self.env['TEST_MUTATE'] = mutation
                result = self.run_shell('runtime.sh', 'capture')
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse((self.runtime / 'COMPLETE').exists())
                self.assertNotIn(b'synthetic config', result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main(verbosity=2)
