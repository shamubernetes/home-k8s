#!/usr/bin/env python3
"""Cohort C deterministic unit and opt-in native image recovery tests."""
import contextlib
import hashlib
import io
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from unittest import mock
import uuid

ROOT = Path(__file__).resolve().parents[1]
APPS = {'plex': 'media', 'stash': 'media', 'tubearchivist': 'media', 'romm': 'games'}


def appdir(app):
    return ROOT / 'kubernetes/apps' / APPS[app] / app / 'app'


def module(app):
    spec = importlib.util.spec_from_file_location(app, appdir(app) / 'capture.py')
    assert spec is not None and spec.loader is not None
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def db(path):
    connection = sqlite3.connect(path)
    connection.execute('PRAGMA journal_mode=WAL')
    connection.execute('CREATE TABLE state (id INTEGER PRIMARY KEY, value TEXT)')
    connection.executemany('INSERT INTO state VALUES (?,?)', [(i, str(i)) for i in range(50)])
    connection.commit()
    return connection


def command(args, **kwargs):
    result = subprocess.run(args, text=True, capture_output=True, **kwargs)
    if result.returncode:
        raise AssertionError('command failed: ' + ' '.join(args[:3]) + '\n' + result.stderr[-2000:])
    return result.stdout


class Contracts(unittest.TestCase):
    def test_every_render_and_daily_suspended_contract(self):
        for app in APPS:
            with self.subTest(app=app):
                rendered = command(['kubectl', 'kustomize', str(appdir(app))])
                docs = json.loads(command(['yq', '-o=json', 'ea', '[.]'], input=rendered))
                policies = [d for d in docs if d.get('kind') == 'SnapshotPolicy']
                self.assertEqual(len(policies), 1)
                policy = policies[0]['spec']
                self.assertTrue(policy['suspend'])
                self.assertEqual(policy['copyMethod'], 'Snapshot')
                job = policy['hooks']['beforeSnapshot'][0]['runJob']['jobSpec']
                self.assertEqual(job['backoffLimit'], 0)
                self.assertEqual(job['activeDeadlineSeconds'], 660)
                self.assertFalse(job['template']['spec']['automountServiceAccountToken'])
                self.assertNotIn('fsGroup', job['template']['spec']['securityContext'])
                expected = ['stash', 'stash-data'] if app == 'stash' else [app]
                self.assertEqual([s['pvc']['name'] for s in policy['sources']], expected)
                if app == 'stash':
                    self.assertEqual(policy['groupBy'], 'None')
                for d in docs:
                    if d.get('kind') in ['Repository', 'SnapshotReplication']:
                        self.assertTrue(d['spec']['suspend'])
                    if d.get('kind') == 'SnapshotSchedule':
                        self.assertEqual(d['spec']['schedule']['cron'].split()[2:], ['*', '*', '*'])
                        self.assertNotIn('*', d['spec']['schedule']['cron'].split()[:2])
                        self.assertFalse(d['spec']['schedule']['runOnCreate'])
                    if d.get('kind') == 'ExternalSecret' and '-kopiur-' in d['metadata']['name']:
                        self.assertEqual(d['spec']['dataFrom'], [{'extract': {'key': 'kopiur-' + app}}])

    def test_source_podtemplates_unchanged(self):
        for app in APPS:
            path = appdir(app) / 'helmrelease.yaml'
            original = command(['git', 'show', '200db1bee:' + str(path.relative_to(ROOT))], cwd=ROOT)
            self.assertEqual(path.read_text(), original)


class StashNativeSQLite(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='k92-media-', dir=os.environ.get('TMPDIR'))
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.paths = [self.root / p for p in ['config', 'data', 'plugins', 'scrapers']]
        for p in self.paths:
            p.mkdir()
        for name in ['blobs', 'metadata']:
            (self.paths[1] / name).mkdir()
            (self.paths[1] / name / 'fixture').write_text(name)
        (self.paths[0] / 'config.yml').write_text('fixture: true\n')
        self.live = db(self.paths[0] / 'stash-go.sqlite')
        self.addCleanup(self.live.close)
        self.helper = module('stash')

    def test_live_wal_native_copy_and_full_state(self):
        self.helper.capture(*self.paths)
        bundle = self.paths[1] / '.kopiur-native/current'
        with contextlib.closing(sqlite3.connect(bundle / 'stash-go.sqlite')) as restored:
            self.assertEqual(restored.execute('PRAGMA integrity_check').fetchall(), [('ok',)])
            self.assertEqual(restored.execute('SELECT count(*) FROM state').fetchone()[0], 50)
        checksums = json.loads((bundle / 'checksums.json').read_text())
        for name, digest in checksums.items():
            self.assertEqual(hashlib.sha256((bundle / name).read_bytes()).hexdigest(), digest)
        self.live.execute('INSERT INTO state VALUES (51,?)', (str(uuid.uuid4()),))
        self.live.commit()
        self.assertEqual(self.live.execute('SELECT count(*) FROM state').fetchone()[0], 51)

    def test_failed_retry_invalidates_previous_complete(self):
        self.helper.capture(*self.paths)
        (self.paths[0] / 'config.yml').unlink()
        with self.assertRaises(ValueError):
            self.helper.capture(*self.paths)
        self.assertFalse((self.paths[1] / '.kopiur-native/COMPLETE').exists())
        self.assertTrue((self.paths[1] / '.kopiur-native/current/stash-go.sqlite').is_file())

    def test_symlink_required_state_rejected(self):
        (self.paths[1] / 'blobs/private').symlink_to('/etc/passwd')
        with self.assertRaises(ValueError):
            self.helper.capture(*self.paths)

    def test_file_mutation_rejected_without_stopping_writer(self):
        original = self.helper.backup_sqlite
        def mutate(source, destination):
            original(source, destination)
            (self.paths[1] / 'blobs/fixture').write_text('changed')
        with mock.patch.object(self.helper, 'backup_sqlite', mutate), self.assertRaises(ValueError):
            self.helper.capture(*self.paths)
        self.assertFalse((self.paths[1] / '.kopiur-native/COMPLETE').exists())


class TubeNativePIT(unittest.TestCase):
    def setUp(self):
        self.helper = module('tubearchivist')
        self.tmp = tempfile.TemporaryDirectory(prefix='k92-media-', dir=os.environ.get('TMPDIR'))
        self.addCleanup(self.tmp.cleanup)
        self.pending = Path(self.tmp.name)
        self.calls = []
        self.page = 0
        self.variant = None

    def request(self, method, path, body=None):
        self.calls.append((method, path))
        if path.endswith('/_mapping'):
            return {'ta_config': {'mappings': {}}, 'ta_video': {'mappings': {}}}
        if path.endswith('/_alias') or path.endswith('/_settings'):
            return {}
        if '/_pit?' in path:
            return {'id': 'fixture-pit'}
        if method == 'DELETE':
            return {'succeeded': True}
        self.page += 1
        hits = [] if self.page > 1 else [{'_index': 'ta_config', '_id': 'fixture', '_source': {'enabled': True}, 'sort': [1]}]
        if self.variant == 'duplicate' and hits:
            hits *= 2
        return {'_shards': {'failed': 1 if self.variant == 'shards' else 0, 'successful': 2, 'total': 2},
                'hits': {'total': {'value': 2 if self.variant == 'short' else 1, 'relation': 'eq'}, 'hits': hits}}

    def test_native_pit_bulk_payload_and_cleanup(self):
        result = self.helper.export_es(self.request, self.pending)
        self.assertEqual(result['documents'], 1)
        lines = (self.pending / 'elasticsearch.ndjson').read_text().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertEqual(json.loads(lines[0])['index']['_index'], 'ta_config')
        self.assertEqual(self.calls[-1], ('DELETE', '/_pit'))

    def test_partial_shards_rejected(self):
        self.variant = 'shards'
        with self.assertRaises(ValueError):
            self.helper.export_es(self.request, self.pending)
        self.assertEqual(self.calls[-1], ('DELETE', '/_pit'))

    def test_duplicate_documents_rejected(self):
        self.variant = 'duplicate'
        with self.assertRaises(ValueError):
            self.helper.export_es(self.request, self.pending)

    def test_truncated_export_rejected(self):
        self.variant = 'short'
        with self.assertRaises(ValueError):
            self.helper.export_es(self.request, self.pending)

    def test_http_failure_and_error_payload_rejected(self):
        for payload in [{'error': 'redacted'}, {'timed_out': True}, {'errors': True}]:
            with self.assertRaises(ValueError):
                self.helper.checked(payload)

    def test_private_cache_symlink_rejected(self):
        for name in ['ytdlp', 'download', 'import']:
            (self.pending / name).mkdir()
        (self.pending / 'ytdlp/cookies').symlink_to('/etc/passwd')
        with self.assertRaises(ValueError):
            self.helper.cache_identity(self.pending)

    def test_sqlite_and_pit_complete_bundle_checksums(self):
        for name in ['ytdlp', 'download', 'import']:
            (self.pending / name).mkdir()
        connection = db(self.pending / 'db.sqlite3')
        connection.close()
        runtime = self.pending / 'escrow.json'
        runtime.write_text(json.dumps({'TA_USERNAME': str(uuid.uuid4()), 'TA_PASSWORD': str(uuid.uuid4())}))
        class Opener:
            def open(inner, request, timeout):
                path = request.full_url.removeprefix('http://elasticsearch.database.svc.cluster.local:9200')
                body = None if request.data is None else json.loads(request.data)
                return contextlib.closing(io.BytesIO(json.dumps(self.request(request.method, path, body)).encode()))
        with mock.patch.object(self.helper.urllib.request, 'build_opener', return_value=Opener()), mock.patch.dict(os.environ, {'ES_CAPTURE_USER': str(uuid.uuid4()), 'ES_CAPTURE_PASSWORD': str(uuid.uuid4())}):
            self.helper.capture(self.pending, runtime)
        bundle = self.pending / '.kopiur-native/current'
        checksums = json.loads((bundle / 'checksums.json').read_text())
        self.assertIn('runtime.json', checksums)
        self.assertIn('db.sqlite3', checksums)
        for name, digest in checksums.items():
            self.assertEqual(hashlib.sha256((bundle / name).read_bytes()).hexdigest(), digest)
        self.assertEqual((self.pending / '.kopiur-native/COMPLETE').read_text(), 'complete\n')


@unittest.skipUnless(os.environ.get('K92_MEDIA_NATIVE_DOCKER') == '1', 'set K92_MEDIA_NATIVE_DOCKER=1 for pinned image tests')
class NativeImages(unittest.TestCase):
    def test_plex_actual_sqlite_and_independent_native_read(self):
        image = 'ghcr.io/home-operations/plex:1.43.4.10903@sha256:fdc6fe82bc7d8acd08fc0aa581fcb76722803ba5905e2537135a697347dd6149'
        with tempfile.TemporaryDirectory(prefix='k92-plex-', dir=os.environ.get('TMPDIR')) as tmp:
            root = Path(tmp)
            (root / 'Plug-in Support/Databases').mkdir(parents=True)
            (root / 'Preferences.xml').write_text('<Preferences/>')
            for name in ['com.plexapp.plugins.library.db', 'com.plexapp.plugins.library.blobs.db']:
                connection = db(root / 'Plug-in Support/Databases' / name)
                connection.close()
            command(['docker', 'run', '--rm', '--network=none', '--user=568:568', '--entrypoint=/bin/bash',
                     '-v', str(root) + ':/config', '-v', str(appdir('plex') / 'capture.sh') + ':/capture.sh:ro',
                     image, '/capture.sh'], timeout=360)
            output = command(['docker', 'run', '--rm', '--network=none', '--entrypoint=/usr/lib/plexmediaserver/Plex SQLite',
                     '-v', str(root / '.kopiur-native/current') + ':/restore:ro', image,
                     '-readonly', '/restore/com.plexapp.plugins.library.db', 'PRAGMA integrity_check; SELECT count(*) FROM state;'], timeout=60)
            self.assertEqual(output.strip().splitlines(), ['ok', '50'])
            self.assertTrue((root / '.kopiur-native/COMPLETE').exists())

    def test_romm_native_pg17_capture_and_restore(self):
        image = 'docker.io/library/postgres:17@sha256:2d2b8998d31037bf721cfdf764d76ba74171b4fab3431b7f72c27c56ddbdf9e3'
        name = 'k92-media-pg-' + uuid.uuid4().hex[:12]
        command(['docker', 'run', '-d', '--name', name, '--network=none', '-e', 'POSTGRES_HOST_AUTH_METHOD=trust', image], timeout=180)
        try:
            for _ in range(90):
                probe = subprocess.run(['docker', 'exec', name, 'pg_isready', '-U', 'postgres'], capture_output=True)
                if probe.returncode == 0:
                    break
                import time
                time.sleep(1)
            for sql in ['CREATE ROLE capture LOGIN', 'CREATE DATABASE romm OWNER capture', 'CREATE DATABASE recovered OWNER capture']:
                command(['docker', 'exec', '-u', 'postgres', name, 'psql', '-v', 'ON_ERROR_STOP=1', '-c', sql])
            command(['docker', 'exec', '-u', 'postgres', name, 'psql', '-U', 'capture', '-d', 'romm', '-v', 'ON_ERROR_STOP=1', '-c',
                     'CREATE TABLE state AS SELECT generate_series(1,50) AS id'])
            command(['docker', 'exec', name, 'sh', '-c', 'mkdir /config /runtime /kopiur /kopiur-postgres; chown 568:568 /config; printf "fixture: true\\n" >/runtime/config.yml; printf fixture >/runtime/ROMM_AUTH_SECRET_KEY'])
            command(['docker', 'cp', str(ROOT / 'kubernetes/components/kopiur-postgres/capture.sh'), name + ':/kopiur-postgres/capture.sh'])
            command(['docker', 'cp', str(appdir('romm') / 'capture.sh'), name + ':/kopiur/capture.sh'])
            args = ['docker', 'exec', '-u', '568:568']
            for value in ['PGHOST=/var/run/postgresql', 'PGUSER=capture', 'PGPASSWORD=' + uuid.uuid4().hex, 'PGDATABASES=romm',
                          'CAPTURE_MODE=single-db-stable-filetree', 'CONFIG_FILE=/runtime/config.yml']:
                args += ['-e', value]
            command(args + [name, 'bash', '/kopiur/capture.sh'], timeout=120)
            command(['docker', 'exec', name, 'pg_restore', '-U', 'capture', '-d', 'recovered', '--no-owner', '--no-privileges',
                     '--exit-on-error', '/config/.kopiur-postgres/current/romm.dump'])
            result = command(['docker', 'exec', name, 'psql', '-U', 'capture', '-d', 'recovered', '-Atc', 'SELECT count(*) FROM state'])
            self.assertEqual(result.strip(), '50')
            command(['docker', 'exec', name, 'sh', '-c', 'test -f /config/.kopiur-postgres/COMPLETE; cd /config/.kopiur-postgres/current; sha256sum -c SHA256SUMS'])
        finally:
            command(['docker', 'rm', '-f', name])


if __name__ == '__main__':
    unittest.main(verbosity=2)
