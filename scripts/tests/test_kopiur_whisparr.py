"""Whisparr requires both native databases and the same stopped-writer filetree."""
import hashlib
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock
from test_kopiur_postgres import MODULE


class WhisparrTests(unittest.TestCase):
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
        subprocess.run(['sh', '-n', str(driver)], check=True)
        substituted = subprocess.run(['flux', 'envsubst', '--strict'], input=driver.read_bytes(),
                                     capture_output=True, check=True)
        self.assertEqual(substituted.stdout, driver.read_bytes())

    def test_capture_baseline_is_opt_in_and_failure_releases_the_hold(self):
        drill = MODULE['DockerDrill']('whisparr')
        drill.capture_state = 'fixture-state'
        run = mock.Mock(return_value=subprocess.CompletedProcess([], 0, b'', b''))
        with mock.patch.object(drill, 'start', return_value='helper'), \
                mock.patch.object(drill, 'counts', side_effect=RuntimeError('baseline failed')) as counts, \
                mock.patch.dict(drill.capture.__globals__, {'run': run}):
            self.assertEqual(drill.capture('db', 'config', drill.databases), 0)
            counts.assert_not_called()
            run.reset_mock()
            with self.assertRaisesRegex(RuntimeError, 'baseline failed'):
                drill.capture('db', 'config', drill.databases, baseline=True)
            calls = [call.args[-1] for call in run.call_args_list]
            self.assertEqual(calls, ['acquire', 'release'])

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
