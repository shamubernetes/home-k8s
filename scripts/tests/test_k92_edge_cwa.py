import importlib.util
from pathlib import Path
import sqlite3
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
PATH = ROOT / 'kubernetes/apps/media/calibre-web-automated/recovery/online-sqlite.py'
spec = importlib.util.spec_from_file_location('cwa_edge', PATH)
assert spec and spec.loader
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class OnlineSQLite(unittest.TestCase):
    def setup_tree(self, root):
        for name in module.DATABASES:
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            with sqlite3.connect(path) as db:
                db.execute('PRAGMA journal_mode=WAL')
                db.execute('CREATE TABLE sentinel(value)')
                db.execute('INSERT INTO sentinel VALUES (42)')

    def test_real_online_backup_and_restore_with_open_writer(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'config'
            self.setup_tree(root)
            writer = sqlite3.connect(root / 'app.db')
            writer.execute('BEGIN IMMEDIATE')
            writer.execute('INSERT INTO sentinel VALUES (99)')
            try:
                manifest = module.capture(root, Path(tmp) / 'generation')
                self.assertEqual(manifest['consistency'], 'per-database-online')
                self.assertFalse(manifest['library_bytes_captured'])
                self.assertEqual(len(manifest['databases']), 5)
                with sqlite3.connect(Path(tmp) / 'generation/app.db') as restored:
                    self.assertEqual(restored.execute('SELECT * FROM sentinel').fetchall(), [(42,)])
                writer.commit()
                self.assertEqual(writer.execute('SELECT count(*) FROM sentinel').fetchone(), (2,))
            finally:
                writer.close()

    def test_missing_database_never_publishes_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'config'
            self.setup_tree(root)
            (root / 'gdrive.db').unlink()
            target = Path(tmp) / 'generation'
            with self.assertRaises(FileNotFoundError):
                module.capture(root, target)
            self.assertFalse((target / 'manifest.json').exists())

    def test_symlink_and_reused_generation_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'config'
            self.setup_tree(root)
            path = root / 'app.db'
            path.rename(root / 'other.db')
            path.symlink_to(root / 'other.db')
            target = Path(tmp) / 'generation'
            with self.assertRaises(ValueError):
                module.capture(root, target)
            with self.assertRaises(ValueError):
                module.capture(root, target)


if __name__ == '__main__':
    unittest.main()
