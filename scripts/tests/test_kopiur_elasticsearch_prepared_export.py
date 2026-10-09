"""Prepared original export composition and concrete loopback I/O, ARC only."""
import base64
import copy
import json
import secrets
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from kopiur_elasticsearch_capture import snapshot_metadata, synthetic_snapshot_archive
from kopiur_elasticsearch_escrow import EscrowError, _encoded
from kopiur_elasticsearch_escrow_fixture import configuration_archive
from kopiur_elasticsearch_source import KubernetesSource, LoopbackSnapshotIO, PreparedSnapshotExport
from test_kopiur_elasticsearch_source import fixture
from kopiur_elasticsearch_engine_fixture import fixture_consumer_plan


class LoopbackIOTests(unittest.TestCase):
    def setUp(self):
        self.execute = Mock(return_value=b'{"username":"elastic"}')
        self.io = LoopbackSnapshotIO(exec_read=self.execute, repository='fixture',
            snapshot='generation', location='/usr/share/elasticsearch/data/snapshot')
        self.credentials = {'elastic_username': 'elastic',
                            'elastic_password': secrets.token_hex(24) + '"\\:fixture-only'}

    def test_exact_service_password_only_on_stdin_and_no_redirect_or_proxy(self):
        self.assertEqual(self.io.request('/_security/_authenticate', self.credentials), {'username': 'elastic'})
        args, kwargs = self.execute.call_args
        self.assertEqual(args, ('curl', '-q', '--silent', '--fail', '--max-time', '60',
            '--noproxy', '*', '--proto', '=http', '--config', '-',
            '--url', 'http://127.0.0.1:9200/_security/_authenticate'))
        expected = b'user = "elastic:' + self.credentials['elastic_password'][:48].encode() + b'\\"\\\\:fixture-only"\n'
        self.assertEqual(kwargs['data'], expected)
        self.assertNotIn(self.credentials['elastic_password'], repr(args))

    def test_foreign_write_and_query_paths_denied_without_io(self):
        for path in ('/_snapshot/foreign/generation', '/_snapshot/fixture/generation/_restore',
                     '/_snapshot/fixture/generation?wait_for_completion=true', '//foreign/',
                     'http://example.test/', '/_security/user/elastic', None):
            with self.subTest(path=path), self.assertRaises(EscrowError):
                self.io.request(path, self.credentials)
        self.execute.assert_not_called()

    def test_control_password_wrong_username_and_extra_fields_denied_without_io(self):
        for changes in ({'elastic_password': 'PRIVATE\nurl = "http://foreign"'},
                        {'elastic_username': 'other'}, {'extra': 'PRIVATE'}, {'elastic_password': '\x7f'}):
            with self.subTest(changes=changes), self.assertRaises(EscrowError) as failure:
                self.io.request('/', self.credentials | changes)
            self.assertNotIn('PRIVATE', str(failure.exception))
        self.execute.assert_not_called()

    def test_duplicate_nested_keys_invalid_json_and_wrong_type_are_redacted(self):
        for raw in (b'{"key":{"PRIVATE":1,"PRIVATE":2}}', b'PRIVATE', b'["PRIVATE"]', 'PRIVATE'):
            self.execute.return_value = raw
            with self.subTest(raw=raw), self.assertRaisesRegex(EscrowError, '^bound loopback snapshot read failed$'):
                self.io.request('/', self.credentials)

    def test_exec_failure_is_redacted(self):
        self.execute.side_effect = RuntimeError('PRIVATE password')
        with self.assertRaisesRegex(EscrowError, '^bound loopback snapshot read failed$'):
            self.io.request('/', self.credentials)

    def test_archive_exact_location_and_argv(self):
        self.execute.return_value = b'archive'
        self.assertEqual(self.io.read_archive(self.io.location), b'archive')
        self.execute.assert_called_once_with('tar', '-C', self.io.location, '-cf', '-', '.')
        with self.assertRaises(EscrowError):
            self.io.read_archive(self.io.location + '/foreign')
        self.assertEqual(self.execute.call_count, 1)

    def test_unprepared_repository_and_traversal_denied(self):
        for change in ({'repository': 'fixture/foreign'}, {'snapshot': None},
                       {'location': '/usr/share/elasticsearch/config'},
                       {'location': '/usr/share/elasticsearch/data/snapshot/../config'},
                       {'location': '/usr/share/elasticsearch/data/snapshot//child'}):
            values = {'repository': 'fixture', 'snapshot': 'generation', 'location': self.io.location} | change
            with self.subTest(change=change), self.assertRaises(EscrowError):
                LoopbackSnapshotIO(exec_read=self.execute, **values)
        self.execute.assert_not_called()


class PreparedExportTests(unittest.TestCase):
    def setUp(self):
        self.binding, self.pod, self.provider, self.secret, self.parts = fixture()
        self.calls, self.exports = [], []
        self.password = b'synthetic-password'
        self.secret_data = base64.b64encode(self.password)
        self.authority = Mock(return_value=True)
        self.native = synthetic_snapshot_archive()
        self.source = KubernetesSource(self.binding, read_version=lambda: '8.19.1',
            require_authority=self.authority, run=self.remote_read)
        self.prepared = PreparedSnapshotExport(self.source, repository='fixture', snapshot='generation',
            location='/usr/share/elasticsearch/data/snapshot', indices=['fixture'],
            snapshot_uuid='created-uuid', snapshot_version='8.19.0-8.19.1')

    def remote_read(self, argv, data=None):
        self.calls.append((argv, data))
        if argv[1] == 'get':
            if argv[-1] == 'jsonpath={.data.ELASTIC_PASSWORD}':
                return self.secret_data
            return _encoded({'pod': self.pod, 'externalsecret': self.provider, 'secret': self.secret}[argv[2]])
        if argv[-2:] == ['cat', '/proc/1/environ']:
            return b'PATH=/bin\0ELASTIC_PASSWORD=' + self.password + b'\0'
        if argv[-6:] == ['tar', '-C', '/usr/share/elasticsearch/config', '-cf', '-', '.']:
            return configuration_archive(self.binding, self.parts)
        if argv[-6:] == ['tar', '-C', '/usr/share/elasticsearch/data/snapshot', '-cf', '-', '.']:
            return self.native
        path = argv[-1].removeprefix('http://127.0.0.1:9200')
        return _encoded({'/': {'version': {'number': '8.19.1'}},
            '/_security/_authenticate': {'username': 'elastic', 'roles': ['superuser']},
            '/_snapshot/fixture': {'fixture': {'type': 'fs', 'settings': {'location': self.prepared.io.location}}},
            '/_snapshot/fixture/generation': {'snapshots': [{'snapshot': 'generation', 'uuid': 'created-uuid',
                'version': '8.19.0-8.19.1', 'state': 'SUCCESS', 'include_global_state': True,
                'metadata': snapshot_metadata(self.binding), 'indices': ['fixture', '.security-7'],
                'shards': {'total': 2, 'successful': 2, 'failed': 0},
                'feature_states': [{'feature_name': 'security', 'indices': ['.security-7']}]}]}}[path])

    def capture(self):
        # Capture-contract test only. No fake ciphertext or provider acceptance.
        return self.source.capture(self.binding, capture_native=self.prepared.adapter.native,
                                   capture_credentials=self.prepared.adapter.credentials)

    def test_complete_composition_original_schema_runtime_and_native(self):
        parts = self.capture()
        self.assertEqual(parts['native']['data'], self.native)
        self.assertEqual(json.loads(parts['credentials']['data']),
                         {'elastic_username': 'elastic', 'elastic_password': self.password.decode()})
        self.assertNotIn('ELASTIC_PASSWORD', json.loads(parts['runtime']['data'])['variables'])
        self.assertEqual(parts['config/elasticsearch.keystore'], self.parts['config/elasticsearch.keystore'])
        self.assertTrue(any(argv[-1] == 'jsonpath={.data.ELASTIC_PASSWORD}' for argv, _ in self.calls))
        self.assertFalse(any('synthetic-password' in repr(argv) for argv, _ in self.calls))

    def test_no_authority_denies_before_secret_read_or_export(self):
        self.authority.return_value = False
        encrypt = Mock()
        with self.assertRaises(EscrowError):
            self.prepared.export(encrypt_export=encrypt)
        self.assertEqual(self.calls, [])
        encrypt.assert_not_called()

    def test_changed_source_binding_denied_before_read(self):
        self.source.binding['generation'] = 'e' * 32
        with self.assertRaisesRegex(EscrowError, '^prepared source binding changed$'):
            self.prepared.export(encrypt_export=Mock())
        self.assertEqual(self.calls, [])

    def test_secret_rotation_during_data_read_denies_before_authentication(self):
        original = self.source.run
        def rotate(argv, data=None):
            result = original(argv, data=data)
            if argv[-1] == 'jsonpath={.data.ELASTIC_PASSWORD}':
                self.secret['resourceVersion'] = 'changed'
            return result
        self.source.run = rotate
        with self.assertRaises(EscrowError):
            self.capture()
        self.assertFalse(any(argv[1] == 'exec' for argv, _ in self.calls))

    def test_invalid_secret_encodings_denied_before_exec(self):
        for raw in (b'', b' PRIVATE ', b'c3ludGhldGlj\n', base64.b64encode(b'\xff'),
                    base64.b64encode(b'password\n')):
            self.secret_data, self.calls = raw, []
            with self.subTest(raw=raw), self.assertRaises(EscrowError):
                self.capture()
            self.assertFalse(any(argv[1] == 'exec' for argv, _ in self.calls))

    def test_prepared_uuid_mismatch_denied_before_archive_or_config(self):
        self.prepared.adapter.expected_uuid = 'foreign-uuid'
        with self.assertRaisesRegex(EscrowError, 'snapshot_uuid$'):
            self.capture()
        self.assertFalse(any('tar' in argv for argv, _ in self.calls))

    def test_provider_runtime_password_mismatch_never_reaches_export(self):
        self.password = b'changed-runtime-only'
        encrypt = Mock()
        with self.assertRaisesRegex(EscrowError, 'credentials differ from source runtime generation'):
            self.prepared.export(encrypt_export=encrypt)
        encrypt.assert_not_called()
        self.assertFalse(any(argv[-6:] == ['tar', '-C', '/usr/share/elasticsearch/config', '-cf', '-', '.']
                             for argv, _ in self.calls))

    def test_original_catalog_capture_requires_authority_before_secret_io(self):
        self.authority.return_value = False
        contract = fixture_consumer_plan()
        with self.assertRaisesRegex(EscrowError, 'original export authorization'):
            self.prepared.capture_catalog(ledger=contract['ledger'], backend=contract['backend'],
                                         consumers=contract['consumers'])
        self.assertEqual(self.calls, [])

    def test_original_catalog_rejects_partial_ledger_before_any_io(self):
        contract = fixture_consumer_plan()
        with self.assertRaises(EscrowError):
            self.prepared.capture_catalog(ledger=contract['ledger'], backend=contract['backend'], consumers=[])
        self.assertEqual(self.calls, [])

    def test_original_catalog_reads_exact_indices_without_export_or_binding_mutation(self):
        contract = fixture_consumer_plan()
        routes = {'/fixture/_settings?flat_settings=true': {'fixture': {'settings': {'index.uuid': 'original'}}},
            '/fixture/_mapping': {'fixture': {'mappings': {}}},
            '/fixture/_alias': {'fixture': {'aliases': {}}},
            '/fixture/_count': {'count': 2, '_shards': {'total': 1, 'successful': 1, 'failed': 0}}}
        original = self.source.run
        def read(argv, data=None):
            path = argv[-1].removeprefix('http://127.0.0.1:9200')
            if argv[1] == 'exec' and path in routes:
                self.calls.append((argv, data))
                return _encoded(routes[path])
            return original(argv, data=data)
        self.source.run = read
        catalog = self.prepared.capture_catalog(ledger=contract['ledger'], backend=contract['backend'],
                                               consumers=contract['consumers'])
        self.assertEqual(catalog['indices']['fixture']['count'], 2)
        self.assertNotIn('source_catalog', self.source.binding)
        self.assertNotIn('source_catalog', self.prepared.binding)
        self.assertFalse(any('tar' in argv for argv, _ in self.calls))
        self.assertFalse(self.exports)

    def prepare_query_capture(self):
        contract = fixture_consumer_plan()
        routes = {'/fixture/_settings?flat_settings=true': {'fixture': {'settings': {'index.uuid': 'original'}}},
            '/fixture/_mapping': {'fixture': {'mappings': {}}},
            '/fixture/_alias': {'fixture': {'aliases': {}}},
            '/fixture/_count': {'count': 1, '_shards': {'total': 1, 'successful': 1, 'failed': 0}},
            '/fixture/_search?allow_partial_search_results=false': {'timed_out': False,
                '_shards': {'total': 1, 'successful': 1, 'failed': 0},
                'hits': {'total': {'value': 1, 'relation': 'eq'}, 'hits': [
                    {'_index': 'fixture', '_id': 'one', '_source': {'number': 1}, 'sort': [1]}]}}}
        original = self.source.run
        def read(argv, data=None):
            path = argv[-1].removeprefix('http://127.0.0.1:9200')
            if argv[1] == 'exec' and path in routes:
                self.calls.append((argv, data))
                return _encoded(routes[path])
            return original(argv, data=data)
        self.source.run = read
        self.query_routes = routes
        catalog = self.prepared.capture_catalog(ledger=contract['ledger'], backend=contract['backend'],
                                               consumers=contract['consumers'])
        self.binding['source_catalog'] = catalog
        self.source.binding = copy.deepcopy(self.binding)
        self.prepared = PreparedSnapshotExport(self.source, repository='fixture', snapshot='generation',
            location='/usr/share/elasticsearch/data/snapshot', indices=['fixture'],
            snapshot_uuid='created-uuid', snapshot_version='8.19.0-8.19.1')
        contracts = [{k: contract['consumers'][0][k] for k in ('application', 'store')} |
            {'queries': [{'id': 'complete', 'index': 'fixture', 'body': {
                'query': {'match_all': {}}, 'sort': [{'number': 'asc'}], 'size': 10,
                'track_total_hits': True, '_source': True}}]}]
        self.calls = []
        return contract, contracts

    def test_prepared_original_query_capture_concrete_get_and_no_export(self):
        contract, contracts = self.prepare_query_capture()
        value = self.prepared.capture_queries(ledger=contract['ledger'], backend=contract['backend'],
            consumers=contract['consumers'], contracts=contracts)
        self.assertEqual(value['results'][0]['count'], 1)
        searches = [(argv, data) for argv, data in self.calls if argv[-1].endswith('allow_partial_search_results=false')]
        self.assertEqual(len(searches), 2)
        self.assertTrue(all(b'request = "GET"' in data for _, data in searches))
        self.assertNotIn('source_queries', self.source.binding)
        self.assertFalse(self.exports)
        self.assertFalse(any('tar' in argv for argv, _ in self.calls))

    def test_prepared_query_authority_denied_before_credentials_or_query_io(self):
        contract, contracts = self.prepare_query_capture()
        self.authority.return_value = False
        with self.assertRaises(EscrowError):
            self.prepared.capture_queries(ledger=contract['ledger'], backend=contract['backend'],
                consumers=contract['consumers'], contracts=contracts)
        self.assertEqual(self.calls, [])

    def test_prepared_query_restart_during_search_denies_returned_bytes(self):
        contract, contracts = self.prepare_query_capture()
        original = self.source.run
        def restart(argv, data=None):
            value = original(argv, data=data)
            if argv[-1].endswith('allow_partial_search_results=false'):
                self.pod['status']['containerStatuses'][0]['restartCount'] += 1
            return value
        self.source.run = restart
        with self.assertRaisesRegex(EscrowError, 'bound consumer query failed'):
            self.prepared.capture_queries(ledger=contract['ledger'], backend=contract['backend'],
                consumers=contract['consumers'], contracts=contracts)
        self.assertEqual(sum(argv[-1].endswith('allow_partial_search_results=false') for argv, _ in self.calls), 1)

    def test_prepared_query_missing_roster_denied_before_source_io(self):
        contract, contracts = self.prepare_query_capture()
        with self.assertRaises(EscrowError):
            self.prepared.capture_queries(ledger=contract['ledger'], backend=contract['backend'],
                consumers=contract['consumers'], contracts=[])
        self.assertEqual(self.calls, [])

    def test_original_catalog_restart_during_count_read_denies_result(self):
        contract = fixture_consumer_plan()
        original = self.source.exec_read
        def restart(*command, data=None):
            if command[-1].endswith('/fixture/_count'):
                self.pod['status']['containerStatuses'][0]['restartCount'] += 1
                return _encoded({'count': 2, '_shards': {'total': 1, 'successful': 1, 'failed': 0}})
            if '/fixture/' in command[-1]:
                suffix = command[-1].split('/fixture/', 1)[1]
                key = {'_settings?flat_settings=true': 'settings', '_mapping': 'mappings', '_alias': 'aliases'}[suffix]
                return _encoded({'fixture': {key: {'index.uuid': 'original'} if key == 'settings' else {}}})
            return original(*command, data=data)
        self.source.exec_read = restart
        with self.assertRaisesRegex(EscrowError, 'lifetime or credential-provider version changed'):
            self.prepared.capture_catalog(ledger=contract['ledger'], backend=contract['backend'],
                                         consumers=contract['consumers'])

    def test_snapshot_creation_identity_required_without_io(self):
        for value in ('', None):
            with self.subTest(value=value), self.assertRaises(EscrowError):
                PreparedSnapshotExport(self.source, repository='fixture', snapshot='generation',
                    location=self.prepared.io.location, indices=['fixture'], snapshot_uuid=value,
                    snapshot_version='8.19.0')
        self.assertEqual(self.calls, [])


if __name__ == '__main__':
    unittest.main()
