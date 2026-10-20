"""Execute the SAB hook with real WAL databases and a loopback native-API fixture."""
import hashlib
import http.server
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest

ROOT = Path(__file__).resolve().parents[2]
APP = ROOT / 'kubernetes/apps/arrs/sabnzbd'
SPEC = importlib.util.spec_from_file_location('sab_original', APP / 'qualification/verify-original.py')
assert SPEC is not None and SPEC.loader is not None
VERIFY = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(VERIFY)


class RuntimeAPI:
    """Only synthetic credentials and responses, never a production API."""
    def __init__(self, admin):
        self.misc = {'admin_dir': str(admin), 'api_key': 'synthetic-api-key',
                     'nzb_key': 'synthetic-nzb-key'}
        self.calls = 0
        self.change_on_second = False
        owner = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                owner.calls += 1
                misc = dict(owner.misc)
                if owner.change_on_second and owner.calls > 1:
                    misc['admin_dir'] += '-changed'
                body = json.dumps({'config': {'misc': misc}}).encode()
                self.send_response(200)
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format, *args):
                pass

        self.server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.environment = {'SABNZBD__API_KEY': self.misc['api_key'],
                            'SABNZBD__NZB_KEY': self.misc['nzb_key'],
                            'SABNZBD__PORT': str(self.server.server_port), 'TZ': 'UTC'}

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)


def hook_code():
    result = subprocess.run(['yq', '-o=json', '-I=0', '.', str(APP / 'app/kopiur-policy.yaml')],
                            capture_output=True, text=True, check=True)
    policy = json.loads(result.stdout.splitlines()[0])
    return policy['spec']['hooks']['beforeSnapshot'][0]['workloadExec']['command'][2]


class RuntimeCaptureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.root = self.base / 'config'
        self.external = self.base / 'external'
        self.root.mkdir()
        self.external.mkdir()
        (self.root / 'sabnzbd.ini').write_text('[misc]\napi_key = synthetic-api-key\nnzb_key = synthetic-nzb-key\n')
        self.active = self.database(self.external / 'admin', [(1, 'active-wal'), (2, 'active-two')])
        self.database(self.root / 'admin', [(207, 'stale-config-tree')]).close()
        self.api = RuntimeAPI(self.external / 'admin')
        self.addCleanup(self.api.close)
        self.code = hook_code().replace("ROOT = '/config'", 'ROOT = ' + repr(str(self.root)), 1)
        self.code = self.code.replace("Path('/mnt/media/Downloads/sabnzbd')", 'Path(' + repr(str(self.external)) + ')')
        self.env = dict(os.environ, **self.api.environment)
        self.addCleanup(self.active.close)

    def database(self, directory, rows):
        directory.mkdir()
        connection = sqlite3.connect(directory / 'history1.db')
        connection.execute('PRAGMA journal_mode=WAL')
        connection.execute('CREATE TABLE history(id INTEGER PRIMARY KEY, data TEXT)')
        connection.executemany('INSERT INTO history VALUES(?,?)', rows)
        connection.commit()
        return connection

    def run_hook(self):
        return subprocess.run([sys.executable, '-c', self.code], env=self.env,
                              capture_output=True, text=True, timeout=15)

    def test_captures_effective_wal_not_stale_local_history_and_repeats(self):
        for count in (2, 3):
            result = self.run_hook()
            self.assertEqual(result.returncode, 0, result.stderr)
            stage = self.root / '.kopiur-consistent'
            manifest = json.loads((stage / 'manifest.json').read_text())
            self.assertEqual(manifest['version'], 2)
            with sqlite3.connect(stage / 'database.sqlite3') as restored:
                self.assertEqual(restored.execute('SELECT count(*) FROM history').fetchone()[0], count)
                self.assertEqual(restored.execute('SELECT count(*) FROM history WHERE id=207').fetchone()[0], 0)
            self.assertEqual(VERIFY.inspect(self.root)['rows'], count)
            self.assertNotIn('synthetic-api-key', json.dumps(manifest))
            self.assertNotIn('synthetic-nzb-key', json.dumps(manifest))
            self.assertEqual((stage / 'database.sqlite3').stat().st_mode & 0o777, 0o600)
            self.assertEqual(stage.stat().st_mode & 0o777, 0o700)
            self.assertFalse(list(stage.glob('*.tmp*')))
            if count == 2:
                self.active.execute("INSERT INTO history VALUES(3, 'later-commit')")
                self.active.commit()

    def test_committing_writer_keeps_running_during_online_capture(self):
        stop = threading.Event()
        commits = []
        failures = []

        def writer():
            try:
                with sqlite3.connect(self.external / 'admin/history1.db') as connection:
                    row = 3
                    while not stop.wait(0.002):
                        connection.execute('INSERT INTO history VALUES(?,?)', (row, 'concurrent-commit'))
                        connection.commit()
                        commits.append(row)
                        row += 1
            except Exception as error:
                failures.append(type(error).__name__)

        thread = threading.Thread(target=writer)
        thread.start()
        try:
            self.assertEqual(self.run_hook().returncode, 0)
        finally:
            stop.set()
            thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        self.assertFalse(failures)
        self.assertTrue(commits)
        evidence = VERIFY.inspect(self.root)
        self.assertGreaterEqual(evidence['rows'], 2)
        self.assertLessEqual(evidence['rows'], len(commits) + 2)
        self.assertEqual(self.active.execute('PRAGMA integrity_check').fetchall(), [('ok',)])

    def test_no_api_no_fallback_and_safe_diagnostic(self):
        self.env.pop('SABNZBD__API_KEY')
        result = self.run_hook()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('stage=online-copy class=ValueError line=', result.stderr)
        self.assertNotIn('synthetic', result.stderr)
        self.assertNotIn(str(self.base), result.stderr)
        self.assertFalse((self.root / '.kopiur-consistent').exists())

    def test_unavailable_api_does_not_fall_back_or_print_key(self):
        self.env['SABNZBD__PORT'] = '0'
        result = self.run_hook()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('stage=online-copy class=URLError line=', result.stderr)
        self.assertNotIn('synthetic-api-key', result.stdout + result.stderr)
        self.assertFalse((self.root / '.kopiur-consistent').exists())

    def test_corrupt_external_database_preserves_prior_generation(self):
        self.assertEqual(self.run_hook().returncode, 0)
        stage = self.root / '.kopiur-consistent'
        previous = {name: (stage / name).read_bytes() for name in ('manifest.json', 'database.sqlite3')}
        self.active.close()
        (self.external / 'admin/history1.db').write_bytes(b'corrupt')
        self.assertNotEqual(self.run_hook().returncode, 0)
        self.assertEqual(previous, {name: (stage / name).read_bytes() for name in previous})
        self.assertFalse(list(stage.glob('*.tmp*')))

    def test_effective_key_mismatch_rejected(self):
        self.api.misc['nzb_key'] = 'different-synthetic-key'
        self.assertNotEqual(self.run_hook().returncode, 0)
        self.assertFalse((self.root / '.kopiur-consistent').exists())

    def test_config_change_across_copy_rejected(self):
        self.api.change_on_second = True
        self.assertNotEqual(self.run_hook().returncode, 0)
        self.assertFalse((self.root / '.kopiur-consistent/manifest.json').exists())

    def test_missing_effective_db_never_falls_back(self):
        self.api.misc['admin_dir'] += '-absent'
        self.assertNotEqual(self.run_hook().returncode, 0)
        self.assertFalse((self.root / '.kopiur-consistent').exists())

    def test_symlinked_parent_and_undeclared_storage_rejected(self):
        (self.external / 'alias').symlink_to(self.external / 'admin', target_is_directory=True)
        for path in (self.external / 'alias', self.base / 'undeclared'):
            self.api.misc['admin_dir'] = str(path)
            self.assertNotEqual(self.run_hook().returncode, 0)
        self.assertFalse((self.root / '.kopiur-consistent').exists())

    def test_provenance_binding_and_recovery(self):
        self.assertEqual(self.run_hook().returncode, 0)
        replica = self.base / 'replica'
        shutil.copytree(self.root, replica)
        self.assertTrue(VERIFY.verify(self.root, replica)['nativeFilesystemEquality'])
        manifest_path = replica / '.kopiur-consistent/manifest.json'
        manifest = json.loads(manifest_path.read_text())
        source = self.external / 'admin/history1.db'
        self.assertEqual(manifest['sourceDatabasePathSha256'], hashlib.sha256(str(source).encode()).hexdigest())
        self.assertEqual(manifest['configSha256'], hashlib.sha256((self.root / 'sabnzbd.ini').read_bytes()).hexdigest())
        manifest['sourceDatabasePathSha256'] = 'invalid'
        manifest_path.write_text(json.dumps(manifest))
        with self.assertRaises(ValueError):
            VERIFY.inspect(replica)

    def test_config_generation_mismatch_rejected(self):
        self.assertEqual(self.run_hook().returncode, 0)
        with (self.root / 'sabnzbd.ini').open('a') as handle:
            handle.write('# changed after hook\n')
        with self.assertRaises(ValueError):
            VERIFY.inspect(self.root)


if __name__ == '__main__':
    unittest.main()
