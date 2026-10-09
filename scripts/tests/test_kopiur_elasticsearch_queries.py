"""Consumer query regressions. Execute only on existing ARC."""
import copy
from pathlib import Path
import secrets
import sys
import unittest
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from elasticsearch_restore_cases import restore_inputs
from kopiur_elasticsearch_catalog import SourceCatalog
from kopiur_elasticsearch_engine_fixture import fixture_consumer_plan
from kopiur_elasticsearch_escrow import EscrowError, _binding, _encoded, _validate_parts, capture_export
from kopiur_elasticsearch_queries import (
    ConsumerQueries, LoopbackQueryIO, contract_digests, result_digest, validate_queries,
)
from kopiur_elasticsearch_escrow_transport import encode_bundle, decode_bundle


def body():
    return {'query': {'match_all': {}}, 'sort': [{'number': 'asc'}], 'size': 10,
            'track_total_hits': True, '_source': True}


def response():
    return {'timed_out': False, '_shards': {'total': 1, 'successful': 1, 'failed': 0},
        'hits': {'total': {'value': 2, 'relation': 'eq'}, 'hits': [
            {'_index': 'fixture', '_id': str(n), '_source': {'number': n, 'title': 'private-value'},
             'sort': [n]} for n in (1, 2)]}}


class QueryTests(unittest.TestCase):
    def setUp(self):
        self.manifest, self.parts, _, self.credentials = restore_inputs()
        self.binding = self.manifest['binding']
        self.contract = fixture_consumer_plan()
        self.guard = Mock(return_value=True)
        self.routes = {
            '/fixture/_settings?flat_settings=true': {'fixture': {'settings': {'index.uuid': 'source',
                'index.blocks.write': 'true', 'index.creation_date': '1767225600000'}}},
            '/fixture/_mapping': {'fixture': {'mappings': {}}},
            '/fixture/_alias': {'fixture': {'aliases': {}}},
            '/fixture/_count': {'count': 2, '_shards': {'total': 1, 'successful': 1, 'failed': 0}},
        }
        self.catalog = SourceCatalog(self.binding, ledger=self.contract['ledger'],
            backend=self.contract['backend'], consumer_indices=self.contract['consumers'],
            indices=['fixture'], guard=self.guard, request=lambda p, c: copy.deepcopy(self.routes[p]))
        self.binding['source_catalog'] = self.catalog.capture(self.credentials)
        self.catalog.binding = copy.deepcopy(self.binding)
        self.records = [{k: self.contract['consumers'][0][k] for k in ('application', 'store')} |
            {'queries': [{'id': 'complete', 'index': 'fixture', 'body': body()}]}]
        self.value = response()
        self.query = Mock(side_effect=lambda i, b, c: copy.deepcopy(self.value))
        self.guard.reset_mock()
        self.prepared = self.build()

    def build(self):
        return ConsumerQueries(self.catalog, contracts=self.records, query=self.query)

    def test_capture_binds_catalog_roster_explicit_query_and_result_digest(self):
        captured = self.prepared.capture(self.credentials)
        self.assertEqual(captured['contracts'], contract_digests(self.records))
        self.assertEqual(captured['results'][0]['count'], 2)
        self.assertEqual(self.query.call_count, 2)
        self.assertNotIn('private-value', _encoded(captured).decode())
        self.assertNotIn(self.credentials['elastic_password'], _encoded(captured).decode())

    def test_results_are_independent_from_mutated_caller_contracts(self):
        self.records[0]['queries'][0]['body']['size'] = 1
        self.assertEqual(self.prepared.contracts[0]['queries'][0]['body']['size'], 10)

    def test_missing_foreign_duplicate_or_incomplete_roster_denied_before_io(self):
        for records in ([], self.records * 2, [self.records[0] | {'application': 'foreign'}],
                        [self.records[0] | {'queries': []}]):
            self.records = records
            with self.subTest(records_type=type(records).__name__), self.assertRaises(EscrowError):
                self.build()
        self.query.assert_not_called()
        self.guard.assert_not_called()

    def test_unqualified_index_and_duplicate_query_ids_denied_before_io(self):
        for index in ('*', 'fixture-alias', '.security-7', 'fixture/../foreign', [], None):
            self.records[0]['queries'][0]['index'] = index
            with self.subTest(index_type=type(index).__name__), self.assertRaises(EscrowError):
                self.build()
        self.records[0]['queries'][0]['index'] = 'fixture'
        self.records[0]['queries'] *= 2
        with self.assertRaises(EscrowError):
            self.build()
        self.query.assert_not_called()

    def test_guard_revocation_before_each_query_denies_remaining_io(self):
        for boundary in (1, 3):
            self.setUp()
            # Catalog reads occur first, then every query has before/after guards.
            calls = 0
            def guard():
                nonlocal calls
                calls += 1
                return calls != 8 + boundary
            self.guard.side_effect = guard
            with self.assertRaises(EscrowError):
                self.prepared.capture(self.credentials)
            self.assertEqual(self.query.call_count, 0 if boundary == 1 else 1)

    def test_revocation_after_returned_query_denies_result(self):
        active = True
        def query(*args):
            nonlocal active
            active = False
            return response()
        self.query.side_effect = query
        self.guard.side_effect = lambda: active
        with self.assertRaisesRegex(EscrowError, 'affirmative query authority'):
            self.prepared.capture(self.credentials)
        self.assertEqual(self.query.call_count, 1)

    def test_failed_query_sanitizes_private_error(self):
        self.query.side_effect = RuntimeError(self.credentials['elastic_password'])
        with self.assertRaisesRegex(EscrowError, '^bound consumer query failed$') as error:
            self.prepared.capture(self.credentials)
        self.assertTrue(error.exception.__suppress_context__)

    def test_changed_documents_same_counts_denied_between_passes(self):
        second = response()
        second['hits']['hits'][0]['_source']['title'] = 'different'
        self.query.side_effect = [response(), second]
        with self.assertRaisesRegex(EscrowError, 'changed during capture'):
            self.prepared.capture(self.credentials)

    def test_nested_json_bool_number_change_denied(self):
        second = response()
        second['hits']['hits'][0]['_source']['number'] = True
        self.query.side_effect = [response(), second]
        with self.assertRaisesRegex(EscrowError, 'changed during capture'):
            self.prepared.capture(self.credentials)

    def test_catalog_changed_before_or_after_query_denied(self):
        self.routes['/fixture/_count']['count'] = 3
        with self.assertRaisesRegex(EscrowError, 'catalog changed'):
            self.prepared.capture(self.credentials)
        self.query.assert_not_called()
        self.setUp()
        def query(*args):
            self.routes['/fixture/_count']['count'] = 3
            return response()
        self.query.side_effect = query
        with self.assertRaisesRegex(EscrowError, 'catalog changed'):
            self.prepared.capture(self.credentials)

    def test_restore_query_hash_matches_with_new_native_index_uuid(self):
        value = self.prepared.capture(self.credentials)
        self.routes['/fixture/_settings?flat_settings=true']['fixture']['settings']['index.uuid'] = 'restored'
        receipt = self.prepared.verify_restore(value, self.credentials)
        self.assertIs(receipt['complete_query_results_verified'], True)
        self.assertIs(receipt['production_application_accepted'], False)
        self.assertEqual(receipt['query_count'], 1)

    def test_restore_content_difference_not_hidden_by_catalog_count(self):
        value = self.prepared.capture(self.credentials)
        self.value['hits']['hits'][0]['_source']['title'] = 'different'
        with self.assertRaisesRegex(EscrowError, 'restore consumer query results differ'):
            self.prepared.verify_restore(value, self.credentials)

    def test_restore_foreign_catalog_or_query_contract_denied(self):
        value = self.prepared.capture(self.credentials)
        value['catalog_sha256'] = 'f' * 64
        with self.assertRaisesRegex(EscrowError, 'catalog binding invalid'):
            self.prepared.verify_restore(value, self.credentials)
        value = self.prepared.capture(self.credentials)
        value['contracts'][0]['queries'][0]['body_sha256'] = 'f' * 64
        with self.assertRaisesRegex(EscrowError, 'contracts differ'):
            self.prepared.verify_restore(value, self.credentials)

    def test_source_binding_and_encrypted_bundle_preserve_query_contract(self):
        self.binding['source_queries'] = self.prepared.capture(self.credentials)
        self.assertEqual(_binding(self.binding), self.binding)
        for part in self.parts.values():
            part['binding'] = copy.deepcopy(self.binding)
        self.manifest['entries'] = _validate_parts(self.binding, self.parts)
        data, manifest = encode_bundle(self.binding, self.parts)
        self.assertEqual(decode_bundle(data, manifest), self.parts)

    def test_query_binding_results_and_roster_tampering_denied(self):
        value = self.prepared.capture(self.credentials)
        for mutate in (lambda v: v.update(results=[]),
                       lambda v: v['results'][0].update(count=True),
                       lambda v: v['results'][0].update(sha256='not-a-hash'),
                       lambda v: v['results'].append(copy.deepcopy(v['results'][0])),
                       lambda v: v['results'][0].update(id='foreign'),
                       lambda v: v['contracts'][0].update(store='foreign')):
            changed = copy.deepcopy(value)
            mutate(changed)
            with self.subTest(mutation=mutate), self.assertRaises(EscrowError):
                validate_queries(changed, self.binding)

    def test_private_query_value_absent_from_entire_public_export_receipt(self):
        self.records[0]['queries'][0]['body']['query'] = {'term': {'title': 'private-query-value'}}
        self.binding['source_queries'] = self.build().capture(self.credentials)
        for part in self.parts.values():
            part['binding'] = copy.deepcopy(self.binding)
        from kopiur_elasticsearch_escrow import _digest
        export = capture_export(self.binding, observe=lambda: copy.deepcopy(self.binding),
            capture=lambda binding: copy.deepcopy(self.parts), require_capture_authority=lambda binding: True,
            encrypt_export=lambda manifest, parts: {'ciphertext': b'unit-test-only-transport-placeholder',
                'object_id': 'unit-test-only', 'manifest_sha256': _digest(_encoded(manifest))})
        # Callback contract regression only, not real encryption/provider proof.
        public = repr(export)
        self.assertNotIn('private-query-value', public)
        self.assertNotIn('private-value', public)
        self.assertNotIn(self.credentials['elastic_password'], public)
        self.assertIn('body_sha256', public)

    def test_two_authoritative_consumer_pairs_each_require_own_query_coverage(self):
        ledger = {'applications': [], 'physical_stores': []}
        consumers, records = [], []
        for app in ('media/tubearchivist', 'services/zoo-cowbell'):
            store = 'elasticsearch-indices:' + app
            ledger['applications'].append({'id': app, 'state_dependencies': [store]})
            ledger['physical_stores'].append({'id': store, 'kind': 'external_elasticsearch_indices',
                'backend_contract': self.contract['backend'], 'consumer_contracts': [app]})
            consumers.append({'application': app, 'store': store, 'indices': ['fixture']})
            records.append({'application': app, 'store': store,
                'queries': [{'id': 'complete', 'index': 'fixture', 'body': body()}]})
        catalog = SourceCatalog(self.binding, ledger=ledger, backend=self.contract['backend'],
            consumer_indices=consumers, indices=['fixture'], guard=self.guard,
            request=lambda p, c: copy.deepcopy(self.routes[p]))
        catalog.binding['source_catalog'] = catalog.capture(self.credentials)
        for missing in (records[:1], records[1:]):
            with self.assertRaisesRegex(EscrowError, 'roster differs'):
                ConsumerQueries(catalog, contracts=missing, query=self.query)
        prepared = ConsumerQueries(catalog, contracts=records, query=self.query)
        self.assertEqual(len(prepared.capture(self.credentials)['results']), 2)


class QueryResponseTests(unittest.TestCase):
    def test_unique_but_incorrect_sort_order_denied_for_all_directions(self):
        value = response()
        value['hits']['hits'].reverse()
        with self.assertRaises(EscrowError):
            result_digest(value, 'fixture', body())
        request = body() | {'sort': [{'number': 'desc'}]}
        result_digest(value, 'fixture', request)
        with self.assertRaises(EscrowError):
            result_digest(response(), 'fixture', request)
        request['sort'] = [{'category': 'asc'}, {'number': 'desc'}]
        value['hits']['hits'][0]['sort'] = ['same', 2]
        value['hits']['hits'][1]['sort'] = ['same', 1]
        result_digest(value, 'fixture', request)
        value['hits']['hits'].reverse()
        with self.assertRaises(EscrowError):
            result_digest(value, 'fixture', request)

    def test_sort_types_and_numerically_equal_tuples_are_not_unique_order(self):
        for sort in ([1.0], ['2'], [True]):
            value = response()
            value['hits']['hits'][1]['sort'] = sort
            with self.subTest(sort=sort), self.assertRaises(EscrowError):
                result_digest(value, 'fixture', body())

    def test_partial_timeout_truncated_and_inexact_totals_denied(self):
        mutations = [lambda r: r.update(timed_out=True), lambda r: r.update(terminated_early=True),
            lambda r: r['_shards'].update(failed=1), lambda r: r['_shards'].update(total=True),
            lambda r: r['_shards'].update(successful=0), lambda r: r['hits']['total'].update(relation='gte'),
            lambda r: r['hits']['total'].update(value=True), lambda r: r['hits']['total'].update(value=11),
            lambda r: r['hits']['hits'].pop()]
        for mutate in mutations:
            value = response()
            mutate(value)
            with self.subTest(mutation=mutate), self.assertRaises(EscrowError):
                result_digest(value, 'fixture', body())

    def test_wrong_index_duplicate_ids_duplicate_sort_and_invalid_document_denied(self):
        for mutate in (lambda r: r['hits']['hits'][0].update(_index='foreign'),
                       lambda r: r['hits']['hits'][0].update(_id='2'),
                       lambda r: r['hits']['hits'][0].update(sort=[2]),
                       lambda r: r['hits']['hits'][0].update(sort=[]),
                       lambda r: r['hits']['hits'][0].update(_source=[]),
                       lambda r: r['hits']['hits'][0]['_source'].update(number=float('nan'))):
            value = response()
            mutate(value)
            with self.subTest(mutation=mutate), self.assertRaises(EscrowError):
                result_digest(value, 'fixture', body())

    def test_empty_complete_query_result_is_valid(self):
        value = response()
        value['hits'] = {'total': {'value': 0, 'relation': 'eq'}, 'hits': []}
        self.assertEqual(result_digest(value, 'fixture', body())['count'], 0)


class QueryIOTests(unittest.TestCase):
    def setUp(self):
        self.exec = Mock(return_value=_encoded(response()))
        self.io = LoopbackQueryIO(exec_read=self.exec, indices=['fixture'])
        self.credentials = {'elastic_username': 'elastic', 'elastic_password': secrets.token_hex(24)}

    def test_query_and_credentials_only_on_stdin_with_get_no_proxy_or_redirect(self):
        request = body()
        request['query'] = {'term': {'title': 'private"value\\suffix'}}
        self.assertEqual(self.io.query('fixture', request, self.credentials), response())
        args, data = self.exec.call_args.args, self.exec.call_args.kwargs['data']
        self.assertEqual(args[-1], 'http://127.0.0.1:9200/fixture/_search?allow_partial_search_results=false')
        self.assertIn('-q', args)
        self.assertIn('--noproxy', args)
        self.assertNotIn('--location', args)
        self.assertNotIn('private', ' '.join(args))
        self.assertNotIn(self.credentials['elastic_password'], ' '.join(args))
        self.assertIn(b'request = "GET"', data)
        self.assertIn(b'private', data)
        self.assertIn(self.credentials['elastic_password'].encode(), data)

    def test_arbitrary_paths_and_archive_reads_denied(self):
        for path in ('/_all/_search', '/fixture/_delete_by_query', '/fixture/_search'):
            with self.assertRaises(EscrowError):
                self.io.request(path, self.credentials)
        with self.assertRaises(EscrowError):
            self.io.read_archive('/usr/share/elasticsearch/data/snapshot')
        self.exec.assert_not_called()

    def test_query_unsafe_or_unbounded_dsl_denied_before_io(self):
        for mutate in (lambda b: b.update(size=1001), lambda b: b.update(size=True),
                       lambda b: b.update(track_total_hits=1), lambda b: b.update(_source=False),
                       lambda b: b.update(sort=[]), lambda b: b.update(sort=[{'_script': 'asc'}]),
                       lambda b: b.update(query={'script': {'source': 'unreviewed'}}),
                       lambda b: b.update(query={'term': {'title': {'value': 'nested'}}}),
                       lambda b: b.update(aggs={'unapproved': {}})):
            value = body()
            mutate(value)
            with self.subTest(mutation=mutate), self.assertRaises(EscrowError):
                self.io.query('fixture', value, self.credentials)
        self.exec.assert_not_called()

    def test_foreign_wildcard_or_alias_index_denied_before_io(self):
        for index in ('*', 'fixture-alias', 'foreign', '.security-7', []):
            with self.subTest(index_type=type(index).__name__), self.assertRaises(EscrowError):
                self.io.query(index, body(), self.credentials)
        self.exec.assert_not_called()

    def test_duplicate_keys_and_nonjson_response_denied(self):
        for raw in (b'{"hits":{},"hits":{}}', b'[]', b'not-json'):
            self.exec.return_value = raw
            with self.assertRaises(EscrowError):
                self.io.query('fixture', body(), self.credentials)


if __name__ == '__main__':
    unittest.main()
