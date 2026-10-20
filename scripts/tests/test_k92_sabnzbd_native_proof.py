#!/usr/bin/env python3
"""Native proof safety and real SQLite equality regression checks."""
import importlib.util
from pathlib import Path
import sqlite3
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
APP = ROOT / 'kubernetes/apps/arrs/sabnzbd/qualification'
spec = importlib.util.spec_from_file_location('native', APP / 'native-proof.py')
native = importlib.util.module_from_spec(spec)
spec.loader.exec_module(native)


class NativeProofTests(unittest.TestCase):
    def test_database_rows_not_physical_layout(self):
        with tempfile.TemporaryDirectory() as temporary:
            paths = [Path(temporary) / name for name in ('a.db', 'b.db')]
            for index, path in enumerate(paths):
                with sqlite3.connect(path) as connection:
                    connection.execute('CREATE TABLE history(id INTEGER, payload BLOB)')
                    rows = [(1, b'first'), (2, b'second')]
                    connection.executemany('INSERT INTO history VALUES(?,?)', rows[::(-1 if index else 1)])
            self.assertEqual(native.db_digest(paths[0]), native.db_digest(paths[1]))
            with sqlite3.connect(paths[1]) as connection:
                connection.execute('DELETE FROM history WHERE id=1')
            self.assertNotEqual(native.db_digest(paths[0]), native.db_digest(paths[1]))

    def test_live_wal_is_not_silently_ignored(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'wal.db'
            connection = sqlite3.connect(path)
            try:
                connection.execute('PRAGMA journal_mode=WAL')
                connection.execute('CREATE TABLE history(id INTEGER)')
                connection.commit()
                before = native.db_digest(path)
                connection.execute('INSERT INTO history VALUES(1)')
                connection.commit()
                self.assertNotEqual(before, native.db_digest(path))
            finally:
                connection.close()

    def test_corruption_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'invalid.db'
            path.write_bytes(b'not sqlite')
            with self.assertRaises(sqlite3.DatabaseError):
                native.db_digest(path)

    def test_manifest_has_no_production_or_credentials(self):
        text = (APP / 'native-proof.yaml').read_text()
        for required in ('automountServiceAccountToken: false', 'readOnlyRootFilesystem: true',
                         'policyTypes: [Ingress, Egress]', 'ingress: []', 'egress: []',
                         'claimName: sabnzbd-k92-d-20261020-nas',
                         'claimName: sabnzbd-k92-d-20261020-r2'):
            self.assertIn(required, text)
        for forbidden in ('secretKeyRef', 'secretRef', 'fsGroup:', 'claimName: sabnzbd\n',
                          'hostNetwork', 'privileged: true'):
            self.assertNotIn(forbidden, text)
        source = (APP / 'native-proof.py').read_text()
        self.assertIn("'--pause'", source)
        self.assertIn("'retirementAllowed': False", source)
        self.assertIn("module.verify('/nas', '/r2') != pair", source)
        self.assertIn("'history_retention_option': 'all'", source)
        self.assertNotIn("cfg['rss'] = {}", source)
        self.assertIn('sys.exc_info()[0] is None', source)


    def test_exact_empty_rss_native_upgrade_preserves_all_original_rows(self):
        with tempfile.TemporaryDirectory() as temporary:
            original, restored = [Path(temporary) / name for name in ('original.db', 'restored.db')]
            for path in (original, restored):
                with sqlite3.connect(path) as connection:
                    connection.execute('CREATE TABLE history(id INTEGER, payload BLOB)')
                    connection.execute('INSERT INTO history VALUES(1,?)', (b'original',))
                    connection.execute('PRAGMA user_version=7')
            with sqlite3.connect(restored) as connection:
                connection.execute('CREATE TABLE rss(id INTEGER)')
                connection.execute('PRAGMA user_version=8')
            result = native.native_database_equality(original, restored)
            self.assertTrue(result['originalRecordsEqual'])
            self.assertEqual(result['addedEmptyNativeTables'], ['rss'])
            with sqlite3.connect(restored) as connection:
                connection.execute('INSERT INTO rss VALUES(1)')
            with self.assertRaises(ValueError):
                native.native_database_equality(original, restored)
            with sqlite3.connect(restored) as connection:
                connection.execute('DELETE FROM rss')
                connection.execute('DELETE FROM history')
            with self.assertRaises(ValueError):
                native.native_database_equality(original, restored)

    def test_unknown_native_schema_change_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            original, restored = [Path(temporary) / name for name in ('original.db', 'restored.db')]
            for path in (original, restored):
                with sqlite3.connect(path) as connection:
                    connection.execute('CREATE TABLE history(id INTEGER)')
                    connection.execute('INSERT INTO history VALUES(1)')
                    connection.execute('PRAGMA user_version=8')
            with sqlite3.connect(restored) as connection:
                connection.execute('CREATE TABLE rss(id INTEGER)')
                connection.execute('PRAGMA user_version=9')
            with self.assertRaises(ValueError):
                native.native_database_equality(original, restored)


if __name__ == '__main__':
    unittest.main()
