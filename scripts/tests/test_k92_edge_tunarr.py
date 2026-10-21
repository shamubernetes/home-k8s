import importlib.util
from pathlib import Path
import unittest
import tempfile
import sqlite3

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('tunarr_edge', ROOT / 'kubernetes/apps/media/tunarr/backups/render-kopiur-rollback.py')
assert spec and spec.loader
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class RollbackOnly(unittest.TestCase):
    def repository(self):
        return {'kind': 'Repository', 'metadata': {'namespace': 'media', 'name': 'fixture-only', 'generation': 3}, 'spec': {'backend': {'rclone': {}}}, 'status': {'conditions': [{'type': 'Ready', 'status': 'True', 'observedGeneration': 3}]}}

    def test_no_application_or_native_process_hooks_and_daily_only(self):
        result = module.render(self.repository())
        policy, schedule = result['items']
        self.assertEqual([r['kind'] for r in result['items']], ['SnapshotPolicy', 'SnapshotSchedule'])
        self.assertEqual(policy['spec']['sources'][0]['pvc']['name'], 'tunarr')
        self.assertNotIn('hooks', policy['spec'])
        self.assertEqual(policy['spec']['defaultDeletionPolicy'], 'Retain')
        self.assertTrue(policy['spec']['suspend'])
        self.assertTrue(schedule['spec']['schedule']['suspend'])
        self.assertEqual(schedule['spec']['schedule']['cron'], '43 2 * * *')
        self.assertFalse(schedule['spec']['schedule']['runOnCreate'])

    def test_missing_or_stale_repository_cannot_be_rendered(self):
        repo = self.repository()
        repo['status']['conditions'][0]['observedGeneration'] = 2
        with self.assertRaises(ValueError):
            module.render(repo)
        repo = self.repository()
        repo['metadata']['namespace'] = 'arrs'
        with self.assertRaises(ValueError):
            module.render(repo)

    def test_restored_sqlite_without_starting_application(self):
        check_spec = importlib.util.spec_from_file_location('tunarr_restore_edge', ROOT / 'kubernetes/apps/media/tunarr/backups/check-restored-sqlite.py')
        assert check_spec and check_spec.loader
        checker = importlib.util.module_from_spec(check_spec)
        check_spec.loader.exec_module(checker)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.assertRaises(ValueError):
                checker.check(root)
            with sqlite3.connect(root / 'tunarr.db') as db:
                db.execute('CREATE TABLE channels(id)')
                db.execute('INSERT INTO channels VALUES (1)')
            result = checker.check(root)
            self.assertEqual(result['sqlite_databases'][0]['integrity_check'], 'ok')
            self.assertFalse(result['application_started'])
            self.assertFalse(result['meilisearch_verified'])

    def test_existing_frozen_root_is_preserved(self):
        source = (ROOT / 'kubernetes/apps/media/tunarr/ks.yaml').read_text()
        self.assertIn('suspend: true', source)
        self.assertIn('kustomize.toolkit.fluxcd.io/reconcile: disabled', source)


if __name__ == '__main__':
    unittest.main()
