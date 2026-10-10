"""Whisparr requires both native databases and the same stopped-writer filetree."""
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from test_kopiur_postgres import MODULE


class WhisparrTests(unittest.TestCase):
    def configure_bundle_ignore(self, config, mode='install'):
        driver = MODULE['ROOT'] / 'scripts/kopiur-whisparr-recurring-capture'
        script = driver.read_text().split('state=$(printenv', 1)[0]
        # Exercise the same function on macOS, where stat uses different flags.
        if sys.platform == 'darwin':
            script = script.replace('stat -c %h', 'stat -f %l')
        return subprocess.run(['sh', '-c', script + '\nconfigure_bundle_ignore "$1" "$2"',
                               'install-test', str(config), mode], capture_output=True)

    def test_native_ignore_is_owned_ordered_and_idempotent(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary)
            self.assertEqual(self.configure_bundle_ignore(config).returncode, 0)
            ignore = config / '.kopiaignore'
            expected = ('# Kopiur Whisparr paired native recovery bundle.\n'
                        '/*\n!/.kopiur-postgres\n/.kopiur-postgres/*\n'
                        '!/.kopiur-postgres/COMPLETE\n!/.kopiur-postgres/current\n')
            self.assertEqual(ignore.read_text(), expected)
            before = ignore.stat()
            self.assertEqual(before.st_nlink, 1)
            self.assertEqual(before.st_mode & 0o777, 0o600)
            self.assertEqual(self.configure_bundle_ignore(config).returncode, 0)
            self.assertEqual(ignore.stat().st_ino, before.st_ino)
            self.assertEqual(ignore.read_text(), expected)
            self.assertEqual(list(config.glob('.kopiur-ignore.*')), [])
            script = (MODULE['ROOT'] / 'scripts/kopiur-whisparr-recurring-capture').read_text()
            self.assertLess(script.index('configure_bundle_ignore /config'), script.index('sh "$helper" acquire'))

    def test_native_ignore_collisions_fail_without_replacement(self):
        for kind in ('unknown', 'symlink', 'dangling', 'hardlink', 'directory', 'fifo'):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as temporary:
                config = Path(temporary)
                ignore = config / '.kopiaignore'
                target = config / 'target'
                target.write_bytes(b'user-owned rules\n')
                if kind == 'unknown':
                    ignore.write_bytes(b'user-owned rules\n')
                elif kind in ('symlink', 'dangling'):
                    ignore.symlink_to(target if kind == 'symlink' else config / 'missing')
                elif kind == 'hardlink':
                    self.assertEqual(self.configure_bundle_ignore(config).returncode, 0)
                    target.unlink()
                    os.link(ignore, target)
                elif kind == 'directory':
                    ignore.mkdir()
                else:
                    os.mkfifo(ignore)
                before = ignore.lstat()
                original = ignore.read_bytes() if kind in ('unknown', 'hardlink') else None
                for mode in ('install', 'remove'):
                    self.assertNotEqual(self.configure_bundle_ignore(config, mode).returncode, 0)
                    self.assertEqual(ignore.lstat().st_ino, before.st_ino)
                    self.assertEqual(ignore.lstat().st_mode, before.st_mode)
                    if original is not None:
                        self.assertEqual(ignore.read_bytes(), original)
                    self.assertEqual(list(config.glob('.kopiur-ignore.*')), [])

    def test_native_ignore_rollback_removes_only_owned_filter(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary)
            untouched = config / 'config.xml'
            untouched.write_bytes(b'original application data')
            self.assertEqual(self.configure_bundle_ignore(config).returncode, 0)
            self.assertEqual(self.configure_bundle_ignore(config, 'remove').returncode, 0)
            self.assertFalse((config / '.kopiaignore').exists())
            self.assertEqual(untouched.read_bytes(), b'original application data')
            self.assertEqual(self.configure_bundle_ignore(config, 'remove').returncode, 0)
            self.assertEqual(list(config.glob('.kopiur-ignore.*')), [])
            self.assertNotEqual(self.configure_bundle_ignore(config, 'typo').returncode, 0)

    def test_bundle_requires_exact_ordered_two_database_inventory(self):
        for databases, accepted in ((['whisparrv3_main', 'whisparrv3_logs'], True),
                                    (['whisparrv3_main'], False),
                                    (['whisparrv3_logs', 'whisparrv3_main'], False),
                                    (['radarr_main', 'whisparrv3_logs'], False)):
            with self.subTest(databases=databases), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                current = root / 'current'
                current.mkdir()
                artifacts = {'application-config': b'<Config/>', 'filetree.sha256': b'a' * 64 + b'\n',
                             'metadata': ('format=2\nconsistency=quiesced-whisparr-stable-filetree\n' +
                                          ''.join('database=' + db + ' server_version=170000 tables=1\n'
                                                  for db in databases)).encode()}
                archive = io.BytesIO()
                with MODULE['tarfile'].open(fileobj=archive, mode='w') as handle:
                    member = MODULE['tarfile'].TarInfo('config.xml')
                    member.size = 1
                    handle.addfile(member, io.BytesIO(b'x'))
                artifacts['application-state.tar'] = archive.getvalue()
                for db in databases:
                    artifacts[db + '.dump'] = b'synthetic inventory test only'
                    artifacts[db + '.toc'] = b'synthetic inventory test only'
                for name, data in artifacts.items():
                    (current / name).write_bytes(data)
                (current / 'SHA256SUMS').write_text(''.join(hashlib.sha256(data).hexdigest() + '  ' + name + '\n'
                                                        for name, data in artifacts.items()))
                (root / 'COMPLETE').write_text('complete\n')
                if accepted:
                    self.assertEqual(MODULE['verify_bundle'](root), databases)
                else:
                    with self.assertRaisesRegex(ValueError, 'unexpected database inventory'):
                        MODULE['verify_bundle'](root)

    def test_native_catalog_uses_foreign_id_not_optional_stash_id(self):
        records = [{'id': 1, 'foreignId': 'native-identity', 'title': 'Fixture', 'path': '/media/fixture'}]
        def sql(query):
            self.assertIn('mm."ForeignId"', query)
            self.assertNotIn('mm."StashId"', query)
            return json.dumps(records)
        result = MODULE['validate_radarr_api'](records, 1, sql, app='whisparr')
        self.assertEqual(result['native_records_equal'], 1)
        wrong = [dict(records[0], foreignId='changed')]
        with self.assertRaisesRegex(ValueError, 'records differ'):
            MODULE['validate_radarr_api'](wrong, 1, sql, app='whisparr')

    def test_multi_database_capture_requires_bounded_hold_before_and_after(self):
        script = MODULE['CAPTURE'].read_text()
        case = script.split('  quiesced-whisparr-stable-filetree)', 1)[1].split('    ;;', 1)[0]
        self.assertIn('[ "$PGDATABASES" = "whisparrv3_main whisparrv3_logs" ]', case)
        self.assertIn('sh /kopiur/recurring.sh check 660', case)
        tail = script.split('printf \'completed_at=', 1)[1]
        self.assertIn('sh /kopiur/recurring.sh check 1', tail)
        self.assertLess(tail.index('check 1'), tail.index('COMPLETE.tmp'))
        driver = MODULE['ROOT'] / 'scripts/kopiur-whisparr-recurring-capture'
        self.assertIn('sh "$helper" check 1500', driver.read_text())
        self.assertIn('timeout --kill-after=30 1200 sh /kopiur/capture.sh', driver.read_text())
        self.assertLess(driver.read_text().index('sh "$helper" check 1500'),
                        driver.read_text().index('timeout --kill-after'))
        subprocess.run(['sh', '-n', str(driver)], check=True)
        substituted = subprocess.run(['flux', 'envsubst', '--strict'], input=driver.read_bytes(),
                                     capture_output=True, check=True)
        self.assertEqual(substituted.stdout, driver.read_bytes())

    def test_capture_baseline_is_opt_in_and_failure_releases_the_hold(self):
        drill = MODULE['DockerDrill']('whisparr')
        drill.capture_state = 'fixture-state'
        run = mock.Mock(return_value=subprocess.CompletedProcess([], 0, b'', b''))
        with mock.patch.object(drill, 'start', return_value='helper'), \
                mock.patch.object(drill, 'healthy') as healthy, \
                mock.patch.object(drill, 'counts', side_effect=RuntimeError('baseline failed')) as counts, \
                mock.patch.dict(drill.capture.__globals__, {'run': run}):
            self.assertEqual(drill.capture('db', 'config', drill.databases), 0)
            counts.assert_not_called()
            run.reset_mock()
            with self.assertRaisesRegex(RuntimeError, 'baseline failed'):
                drill.capture('db', 'config', drill.databases, baseline=True)
            calls = [call.args[-1] for call in run.call_args_list]
            self.assertEqual(calls, ['acquire', 'release'])
            self.assertEqual(healthy.call_count, 2)

    def test_legacy_whisparr_cannot_reach_disposable_database(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary)
            with mock.patch.dict(MODULE['restore_pvc'].__globals__,
                                 {'verify_bundle': mock.Mock(return_value=['whisparrv3_main', 'whisparrv3_logs']),
                                  'DockerDrill': mock.Mock()}) as globals_map:
                current = source / '.kopiur-postgres/current'
                current.mkdir(parents=True)
                (current / 'metadata').write_text('format=1\nconsistency=per-database-snapshot-before-pvc\n')
                with self.assertRaisesRegex(ValueError, 'paired stable-filetree'):
                    MODULE['restore_pvc'](source, 'whisparr')
                globals_map['DockerDrill'].assert_not_called()


if __name__ == '__main__':
    unittest.main()
