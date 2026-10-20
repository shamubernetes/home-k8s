#!/usr/bin/env python3
"""C source regressions. Models and native fixtures are NOT original qualification."""
import ast
import contextlib
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest import mock
import uuid

from test_k92_acceleration_media import (Contracts, NativeImages, StashNativeSQLite,
    TubeNativePIT, appdir, command, require_fixture_running)


def recovery(app):
    spec = importlib.util.spec_from_file_location(app + '_restore', appdir(app) / 'restore.py')
    assert spec is not None and spec.loader is not None
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


class StashIntegration(StashNativeSQLite):
    def test_root_mode_change_invalidates_bundle(self):
        original = self.helper.backup_sqlite
        def mutate(source, target):
            original(source, target)
            (self.paths[1] / 'blobs').chmod(0o700)
        (self.paths[1] / 'blobs').chmod(0o755)
        with mock.patch.object(self.helper, 'backup_sqlite', mutate), self.assertRaises(ValueError):
            self.helper.capture(*self.paths)
        self.assertFalse((self.paths[1] / '.kopiur-native/COMPLETE').exists())

    def test_complete_authoritative_bundle_independent_restore(self):
        self.helper.capture(*self.paths)
        targets = [self.root / ('recovered-' + p.name) for p in self.paths]
        for target in targets:
            target.mkdir()
        bundle = self.paths[1] / '.kopiur-native/current'
        recovery('stash').restore(bundle, *targets)
        self.assertEqual((targets[0] / 'config.yml').read_bytes(), (self.paths[0] / 'config.yml').read_bytes())
        for name in ['blobs', 'metadata']:
            self.assertEqual((targets[1] / name / 'fixture').read_bytes(), (self.paths[1] / name / 'fixture').read_bytes())
        with contextlib.closing(sqlite3.connect(targets[0] / 'stash-go.sqlite')) as restored:
            self.assertEqual(restored.execute('SELECT * FROM state ORDER BY id').fetchall(), self.live.execute('SELECT * FROM state ORDER BY id').fetchall())
        with self.assertRaises(ValueError):
            recovery('stash').restore(bundle, *targets)

    def test_corruption_fails_before_restore_writes(self):
        self.helper.capture(*self.paths)
        bundle = self.paths[1] / '.kopiur-native/current'
        (bundle / 'config.yml').write_text('corrupted')
        targets = [self.root / ('recovered-' + p.name) for p in self.paths]
        for target in targets:
            target.mkdir()
        with self.assertRaises(ValueError):
            recovery('stash').restore(bundle, *targets)
        self.assertTrue(all(not any(p.iterdir()) for p in targets))


class TubeRestoreModels(TubeNativePIT):
    """Synthetic request responses, explicitly not native Elasticsearch proof."""
    def setUp(self):
        super().setUp()
        self.restorer = recovery('tubearchivist')
        for name, value in {
            'elasticsearch-mappings.json': {'ta_config': {'mappings': {'properties': {'enabled': {'type': 'boolean'}}}}},
            'elasticsearch-settings.json': {'ta_config': {'settings': {'index': {'uuid': 'synthetic', 'version': {'created': '8190000'}, 'number_of_shards': '1'}}}},
            'elasticsearch-aliases.json': {'ta_config': {'aliases': {}}},
            'metadata.json': {'documents': 1},
        }.items():
            (self.pending / name).write_text(json.dumps(value))
        (self.pending / 'elasticsearch.ndjson').write_text('{"index":{"_index":"ta_config","_id":"fixture"}}\n{"enabled":true}\n')
        self.variant = None
        self.puts = []

    def request_restore(self, method, path, body=None):
        if path.startswith('/_cat/'):
            return [{'index': 'existing'}] if self.variant == 'nonempty' else []
        if method == 'PUT':
            self.puts.append(body)
            return {'acknowledged': True}
        if path.startswith('/_bulk'):
            return {'errors': self.variant == 'bulk-error', 'items': [{'index': {'status': 400 if self.variant == 'item-error' else 201, '_index': 'ta_config', '_id': 'fixture'}}]}
        if '/_doc/' in path:
            return {'found': True, '_source': {'enabled': self.variant != 'wrong-source'}}
        if path.endswith('/_alias'):
            return {'ta_config': {'aliases': {}}}
        if path.endswith('/_mapping'):
            return json.loads((self.pending / 'elasticsearch-mappings.json').read_text())
        raise AssertionError('unexpected fixture request')

    def test_catalog_bulk_and_document_equality(self):
        result = self.restorer.restore_es(self.pending, self.request_restore)
        self.assertTrue(result['document_equality'])
        self.assertNotIn('uuid', self.puts[0]['settings']['index'])
        self.assertNotIn('version', self.puts[0]['settings']['index'])

    def test_native_error_item_and_wrong_data_fail(self):
        for variant in ['bulk-error', 'item-error', 'wrong-source', 'nonempty']:
            with self.subTest(variant=variant), self.assertRaises(ValueError):
                self.variant = variant
                self.restorer.restore_es(self.pending, self.request_restore)

    def test_incomplete_catalog_rejected(self):
        (self.pending / 'elasticsearch-settings.json').write_text('{}')
        with self.assertRaises(ValueError):
            self.restorer.restore_es(self.pending, self.request_restore)
        self.assertEqual(self.puts, [])

    def test_truncated_bulk_rejected(self):
        (self.pending / 'elasticsearch.ndjson').write_text('{"index":{"_index":"ta_config","_id":"fixture"}}\n')
        with self.assertRaises(ValueError):
            self.restorer.restore_es(self.pending, self.request_restore)


class SourcePublication(unittest.TestCase):
    def test_embedded_native_python_compiles(self):
        tree = ast.parse(Path(__file__).read_text())
        payloads = [n.value.value for n in ast.walk(tree) if isinstance(n, ast.Assign)
                    and isinstance(n.value, ast.Constant) and isinstance(n.value.value, str)
                    and any(isinstance(t, ast.Name) and t.id in {'capture', 'code', 'ready'} for t in n.targets)]
        self.assertGreaterEqual(len(payloads), 3)
        for payload in payloads:
            compile(payload, '<native-fixture-payload>', 'exec')

    def test_dedicated_backend_refs_and_no_guest_or_shared_bucket(self):
        for app in ['plex', 'stash', 'tubearchivist', 'romm']:
            with self.subTest(app=app):
                secret = (appdir(app) / 'externalsecret-kopiur.yaml').read_text()
                repo = (appdir(app) / 'kopiur-repositories.yaml').read_text()
                self.assertIn("KOPIA_RCLONE_CONFIG: '{{ .NAS_RCLONE_CONFIG }}'", secret)
                self.assertNotIn('user = guest', secret)
                self.assertNotIn('bucket: storage-backup', repo)
                self.assertIn('remotePath: mnemosyne:kopiur-' + app + '\n', repo)
                self.assertIn('bucket: kopiur-' + app + '\n', repo)
                self.assertIn('key: kopiur-' + app + '\n', secret)

    def test_romm_key_precedes_only_completion_publication(self):
        source = (appdir('romm') / 'capture.sh').read_text()
        self.assertNotIn('sh /kopiur-postgres/capture.sh', source)
        self.assertLess(source.index('cp /runtime/ROMM_AUTH_SECRET_KEY'), source.index('pg_dump -w'))
        self.assertLess(source.index('sha256sum -c "$root/pending/runtime.before"'), source.index('mv "$root/pending" "$root/current"'))
        self.assertEqual(source.count('mv "$root/COMPLETE.tmp" "$root/COMPLETE"'), 1)

    def test_plex_inventory_keeps_record_association(self):
        source = (appdir('plex') / 'capture.sh').read_text()
        self.assertNotIn("%P\\0%i\\0", source)
        self.assertIn("%P/%i/%s/%T@/%C@/%m/%U/%G\\0", source)


@unittest.skipUnless(os.environ.get('K92_MEDIA_NATIVE_DOCKER') == '1', 'native fixture requires approved capacity')
class AdditionalNativeImages(unittest.TestCase):
    def test_stash_exact_image_capture_and_independent_bundle_restore(self):
        image = 'ghcr.io/jfroy/stash:v0.28.1-cudajellyfin.4@sha256:0e4b5eb7971ddadc0e482fd9799a5fd621160359ce67da4bdda990ccc67e06c2'
        prefix = 'k92-media-stash-' + uuid.uuid4().hex[:12]
        owned = []
        def graphql(name, query):
            code = "import json,sys,urllib.request; q=urllib.request.Request('http://127.0.0.1:9999/graphql',data=json.dumps({'query':sys.argv[1]}).encode(),headers={'Content-Type':'application/json'}); print(json.dumps(json.load(urllib.request.urlopen(q,timeout=5))))"
            result = json.loads(command(['docker', 'exec', name, 'python3', '-c', code, query], timeout=10))
            if result.get('errors'):
                raise AssertionError('native Stash GraphQL failed')
            return result['data']
        def start(name, paths):
            mounts = []
            for path, mount in zip(paths, ['/config', '/data', '/plugins', '/scrapers']):
                mounts += ['-v', str(path) + ':' + mount]
            mounts += ['-v', str(paths[1] / 'blobs') + ':/blobs', '-v', str(paths[1] / 'metadata') + ':/metadata',
                       '-v', str(passwd) + ':/etc/passwd:ro']
            options = []
            variables = {'HOME': '/config', 'STASH_CONFIG_FILE': '/config/config.yml', 'STASH_DATABASE': '/config/stash-go.sqlite',
                         'STASH_BLOBS_PATH': '/blobs', 'STASH_BLOBS_STORAGE': 'FILESYSTEM', 'STASH_METADATA': '/metadata',
                         'STASH_PLUGINS_PATH': '/plugins', 'STASH_SCRAPERS_PATH': '/scrapers', 'STASH_PORT': '9999',
                         'STASH_CACHE': '/tmp/cache', 'STASH_GENERATED': '/tmp/generated'}
            for key, value in variables.items():
                options += ['-e', key + '=' + value]
            owned.append(name)
            command(['docker', 'run', '-d', '--name', name, '--network=none',
                     '--user=' + str(os.getuid()) + ':' + str(os.getgid()), *mounts, *options,
                     '-v', str(appdir('stash')) + ':/helpers:ro', image], timeout=600)
            for attempt in range(120):
                try:
                    graphql(name, '{ systemStatus { status } }')
                    return
                except (AssertionError, KeyError, json.JSONDecodeError):
                    require_fixture_running(name)
                    if attempt == 119:
                        raise
                    time.sleep(1)
        with tempfile.TemporaryDirectory(prefix=prefix, dir=os.environ.get('TMPDIR')) as temporary:
            root = Path(temporary)
            # Native Go requires the fixture owner's UID in the passwd database.
            passwd = root / 'fixture-passwd'
            accounts = command(['docker', 'run', '--rm', '--network=none',
                                '--entrypoint=/bin/cat', image, '/etc/passwd'], timeout=600)
            if not any(line.split(':')[2] == str(os.getuid())
                       for line in accounts.splitlines() if len(line.split(':')) >= 3):
                accounts += ('\nfixture:x:' + str(os.getuid()) + ':' + str(os.getgid())
                             + ':isolated fixture:/config:/usr/sbin/nologin\n')
            passwd.write_text(accounts)
            paths = [root / p for p in ['config', 'data', 'plugins', 'scrapers']]
            for path in paths:
                path.mkdir()
            for name in ['blobs', 'metadata']:
                (paths[1] / name).mkdir()
            try:
                source = prefix + '-source'
                start(source, paths)
                graphql(source, 'mutation { performerCreate(input:{name:"synthetic-k92-performer"}) { id name } }')
                query = '{ findPerformers(filter:{per_page:-1}) { count performers { id name } } }'
                original = graphql(source, query)
                self.assertEqual(original['findPerformers']['count'], 1)
                (paths[1] / 'blobs/fixture').write_bytes(b'\x00synthetic-native-bytes\xff')
                command(['docker', 'exec', source, 'python3', '/helpers/capture.py'], timeout=600)
                self.assertEqual(graphql(source, query), original)
                targets = [root / ('restored-' + p.name) for p in paths]
                for target in targets:
                    target.mkdir()
                recovery('stash').restore(paths[1] / '.kopiur-native/current', *targets)
                self.assertEqual((targets[1] / 'blobs/fixture').read_bytes(), b'\x00synthetic-native-bytes\xff')
                restored = prefix + '-restored'
                start(restored, targets)
                self.assertEqual(graphql(restored, query), original)
            finally:
                for name in reversed(owned):
                    command(['docker', 'rm', '-fv', name], timeout=60)

    def test_ta_exact_image_real_es_pit_and_native_restore(self):
        es = 'docker.elastic.co/elasticsearch/elasticsearch:8.19.23@sha256:4d0724bb8d78d7a2330693623e26ca4d621079d062ecc8a30b3e1fc180ec14c1'
        ta = 'bbilly1/tubearchivist:v0.5.12@sha256:ba1c846ddd0c6fdd0f040727129d2466e03b7a0c26499223839d450c7586ac09'
        # Pull before starting ES so image unpack does not hide startup failure.
        command(['docker', 'pull', es], timeout=600)
        command(['docker', 'pull', ta], timeout=600)
        prefix = 'k92-media-es-' + uuid.uuid4().hex[:12]
        owned = []
        with tempfile.TemporaryDirectory(prefix=prefix, dir=os.environ.get('TMPDIR')) as temporary:
            root = Path(temporary)
            try:
                # Two fresh ES instances, no production network or shared storage.
                for label in ['source', 'restored']:
                    name = prefix + '-' + label
                    owned.append(name)
                    command(['docker', 'run', '-d', '--name', name, '--network=none', '--hostname=localhost', '--memory=1200m',
                             '-e', 'discovery.type=single-node', '-e', 'xpack.security.enabled=false',
                             '-e', 'ES_JAVA_OPTS=-Xms256m -Xmx256m', es], timeout=600)
                    require_fixture_running(name)
                    client = prefix + '-client-' + label
                    owned.append(client)
                    command(['docker', 'run', '-d', '--name', client, '--network=container:' + name,
                             '--user=' + str(os.getuid()) + ':' + str(os.getgid()),
                             '-v', str(root) + ':/fixture', '-v', str(appdir('tubearchivist')) + ':/helpers:ro',
                             '--entrypoint=sleep', ta, 'infinity'], timeout=600)
                    ready = "import urllib.request; urllib.request.urlopen('http://127.0.0.1:9200/_cluster/health?wait_for_status=yellow&timeout=1s',timeout=3).read()"
                    for attempt in range(120):
                        try:
                            command(['docker', 'exec', client, 'python3', '-c', ready], timeout=10)
                            break
                        except AssertionError:
                            require_fixture_running(name)
                            if attempt == 119:
                                raise
                            time.sleep(2)
                source = prefix + '-client-source'
                # Real ES PIT and exact deployed app Python/SQLite, synthetic model SQL.
                capture = '''import importlib.util,json,os,sqlite3,urllib.request,uuid
from pathlib import Path
r=Path('/fixture/cache'); r.mkdir()
for n in ['ytdlp','download','import']: (r/n).mkdir()
c=sqlite3.connect(r/'db.sqlite3'); c.execute('create table model(id integer primary key, value text)'); c.execute("insert into model values(1,'synthetic')"); c.commit(); c.close()
Path('/fixture/runtime.json').write_text(json.dumps({'TA_USERNAME':uuid.uuid4().hex,'TA_PASSWORD':uuid.uuid4().hex}))
def req(path,body):
 q=urllib.request.Request('http://127.0.0.1:9200'+path,data=json.dumps(body).encode(),method='PUT',headers={'Content-Type':'application/json'})
 return json.load(urllib.request.urlopen(q))
req('/ta_config',{'settings':{'number_of_replicas':0},'mappings':{'properties':{'enabled':{'type':'boolean'}}}})
req('/ta_config/_doc/fixture?refresh=true',{'enabled':True})
os.environ.update(ES_CAPTURE_USER=uuid.uuid4().hex,ES_CAPTURE_PASSWORD=uuid.uuid4().hex)
s=importlib.util.spec_from_file_location('capture','/helpers/capture.py'); m=importlib.util.module_from_spec(s); s.loader.exec_module(m)
m.capture(r,Path('/fixture/runtime.json'),url='http://127.0.0.1:9200')
'''
                command(['docker', 'exec', '-i', source, 'python3', '-'], input=capture, timeout=600)
                (root / 'restored-cache').mkdir()
                output = command(['docker', 'exec', prefix + '-client-restored', 'python3', '/helpers/restore.py', '--isolated',
                                  '--bundle', '/fixture/cache/.kopiur-native/current', '--cache', '/fixture/restored-cache'], timeout=600)
                self.assertTrue(json.loads(output)['document_equality'])
                self.assertEqual((root / 'restored-cache/runtime.json').read_bytes(), (root / 'cache/.kopiur-native/current/runtime.json').read_bytes())
                with contextlib.closing(sqlite3.connect(root / 'restored-cache/db.sqlite3')) as restored:
                    self.assertEqual(restored.execute('SELECT * FROM model').fetchall(), [(1, 'synthetic')])
            finally:
                for name in reversed(owned):
                    command(['docker', 'rm', '-fv', name], timeout=60)


if __name__ == '__main__':
    unittest.main(verbosity=2)
