"""Catalog regressions, ARC only. Synthetic responses are not engine proof."""
import copy
import json
from pathlib import Path
import secrets
import sys
import unittest
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from kopiur_elasticsearch_catalog import LoopbackCatalogIO, SourceCatalog, validate_catalog
from kopiur_elasticsearch_engine_fixture import fixture_consumer_plan
from kopiur_elasticsearch_escrow import EscrowError, _binding, _encoded, _validate_component, _validate_parts, _current
from kopiur_elasticsearch_escrow_transport import encode_bundle, decode_bundle
from kopiur_elasticsearch_restore import RestorePlan
from elasticsearch_restore_cases import restore_inputs


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.manifest, self.parts, self.runtime, self.credentials = restore_inputs()
        self.binding = self.manifest['binding']
        self.contract = fixture_consumer_plan()
        self.guard = Mock(return_value=True)
        self.requests = []
        self.routes = {
            '/fixture/_settings?flat_settings=true': {'fixture': {'settings': {
                'index.uuid': 'source-index-identity', 'index.blocks.write': 'true',
                'index.creation_date': '1767225600000', 'index.number_of_shards': '1'}}},
            '/fixture/_mapping': {'fixture': {'mappings': {'properties': {'title': {'type': 'keyword'}}}}},
            '/fixture/_alias': {'fixture': {'aliases': {'fixture-alias': {}}}},
            '/fixture/_count': {'count': 2, '_shards': {'total': 1, 'successful': 1, 'failed': 0}},
        }
        self.catalog = self.build()

    def request(self, path, credentials):
        self.requests.append(path)
        self.assertEqual(credentials, self.credentials)
        return copy.deepcopy(self.routes[path])

    def build(self):
        return SourceCatalog(self.binding, ledger=self.contract['ledger'], backend=self.contract['backend'],
            consumer_indices=self.contract['consumers'], indices=['fixture'], guard=self.guard, request=self.request)

    def test_fresh_catalog_binds_source_ledger_and_every_index(self):
        value = self.catalog.capture(self.credentials)
        self.assertEqual(value['source']['source_uid'], self.binding['source_uid'])
        self.assertEqual(value['consumers'], self.contract['consumers'])
        self.assertEqual(value['indices']['fixture']['count'], 2)
        self.assertEqual(len(self.requests), 8)
        self.assertEqual(self.guard.call_count, 16)
        self.assertNotIn(self.credentials['elastic_password'], _encoded(value).decode())

    def test_catalog_result_and_inputs_do_not_alias_internal_state(self):
        self.contract['consumers'][0]['indices'].append('foreign')
        value = self.catalog.capture(self.credentials)
        value['indices']['fixture']['count'] = 99
        self.assertEqual(self.catalog.capture(self.credentials)['indices']['fixture']['count'], 2)

    def test_revocation_denies_before_any_read(self):
        for value in (False, None, 1):
            self.guard.return_value = value
            with self.subTest(value=value), self.assertRaises(EscrowError):
                self.catalog.capture(self.credentials)
        self.assertEqual(self.requests, [])

    def test_revocation_after_each_read_denies_returned_metadata(self):
        for point in range(1, 9):
            self.setUp()
            self.guard.side_effect = [True] * (point * 2 - 1) + [False]
            with self.subTest(point=point), self.assertRaises(EscrowError):
                self.catalog.capture(self.credentials)
            self.assertEqual(len(self.requests), point)

    def test_changed_index_generation_between_passes_denied(self):
        count = 0
        def changing(path, credentials):
            nonlocal count
            count += 1
            if count == 5:
                self.routes[path]['fixture']['settings']['index.uuid'] = 'replacement-index'
            return self.request(path, credentials)
        self.catalog.request = changing
        with self.assertRaisesRegex(EscrowError, 'changed during capture'):
            self.catalog.capture(self.credentials)

    def test_partial_or_invalid_counts_denied(self):
        for value in ({'count': True, '_shards': {'total': 1, 'successful': 1, 'failed': 0}},
                      {'count': -1, '_shards': {'total': 1, 'successful': 1, 'failed': 0}},
                      {'count': 2}, {'count': 2, '_shards': {'total': 2, 'successful': 1, 'failed': 1}},
                      {'count': 2, '_shards': {'total': True, 'successful': 1, 'failed': 0}},
                      {'count': 2, '_shards': {'total': 1, 'successful': True, 'failed': 0}},
                      {'count': 2, '_shards': {'total': 1, 'successful': 1, 'failed': False}}):
            self.routes['/fixture/_count'] = value
            with self.subTest(value=value), self.assertRaises(EscrowError):
                self.catalog.capture(self.credentials)

    def test_foreign_index_extra_metadata_and_malformed_payloads_denied(self):
        for value in ({}, [], {'other': {'mappings': {}}},
                      {'fixture': {'mappings': {}, 'extra': 'must-not-ignore'}},
                      {'fixture': {'mappings': []}},
                      {'fixture': {'mappings': {'value': float('nan')}}}):
            self.routes['/fixture/_mapping'] = value
            with self.subTest(value_type=type(value).__name__), self.assertRaises(EscrowError):
                self.catalog.capture(self.credentials)

    def test_adapter_exception_has_no_secret_in_diagnostics(self):
        self.catalog.request = Mock(side_effect=RuntimeError(self.credentials['elastic_password']))
        with self.assertRaisesRegex(EscrowError, '^authenticated source catalog read failed$') as caught:
            self.catalog.capture(self.credentials)
        self.assertTrue(caught.exception.__suppress_context__)

    def test_native_restore_allows_only_new_internal_index_uuid(self):
        captured = self.catalog.capture(self.credentials)
        self.routes['/fixture/_settings?flat_settings=true']['fixture']['settings']['index.uuid'] = 'restore-uuid'
        receipt = self.catalog.verify_restore(captured, self.credentials)
        self.assertIs(receipt['fresh_counts_metadata_verified'], True)
        self.assertIs(receipt['production_recovery_accepted'], False)
        self.assertEqual(receipt['index_count'], 1)
        self.assertEqual(receipt['consumer_store_count'], 1)
        self.assertEqual(captured['indices']['fixture']['settings']['index.uuid'], 'source-index-identity')

    def test_native_restore_count_mapping_alias_timestamp_and_fence_differences_denied(self):
        mutations = [lambda: self.routes['/fixture/_count'].update(count=3),
            lambda: self.routes['/fixture/_mapping']['fixture']['mappings'].clear(),
            lambda: self.routes['/fixture/_alias']['fixture']['aliases'].clear(),
            lambda: self.routes['/fixture/_settings?flat_settings=true']['fixture']['settings'].update(
                {'index.creation_date': 'changed'}),
            lambda: self.routes['/fixture/_settings?flat_settings=true']['fixture']['settings'].update(
                {'index.blocks.write': 'false'})]
        for mutation in mutations:
            self.setUp()
            value = self.catalog.capture(self.credentials)
            mutation()
            with self.subTest(mutation=mutation), self.assertRaisesRegex(EscrowError, 'catalog differs'):
                self.catalog.verify_restore(value, self.credentials)

    def test_nested_json_types_cannot_compare_equal_after_restore(self):
        for route, key in (('/fixture/_mapping', 'mappings'), ('/fixture/_alias', 'aliases')):
            self.setUp()
            self.routes[route]['fixture'][key]['metadata'] = {'revision': 1}
            captured = self.catalog.capture(self.credentials)
            self.routes[route]['fixture'][key]['metadata']['revision'] = True
            with self.subTest(route=route), self.assertRaisesRegex(EscrowError, 'catalog differs'):
                self.catalog.verify_restore(captured, self.credentials)

    def test_nested_json_type_change_between_capture_passes_denied(self):
        self.routes['/fixture/_mapping']['fixture']['mappings']['_meta'] = {'revision': 1}
        calls = 0
        def changing(path, credentials):
            nonlocal calls
            calls += 1
            if calls == 6:
                self.routes[path]['fixture']['mappings']['_meta']['revision'] = True
            return self.request(path, credentials)
        self.catalog.request = changing
        with self.assertRaisesRegex(EscrowError, 'changed during capture'):
            self.catalog.capture(self.credentials)

    def test_missing_consumer_reciprocal_dependency_denied_before_io(self):
        self.contract['ledger']['applications'][0]['state_dependencies'] = []
        with self.assertRaises(EscrowError):
            self.build()
        self.assertEqual(self.requests, [])

    def test_catalog_binding_and_coverage_tampering_denied(self):
        captured = self.catalog.capture(self.credentials)
        for mutation in (lambda v: v['source'].update(generation='foreign'),
                         lambda v: v.update(ledger_sha256='not-a-hash'),
                         lambda v: v['consumers'].append(copy.deepcopy(v['consumers'][0])),
                         lambda v: v['indices']['fixture'].update(count=True),
                         lambda v: v['indices']['fixture']['settings'].update({'index.uuid': ''}),
                         lambda v: v['consumers'][0].update(indices=['foreign'])):
            value = copy.deepcopy(captured)
            mutation(value)
            with self.subTest(mutation=mutation), self.assertRaises(EscrowError):
                validate_catalog(value, self.binding)

    def catalog_parts(self):
        self.routes['/fixture/_mapping']['fixture']['mappings']['_meta'] = {'revision': 1}
        self.binding['source_catalog'] = self.catalog.capture(self.credentials)
        for part in self.parts.values():
            part['binding'] = copy.deepcopy(self.binding)
        self.manifest['entries'] = _validate_parts(self.binding, self.parts)
        return self.manifest, self.parts

    @staticmethod
    def change_catalog_type(binding):
        binding['source_catalog']['indices']['fixture']['mappings']['_meta']['revision'] = True

    def test_component_catalog_json_type_mismatch_denied(self):
        self.catalog_parts()
        component = copy.deepcopy(self.parts['native'])
        self.change_catalog_type(component['binding'])
        with self.assertRaisesRegex(EscrowError, 'mixed or stale component binding'):
            _validate_component(self.binding, 'native', component)

    def test_observed_source_catalog_json_type_mismatch_denied(self):
        self.catalog_parts()
        observed = copy.deepcopy(self.binding)
        self.change_catalog_type(observed)
        with self.assertRaisesRegex(EscrowError, 'source identity or runtime/credential version changed'):
            _current(self.binding, lambda: observed)

    def test_prepared_manifest_catalog_json_type_mutation_denied(self):
        self.catalog_parts()
        plan = RestorePlan(self.manifest, self.parts, replay=list(self.runtime['variables']), omit={},
            repository='fixture', snapshot='generation', location='/usr/share/elasticsearch/data/snapshot',
            indices=['fixture'], **self.contract)
        changed = copy.deepcopy(self.manifest)
        self.change_catalog_type(changed['binding'])
        with self.assertRaisesRegex(EscrowError, 'prepared restore generation changed'):
            plan.check(changed, self.parts)

    def test_canonical_bundle_catalog_json_type_mismatch_denied(self):
        self.catalog_parts()
        data, expected = encode_bundle(self.binding, self.parts)
        bundle = json.loads(data)
        self.change_catalog_type(bundle['manifest']['binding'])
        for part in bundle['parts'].values():
            self.change_catalog_type(part['binding'])
        changed = _encoded(bundle)
        with self.assertRaisesRegex(EscrowError, '^escrow bundle invalid$'):
            decode_bundle(changed, expected)

    def test_source_catalog_must_match_restore_ledger_before_target_creation(self):
        catalog = self.catalog.capture(self.credentials)
        self.binding['source_catalog'] = catalog
        for part in self.parts.values():
            part['binding'] = copy.deepcopy(self.binding)
        self.manifest['entries'] = __import__('kopiur_elasticsearch_escrow')._validate_parts(self.binding, self.parts)
        catalog['ledger_sha256'] = 'f' * 64
        for part in self.parts.values():
            part['binding'] = copy.deepcopy(self.binding)
        with self.assertRaisesRegex(EscrowError, 'differs from prepared consumer coverage'):
            RestorePlan(self.manifest, self.parts, replay=list(self.runtime['variables']), omit={},
                repository='fixture', snapshot='generation', location='/usr/share/elasticsearch/data/snapshot',
                indices=['fixture'], **self.contract)

    def test_source_catalog_is_validated_by_escrow_binding(self):
        self.binding['source_catalog'] = self.catalog.capture(self.credentials)
        self.assertEqual(_binding(self.binding), self.binding)
        self.binding['source_catalog']['source']['source_uid'] = 'foreign'
        with self.assertRaisesRegex(EscrowError, 'catalog binding invalid'):
            _binding(self.binding)


class LoopbackCatalogTests(unittest.TestCase):
    def setUp(self):
        self.exec = Mock(return_value=b'{"count":2}')
        self.io = LoopbackCatalogIO(exec_read=self.exec, indices=['fixture'])
        self.credentials = {'elastic_username': 'elastic', 'elastic_password': secrets.token_hex(24)}

    def test_exact_read_uses_credential_safe_loopback_transport(self):
        self.assertEqual(self.io.request('/fixture/_count', self.credentials), {'count': 2})
        args = self.exec.call_args.args
        self.assertEqual(args[-1], 'http://127.0.0.1:9200/fixture/_count')
        self.assertIn('-q', args)
        self.assertIn('--noproxy', args)
        self.assertNotIn(self.credentials['elastic_password'], ' '.join(args))
        self.assertIn(self.credentials['elastic_password'].encode(), self.exec.call_args.kwargs['data'])

    def test_paths_do_not_admit_all_indices_alias_expansion_queries_or_writes(self):
        for path in ('/_all/_count', '/fixture-alias/_count', '/fixture/_count?q=*',
                     '/fixture/_search', '/fixture/_settings', '/other/_count',
                     '/fixture/_count/../_delete_by_query', '/_snapshot/fixture'):
            with self.subTest(path=path), self.assertRaises(EscrowError):
                self.io.request(path, self.credentials)
        self.exec.assert_not_called()

    def test_catalog_capability_cannot_read_archives(self):
        with self.assertRaisesRegex(EscrowError, 'cannot read archives'):
            self.io.read_archive('/usr/share/elasticsearch/data/snapshot')
        self.exec.assert_not_called()

    def test_unqualified_indices_denied_before_transport(self):
        for indices in ([], ['*'], ['.security-7'], ['fixture,foreign'], ['fixture', 'fixture'],
                        ['fixture/../foreign'], ['fixture?expand_wildcards=all']):
            with self.subTest(indices=indices), self.assertRaises(EscrowError):
                LoopbackCatalogIO(exec_read=self.exec, indices=indices)
        self.exec.assert_not_called()

    def test_duplicate_keys_and_nonobject_transport_responses_denied(self):
        for raw in (b'{"count":1,"count":2}', b'[]', b'not-json'):
            self.exec.return_value = raw
            with self.subTest(raw_type=type(raw).__name__), self.assertRaises(EscrowError):
                self.io.request('/fixture/_count', self.credentials)


if __name__ == '__main__':
    unittest.main()
