"""The online Plex backup must pin a WAL reader, never wait for writer quiet."""
import sqlite3
import tempfile
from pathlib import Path
import unittest


class WalReadSnapshotTest(unittest.TestCase):
    def test_pinned_reader_restores_its_point_while_writers_commit(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            source = root / 'live.db'
            writer = sqlite3.connect(source, timeout=0.5)
            self.addCleanup(writer.close)
            self.assertEqual(writer.execute('PRAGMA journal_mode=WAL').fetchone(), ('wal',))
            writer.execute('CREATE TABLE state (id INTEGER PRIMARY KEY, value TEXT)')
            writer.execute("INSERT INTO state VALUES (1, 'captured original value')")
            writer.commit()
            reader = sqlite3.connect(source.as_uri() + '?mode=ro', uri=True)
            self.addCleanup(reader.close)
            reader.execute('PRAGMA query_only=ON')
            reader.execute('BEGIN')
            reader.execute('SELECT 1 FROM sqlite_schema LIMIT 1').fetchall()
            # A real committed update must neither block nor change the pinned point.
            writer.execute("UPDATE state SET value='new live value'")
            writer.commit()
            destination = sqlite3.connect(root / 'restored.db')
            self.addCleanup(destination.close)
            reader.backup(destination)
            self.assertEqual(destination.execute('PRAGMA integrity_check').fetchall(), [('ok',)])
            self.assertEqual(destination.execute('SELECT value FROM state').fetchone(),
                             ('captured original value',))
            self.assertEqual(writer.execute('SELECT value FROM state').fetchone(), ('new live value',))

    def test_producer_uses_native_readonly_wal_snapshot_without_db_quiescence_gate(self):
        repo = Path(__file__).resolve().parents[2]
        text = (repo / 'kubernetes/apps/media/plex/app/capture.sh').read_text()
        self.assertIn("-cmd 'PRAGMA query_only=ON;' -cmd 'BEGIN;'", text)
        self.assertIn("-cmd 'SELECT 1 FROM sqlite_schema LIMIT 1;'", text)
        self.assertIn("'PRAGMA journal_mode;')\" = wal", text)
        self.assertNotIn('db.before', text)
        self.assertNotIn('db.after', text)
        self.assertIn("sqlite='/usr/lib/plexmediaserver/Plex SQLite'", text)
        self.assertIn('state_inventory >"$output/pending/state.after"', text)


if __name__ == '__main__':
    unittest.main()
