"""Exercise the exact embedded hooks using disposable SQLite fixtures.

Run: python3 -m unittest discover -s scripts/tests -p test_kopiur_sqlite_cohort.py
Requires yq, as do the repository's app validators. No live credentials needed.
"""
import ast
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[2]
APPS = ("cwa-bdl", "sabnzbd", "listenarr")


def load_documents(path):
    result = subprocess.run(["yq", "-o=json", "-I=0", ".", str(path)],
                            capture_output=True, text=True, check=True)
    return [json.loads(line) for line in result.stdout.splitlines() if line.strip()]


class CohortTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.policies = {}
        for app in APPS:
            path = REPO / "kubernetes/apps/arrs" / app / "app/kopiur-policy.yaml"
            docs = load_documents(path)
            cls.policies[app] = docs[0]

    def fixture(self, app, root):
        policy = self.policies[app]
        hook = policy["spec"]["hooks"]["beforeSnapshot"][0]
        code = hook["workloadExec"]["command"][2]
        tree = ast.parse(code)
        settings = {node.targets[0].id: ast.literal_eval(node.value)
                    for node in tree.body[:3]}
        code = code.replace(repr(settings["ROOT"]), repr(str(root)), 1)
        for name in settings["REQUIRED_FILES"]:
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('{}' if path.suffix == '.json' else 'fixture-state')
        dbpath = root / settings["DATABASE"]
        dbpath.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(dbpath)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("CREATE TABLE state (id INTEGER PRIMARY KEY, value TEXT)")
        connection.execute("INSERT INTO state VALUES (1, 'committed-wal-state')")
        connection.commit()
        self.addCleanup(connection.close)
        return code, settings, dbpath, connection

    def run_hook(self, code):
        return subprocess.run([sys.executable, '-c', code], capture_output=True,
                              text=True, timeout=10)

    def test_wal_restore_and_rerun(self):
        for app in APPS:
            with self.subTest(app=app), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                code, settings, source, connection = self.fixture(app, root)
                self.assertTrue(Path(str(source) + '-wal').exists())
                for expected in (1, 2):
                    result = self.run_hook(code)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    backup = root / '.kopiur-consistent/database.sqlite3'
                    manifest = json.loads((backup.parent / 'manifest.json').read_text())
                    self.assertEqual(manifest['database'], settings['DATABASE'])
                    self.assertEqual(manifest['sha256'], hashlib.sha256(backup.read_bytes()).hexdigest())
                    with sqlite3.connect(backup) as restored:
                        self.assertEqual(restored.execute('PRAGMA integrity_check').fetchall(), [('ok',)])
                        self.assertEqual(restored.execute('SELECT count(*) FROM state').fetchone(), (expected,))
                    self.assertEqual(list(backup.parent.glob('*.tmp*')), [])
                    self.assertEqual(os.stat(backup).st_mode & 0o777, 0o600)
                    self.assertEqual(os.stat(backup.parent).st_mode & 0o777, 0o700)
                    if expected == 1:
                        connection.execute("INSERT INTO state VALUES (2, 'new-state')")
                        connection.commit()

    def test_missing_database_fails_without_creating_it(self):
        for app in APPS:
            with self.subTest(app=app), tempfile.TemporaryDirectory() as tmp:
                code, _, path, connection = self.fixture(app, Path(tmp))
                connection.close()
                path.unlink()
                result = self.run_hook(code)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(path.exists())
                self.assertFalse((Path(tmp) / '.kopiur-consistent').exists())

    def test_corrupt_database_preserves_previous_valid_copy(self):
        for app in APPS:
            with self.subTest(app=app), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                code, _, path, connection = self.fixture(app, root)
                self.assertEqual(self.run_hook(code).returncode, 0)
                backup = root / '.kopiur-consistent/database.sqlite3'
                before = backup.read_bytes()
                connection.close()
                path.write_bytes(b'not a SQLite database')
                self.assertNotEqual(self.run_hook(code).returncode, 0)
                self.assertEqual(backup.read_bytes(), before)
                self.assertEqual(list(backup.parent.glob('*.tmp*')), [])

    def test_missing_required_state_fails(self):
        for app in APPS:
            with self.subTest(app=app), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                code, settings, _, _ = self.fixture(app, root)
                (root / settings['REQUIRED_FILES'][0]).unlink()
                self.assertNotEqual(self.run_hook(code).returncode, 0)
                self.assertFalse((root / '.kopiur-consistent').exists())

    def test_symlink_backup_directory_fails(self):
        for app in APPS:
            with self.subTest(app=app), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                code, _, _, _ = self.fixture(app, root)
                target = root / 'do-not-write'
                target.mkdir()
                (root / '.kopiur-consistent').symlink_to(target, target_is_directory=True)
                self.assertNotEqual(self.run_hook(code).returncode, 0)
                self.assertEqual(list(target.iterdir()), [])

    def test_bad_json_fails(self):
        for app in ('cwa-bdl', 'listenarr'):
            with self.subTest(app=app), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                code, settings, _, _ = self.fixture(app, root)
                name = next(n for n in settings['REQUIRED_FILES'] if n.endswith('.json'))
                (root / name).write_text('{broken')
                self.assertNotEqual(self.run_hook(code).returncode, 0)
                self.assertFalse((root / '.kopiur-consistent').exists())

    def test_policies_keep_independent_backups_and_fail_closed(self):
        for app in APPS:
            with self.subTest(app=app):
                appdir = REPO / 'kubernetes/apps/arrs' / app
                docs = load_documents(appdir / 'app/kopiur-policy.yaml')
                policy, schedule, replication = docs
                spec = policy['spec']
                self.assertEqual(spec['repository']['name'], app + '-nas-smb')
                self.assertEqual(spec['sources'][0]['pvc']['name'], app)
                self.assertEqual(spec['copyMethod'], 'Snapshot')
                self.assertEqual(spec['verification']['verifyFilesPercent'], 100)
                self.assertEqual(spec['verification']['successExpr'], 'stats.files > 0 && stats.errors == 0')
                self.assertFalse(spec['hooks']['beforeSnapshot'][0]['workloadExec']['continueOnFailure'])
                self.assertFalse(schedule['spec']['schedule']['runOnCreate'])
                self.assertEqual(replication['spec']['sourceRef']['name'], app + '-nas-smb')
                self.assertEqual(replication['spec']['destinationRef']['name'], app + '-r2')
                resources = load_documents(appdir / 'app/kustomization.yaml')[0]['resources']
                self.assertIn('../../../../templates/volsync', resources)
                secrets = load_documents(appdir / 'app/externalsecret-kopiur.yaml')
                self.assertEqual(secrets[0]['spec']['dataFrom'][0]['extract']['key'], 'kopiur-' + app)
                self.assertEqual(secrets[1]['spec']['dataFrom'][-1]['extract']['key'], 'kopiur-' + app)
                self.assertNotEqual(secrets[0]['spec']['target']['template']['data']['KOPIA_PASSWORD'],
                                    secrets[1]['spec']['target']['template']['data']['KOPIA_PASSWORD'])
                ks = load_documents(appdir / 'ks.yaml')[0]
                self.assertTrue({'kopiur','rook-ceph-cluster','snapshot-controller','external-secrets-secret-store'}
                                <= {d['name'] for d in ks['spec']['dependsOn']})
                self.assertEqual(len(ks['spec']['healthChecks']), 6)


if __name__ == '__main__':
    unittest.main()
