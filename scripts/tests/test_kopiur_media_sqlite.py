"""Real-image SQLite hook fixtures. No cluster, credentials, or application boot.

Requires Docker and yq. Pull the immutable app images before running.
Run: python3 -m unittest discover -s scripts/tests -p test_kopiur_media_sqlite.py -v
"""
import contextlib
import hashlib
import json
import re
import subprocess
import time
import unittest
import uuid

from test_kopiur_sqlite_cohort import load_documents, REPO

APPS = {'kometa': 'config.cache', 'audiobookshelf': 'absdatabase.sqlite'}


def accept_artifact(manifest, attempt, data, database, not_before):
    """The version-2 restore contract, exercised against real hook artifacts."""
    if (manifest['version'] != 2 or manifest['database'] != database
            or not re.fullmatch('[0-9a-f]{32}', manifest['generation'])
            or manifest['artifact'] != 'database-' + manifest['generation'] + '.sqlite3'
            or manifest['integrity_check'] != 'ok'
            or attempt != {'generation': manifest['generation'], 'state': 'complete'}
            or manifest['completed_at'] < not_before
            or manifest['size_bytes'] != len(data)
            or manifest['sha256'] != hashlib.sha256(data).hexdigest()):
        raise ValueError('stale, incomplete, or altered recovery artifact')


class MediaHookTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.apps = {}
        subprocess.run(['docker', 'info', '--format', '{{.OSType}}'], check=True, capture_output=True)
        for app in APPS:
            appdir = REPO / 'kubernetes/apps/media' / app / 'app'
            docs = load_documents(appdir / 'kopiur-policy.yaml')
            image = load_documents(appdir / 'helmrelease.yaml')[0]['spec']['values']['controllers']
            image = next(iter(image.values()))['containers']['app']['image']
            reference = image['repository'] + ':' + image['tag']
            # No mutable replacement image, API stubs, or external sqlite package.
            subprocess.run(['docker', 'image', 'inspect', reference], check=True, capture_output=True)
            cls.apps[app] = (reference, docs[0]['spec']['hooks']['beforeSnapshot'][0]['workloadExec']['command'])

    @contextlib.contextmanager
    def container(self, app):
        name = 'kopiur-fixture-' + uuid.uuid4().hex[:12]
        subprocess.run(['docker', 'run', '-d', '--name', name, '--network', 'none',
                        '--user', '568:568', '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges',
                        '--read-only', '--tmpfs', '/config:rw,uid=568,gid=568,mode=0700',
                        '--entrypoint', '/bin/sh', self.apps[app][0], '-c', 'sleep 300'],
                       check=True, capture_output=True)
        try:
            yield name
        finally:
            subprocess.run(['docker', 'rm', '-f', name], check=True, capture_output=True)

    def run_code(self, app, name, code, background=False):
        exe, flag, _ = self.apps[app][1]
        return subprocess.run(['docker', 'exec', *(['-d'] if background else []), name, exe, flag, code],
                              capture_output=True, text=True, timeout=15)

    def fs(self, app, name, action, relative, value=None):
        filename = '/config/' + relative
        if app == 'kometa':
            code = "from pathlib import Path; import json; p=Path(" + repr(filename) + "); "
            operations = {'read': 'print(p.read_bytes().hex())',
                          'write': 'p.write_text(' + repr(value) + ')',
                          'unlink': 'p.unlink()', 'mkdir': 'p.mkdir()', 'rmdir': 'p.rmdir()',
                          'chmod': 'p.chmod(' + repr(value) + ')',
                          'symlink': 'p.symlink_to(' + repr(value) + ')',
                          'mode': 'print(p.stat().st_mode & 0o777)',
                          'exists': 'print(int(p.exists()))',
                          'names': 'print(json.dumps(sorted(x.name for x in p.iterdir())))'}
        else:
            code = 'const fs=require("fs"); const p=' + json.dumps(filename) + '; '
            operations = {'read': 'console.log(fs.readFileSync(p).toString("hex"))',
                          'write': 'fs.writeFileSync(p,' + json.dumps(value) + ')',
                          'unlink': 'fs.unlinkSync(p)', 'mkdir': 'fs.mkdirSync(p)', 'rmdir': 'fs.rmdirSync(p)',
                          'chmod': 'fs.chmodSync(p,' + json.dumps(value) + ')',
                          'symlink': 'fs.symlinkSync(' + json.dumps(value) + ',p)',
                          'mode': 'console.log(fs.statSync(p).mode & 0o777)',
                          'exists': 'console.log(Number(fs.existsSync(p)))',
                          'names': 'console.log(JSON.stringify(fs.readdirSync(p).sort()))'}
        result = self.run_code(app, name, code + operations[action])
        self.assertEqual(result.returncode, 0, result.stderr)
        return bytes.fromhex(result.stdout.strip()) if action == 'read' else result.stdout.strip()

    def seed(self, app, name, wal=True):
        database = '/config/' + APPS[app]
        journal = 'WAL' if wal else 'DELETE'
        sql = ('PRAGMA journal_mode=' + journal + '; CREATE TABLE state(id PRIMARY KEY, value); '
               "INSERT INTO state VALUES(1, 'committed-state');")
        if app == 'kometa':
            code = ('import sqlite3,time; from pathlib import Path; '
                    "Path('/config/config.yml').write_text('settings: {}'); "
                    "Path('/config/UUID').write_text('fixture-uuid'); "
                    'db=sqlite3.connect(' + repr(database) + '); db.executescript(' + repr(sql) + '); '
                    + ('' if wal else 'db.close(); ')
                    + "Path('/config/ready').touch(); time.sleep(240)")
        else:
            code = ('const fs=require("fs"), s=require("/app/node_modules/sqlite3"); '
                    'fs.mkdirSync("/config/metadata"); const db=new s.Database(' + json.dumps(database) + '); '
                    'db.exec(' + json.dumps(sql) + ',e=>{if(e) throw e; '
                    + ('' if wal else 'db.close(); ')
                    + 'fs.writeFileSync("/config/ready","");}); setInterval(()=>{},1000);')
        result = self.run_code(app, name, code, background=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.wait_file(app, name, 'ready')

    def wait_file(self, app, name, filename):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if self.fs(app, name, 'exists', filename) == '1':
                return
            time.sleep(0.1)
        self.fail('fixture process did not become ready')

    def hook(self, app, name, timeout=False):
        code = self.apps[app][1][2]
        # Only the deadline constant changes in timeout tests. Driver calls are real.
        if timeout:
            code = code.replace('DEADLINE_SECONDS = 90', 'DEADLINE_SECONDS = 1')
            code = code.replace('DEADLINE_MS = 90000', 'DEADLINE_MS = 1000')
        return self.run_code(app, name, code)

    def artifact(self, app, name, not_before=0.0):
        manifest = json.loads(self.fs(app, name, 'read', '.kopiur-consistent/manifest.json'))
        attempt = json.loads(self.fs(app, name, 'read', '.kopiur-consistent/attempt.json'))
        data = self.fs(app, name, 'read', '.kopiur-consistent/' + manifest['artifact'])
        assert isinstance(data, bytes)
        accept_artifact(manifest, attempt, data, APPS[app], not_before)
        return manifest, data

    def sql(self, app, name, filename, sql):
        if app == 'kometa':
            code = ('import sqlite3; db=sqlite3.connect(' + repr(filename) + '); '
                    'db.executescript(' + repr(sql) + '); db.close()')
        else:
            code = ('const s=require("/app/node_modules/sqlite3"), db=new s.Database(' + json.dumps(filename) + '); '
                    'db.exec(' + json.dumps(sql) + ',e=>{if(e) throw e; db.close();});')
        result = self.run_code(app, name, code)
        self.assertEqual(result.returncode, 0, result.stderr)

    def counts(self, app, name, filename):
        if app == 'kometa':
            code = ('import sqlite3,json; db=sqlite3.connect(' + repr(filename) + '); '
                    "print(json.dumps([db.execute('PRAGMA integrity_check').fetchone()[0], "
                    "db.execute('SELECT count(*) FROM state').fetchone()[0]]))")
        else:
            code = ('const s=require("/app/node_modules/sqlite3"), db=new s.Database(' + json.dumps(filename) + '); '
                    'db.get("PRAGMA integrity_check",(e,r)=>{if(e) throw e; '
                    'db.get("SELECT count(*) AS n FROM state",(e,n)=>{if(e) throw e; '
                    'console.log(JSON.stringify([r.integrity_check,n.n])); db.close();});});')
        result = self.run_code(app, name, code)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_wal_backup_rerun_permissions_and_stale_rejection(self):
        for app in APPS:
            with self.subTest(app=app), self.container(app) as name:
                self.seed(app, name)
                self.assertEqual(self.fs(app, name, 'exists', APPS[app] + '-wal'), '1')
                first = None
                for expected in range(1, 4):
                    self.sql(app, name, '/config/' + APPS[app],
                             "INSERT OR IGNORE INTO state VALUES(" + str(expected) + ", 'new-committed-wal');")
                    source_before = self.fs(app, name, 'read', APPS[app])
                    wal_before = self.fs(app, name, 'read', APPS[app] + '-wal')
                    started = time.time()
                    result = self.hook(app, name)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    manifest, data = self.artifact(app, name, started)
                    self.assertEqual(self.counts(app, name, '/config/.kopiur-consistent/' + manifest['artifact']), ['ok', expected])
                    self.assertEqual(self.fs(app, name, 'read', APPS[app]), source_before)
                    self.assertEqual(self.fs(app, name, 'read', APPS[app] + '-wal'), wal_before)
                    self.assertEqual(self.fs(app, name, 'mode', '.kopiur-consistent/' + manifest['artifact']), str(0o600))
                    self.assertEqual(self.fs(app, name, 'mode', '.kopiur-consistent'), str(0o700))
                    attempt = {'generation': manifest['generation'], 'state': 'complete'}
                    with self.assertRaises(ValueError):
                        accept_artifact(manifest, attempt, data, APPS[app], manifest['completed_at'] + 1)
                    with self.assertRaises(ValueError):
                        accept_artifact(manifest, attempt, data + b'x', APPS[app], 0)
                    altered = bytes([data[0] ^ 1]) + data[1:]
                    self.assertEqual(len(altered), len(data))
                    with self.assertRaises(ValueError):
                        accept_artifact(manifest, attempt, altered, APPS[app], 0)
                    if first:
                        with self.assertRaises(ValueError):
                            accept_artifact(first[0], attempt, first[1], APPS[app], 0)
                    first = manifest, data
                names = json.loads(self.fs(app, name, 'names', '.kopiur-consistent'))
                self.assertEqual(len([x for x in names if x.endswith('.sqlite3')]), 2)

    def test_multistep_backup_completes_before_publication(self):
        for app in APPS:
            with self.subTest(app=app), self.container(app) as name:
                self.seed(app, name)
                self.sql(app, name, '/config/' + APPS[app],
                         'DELETE FROM state; WITH RECURSIVE n(x) AS '
                         '(SELECT 1 UNION ALL SELECT x+1 FROM n WHERE x<400) '
                         'INSERT INTO state SELECT x, randomblob(4000) FROM n;')
                result = self.hook(app, name)
                self.assertEqual(result.returncode, 0, result.stderr)
                manifest, _ = self.artifact(app, name)
                self.assertGreater(manifest['size_bytes'], 256 * 4096)
                self.assertEqual(self.counts(app, name, '/config/.kopiur-consistent/' + manifest['artifact']),
                                 ['ok', 400])

    def test_missing_required_state_invalidates_previous_artifact(self):
        for app in APPS:
            with self.subTest(app=app), self.container(app) as name:
                self.seed(app, name, wal=False)
                self.assertEqual(self.hook(app, name).returncode, 0)
                previous, data = self.artifact(app, name)
                # Database deletion must not result in implicit empty-DB creation.
                self.fs(app, name, 'unlink', APPS[app])
                self.assertNotEqual(self.hook(app, name).returncode, 0)
                self.assertEqual(self.fs(app, name, 'exists', APPS[app]), '0')
                self.assertEqual(self.fs(app, name, 'read', '.kopiur-consistent/' + previous['artifact']), data)
                with self.assertRaises(ValueError):
                    self.artifact(app, name)

    def test_unreadable_database_async_open_error_rejects_stale_copy(self):
        for app in APPS:
            with self.subTest(app=app), self.container(app) as name:
                self.seed(app, name, wal=False)
                self.assertEqual(self.hook(app, name).returncode, 0)
                previous, data = self.artifact(app, name)
                self.fs(app, name, 'chmod', APPS[app], 0)
                self.assertNotEqual(self.hook(app, name).returncode, 0)
                self.assertEqual(self.fs(app, name, 'read', '.kopiur-consistent/' + previous['artifact']), data)
                with self.assertRaises(ValueError):
                    self.artifact(app, name)

    def test_missing_companion_state_fails_closed(self):
        for app, required in [('kometa', 'UUID'), ('kometa', 'config.yml'), ('audiobookshelf', 'metadata')]:
            with self.subTest(app=app, required=required), self.container(app) as name:
                self.seed(app, name, wal=False)
                self.assertEqual(self.hook(app, name).returncode, 0)
                self.fs(app, name, 'rmdir' if required == 'metadata' else 'unlink', required)
                self.assertNotEqual(self.hook(app, name).returncode, 0)
                with self.assertRaises(ValueError):
                    self.artifact(app, name)

    def test_corrupt_sqlite_async_error_fails_closed(self):
        for app in APPS:
            with self.subTest(app=app), self.container(app) as name:
                self.seed(app, name, wal=False)
                self.assertEqual(self.hook(app, name).returncode, 0)
                previous, data = self.artifact(app, name)
                self.fs(app, name, 'write', APPS[app], 'not a SQLite database')
                result = self.hook(app, name)
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertEqual(self.fs(app, name, 'read', '.kopiur-consistent/' + previous['artifact']), data)
                with self.assertRaises(ValueError):
                    self.artifact(app, name)

    def test_real_exclusive_lock_hits_deadline_and_rejects_stale_artifact(self):
        for app in APPS:
            with self.subTest(app=app), self.container(app) as name:
                self.seed(app, name, wal=False)
                self.assertEqual(self.hook(app, name).returncode, 0)
                previous, data = self.artifact(app, name)
                database = '/config/' + APPS[app]
                if app == 'kometa':
                    code = ('import sqlite3,time; from pathlib import Path; db=sqlite3.connect(' + repr(database) + '); '
                            'db.execute("BEGIN EXCLUSIVE"); Path("/config/locked").touch(); time.sleep(240)')
                else:
                    code = ('const fs=require("fs"),s=require("/app/node_modules/sqlite3"); '
                            'const db=new s.Database(' + json.dumps(database) + '); '
                            'db.exec("BEGIN EXCLUSIVE",e=>{if(e) throw e; fs.writeFileSync("/config/locked","");}); '
                            'setInterval(()=>{},1000);')
                self.assertEqual(self.run_code(app, name, code, background=True).returncode, 0)
                self.wait_file(app, name, 'locked')
                started = time.monotonic()
                result = self.hook(app, name, timeout=True)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('deadline exceeded', result.stderr)
                self.assertLess(time.monotonic() - started, 8)
                self.assertEqual(self.fs(app, name, 'read', '.kopiur-consistent/' + previous['artifact']), data)
                with self.assertRaises(ValueError):
                    self.artifact(app, name)

    def test_symlink_source_and_artifact_directory_rejected(self):
        for app in APPS:
            for target in (APPS[app], '.kopiur-consistent'):
                with self.subTest(app=app, target=target), self.container(app) as name:
                    self.seed(app, name, wal=False)
                    if target == APPS[app]:
                        self.fs(app, name, 'unlink', target)
                        self.fs(app, name, 'write', 'untouched', 'sentinel')
                    else:
                        self.fs(app, name, 'mkdir', 'untouched')
                    self.fs(app, name, 'symlink', target, '/config/untouched')
                    self.assertNotEqual(self.hook(app, name).returncode, 0)
                    if target == APPS[app]:
                        self.assertEqual(self.fs(app, name, 'read', 'untouched'), b'sentinel')
                    else:
                        self.assertEqual(json.loads(self.fs(app, name, 'names', 'untouched')), [])


class MediaPolicyTests(unittest.TestCase):
    def test_policy_secrets_dependencies_and_all_mover_identities(self):
        for app in APPS:
            with self.subTest(app=app):
                appdir = REPO / 'kubernetes/apps/media' / app
                policy, schedule, replication = load_documents(appdir / 'app/kopiur-policy.yaml')
                spec = policy['spec']
                self.assertEqual(spec['repository']['name'], app + '-nas-smb')
                self.assertEqual(spec['identity'], {'username': app, 'hostname': 'media'})
                self.assertEqual(spec['sources'][0]['pvc']['name'], app)
                self.assertEqual(spec['copyMethod'], 'Snapshot')
                self.assertEqual(spec['verification']['verifyFilesPercent'], 100)
                self.assertEqual(spec['verification']['successExpr'], 'stats.errors == 0 && stats.files > 0')
                hook = spec['hooks']['beforeSnapshot'][0]['workloadExec']
                self.assertFalse(hook['continueOnFailure'])
                self.assertEqual(hook['timeout'], '2m')
                self.assertFalse(schedule['spec']['schedule']['runOnCreate'])
                self.assertEqual(replication['spec']['sourceRef']['name'], app + '-nas-smb')
                self.assertEqual(replication['spec']['destinationRef']['name'], app + '-r2')
                repos = load_documents(appdir / 'app/kopiur-repositories.yaml')
                for mover in [spec['mover'], *(r['spec']['moverDefaults'] for r in repos)]:
                    self.assertEqual(mover['securityContext']['capabilities'], {'drop': ['ALL']})
                    self.assertFalse(mover['securityContext']['allowPrivilegeEscalation'])
                    self.assertTrue(mover['podSecurityContext']['runAsNonRoot'])
                    for field in ('runAsUser', 'runAsGroup', 'fsGroup'):
                        self.assertEqual(mover['podSecurityContext'][field], 568)
                resources = load_documents(appdir / 'app/kustomization.yaml')[0]['resources']
                self.assertIn('../../../../templates/volsync', resources)
                secrets = load_documents(appdir / 'app/externalsecret-kopiur.yaml')
                for secret in secrets:
                    self.assertEqual(secret['spec']['dataFrom'][-1]['extract']['key'], 'kopiur-' + app)
                    self.assertEqual(secret['spec']['secretStoreRef']['name'], 'op-secret-store')
                self.assertNotEqual(secrets[0]['spec']['target']['template']['data']['KOPIA_PASSWORD'],
                                    secrets[1]['spec']['target']['template']['data']['KOPIA_PASSWORD'])
                ks = load_documents(appdir / 'ks.yaml')[0]
                self.assertTrue({'kopiur', 'rook-ceph-cluster', 'snapshot-controller', 'external-secrets-secret-store'}
                                <= {d['name'] for d in ks['spec']['dependsOn']})
                self.assertEqual(len(ks['spec']['healthChecks']), 6)


if __name__ == '__main__':
    unittest.main()
