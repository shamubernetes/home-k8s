"""ARC-only preparation admission. Real engine restore is a separate provider gate."""
import copy
import json
from pathlib import Path
import secrets
import sys
import unittest
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from kopiur_elasticsearch_escrow import EscrowError, _encoded, _validate_parts
from kopiur_elasticsearch_restore import RestorePlan
from kopiur_elasticsearch_engine_fixture import EngineRestore, fixture_consumer_plan
from elasticsearch_restore_cases import restore_inputs


class RestorePlanTests(unittest.TestCase):
    def setUp(self):
        self.manifest, self.parts, self.runtime, self.credentials = restore_inputs()
        self.choices = {'replay': list(self.runtime['variables']), 'omit': {}, 'repository': 'fixture',
                        'snapshot': 'generation', 'location': '/usr/share/elasticsearch/data/snapshot',
                        'indices': ['fixture'], **fixture_consumer_plan()}

    def plan(self):
        return RestorePlan(self.manifest, self.parts, **self.choices)

    def refresh_runtime(self):
        self.parts['runtime']['data'] = _encoded(self.runtime)
        self.manifest['entries'] = _validate_parts(self.manifest['binding'], self.parts)

    def test_exact_runtime_native_and_security_plan(self):
        plan = self.plan()
        self.assertEqual(plan.runtime, self.runtime)
        self.assertEqual(plan.native_request(), {'indices': 'fixture', 'include_global_state': True,
                                                'feature_states': ['security']})
        receipt = plan.receipt()
        self.assertRegex(receipt.pop('consumer_inventory_sha256'), r'^[0-9a-f]{64}$')
        self.assertEqual(receipt, {'generation': 'a' * 32, 'runtime_replayed_count': 7,
            'runtime_omitted_count': 0, 'native_index_count': 1,
            'consumer_contract_count': 1, 'consumer_queries_verified': False,
            'production_acceptance': False, 'source_admission_released': False})
        self.assertNotIn(self.credentials['elastic_password'], json.dumps(plan.receipt()))

    def test_omission_requires_explicit_reason_and_preserves_original_bytes(self):
        self.runtime['variables']['ELASTIC_PASSWORD'] = self.credentials['elastic_password']
        self.refresh_runtime()
        before = copy.deepcopy(self.parts)
        self.choices['omit'] = {'ELASTIC_PASSWORD': 'Restore captured bootstrap keystore instead'}
        plan = self.plan()
        self.assertNotIn('ELASTIC_PASSWORD', plan.runtime['variables'])
        self.assertEqual(self.parts, before)
        self.assertEqual(plan.receipt()['runtime_omitted_count'], 1)

    def test_missing_duplicated_overlapping_foreign_or_unreasoned_dispositions_denied(self):
        variants = [self.choices | {'replay': self.choices['replay'][:-1]},
                    self.choices | {'replay': self.choices['replay'] * 2},
                    self.choices | {'omit': {'path.repo': 'overlap'}},
                    self.choices | {'omit': {'missing': 'not captured'}},
                    self.choices | {'replay': self.choices['replay'][:-1], 'omit': {'path.repo': ''}},
                    self.choices | {'replay': None}, self.choices | {'omit': None}]
        for choices in variants:
            with self.subTest(choices=choices), self.assertRaises(EscrowError):
                RestorePlan(self.manifest, self.parts, **choices)

    def test_bootstrap_or_configuration_root_replacement_denied(self):
        for name in ('ELASTIC_PASSWORD', 'ELASTIC_PASSWORD_FILE', 'ES_PATH_CONF'):
            self.setUp()
            self.runtime['variables'][name] = secrets.token_hex(24)
            self.choices['replay'].append(name)
            self.refresh_runtime()
            with self.subTest(name=name), self.assertRaisesRegex(EscrowError, 'must not be replaced'):
                self.plan()

    def test_incomplete_altered_or_mixed_components_denied_before_target_creation(self):
        for mutation in ('omitted', 'bytes', 'mode', 'binding', 'extra', 'manifest'):
            self.setUp()
            if mutation == 'omitted':
                del self.parts['config/elasticsearch.keystore']
            elif mutation == 'bytes':
                self.parts['config/elasticsearch.keystore']['data'] += b'changed'
            elif mutation == 'mode':
                self.parts['config/elasticsearch.keystore']['mode'] = 0o644
            elif mutation == 'binding':
                self.parts['config/elasticsearch.keystore']['binding']['generation'] = 'd' * 32
            elif mutation == 'extra':
                self.parts['extra'] = copy.deepcopy(self.parts['native'])
            else:
                self.manifest['entries']['native']['sha256'] = 'd' * 64
            drill = Mock()
            with self.subTest(mutation=mutation), self.assertRaises(EscrowError):
                EngineRestore(drill, 'nas', self.manifest, self.parts, Mock(), [],
                    consumer_credentials={'username': 'fixture-reader', 'password': secrets.token_hex(24)})
            drill.create.assert_not_called()
            drill.run.assert_not_called()

    def test_binding_runtime_mismatch_denied_even_with_consistent_manifest_hashes(self):
        for field in ('image', 'version'):
            self.setUp()
            self.runtime[field] += '-other'
            self.refresh_runtime()
            with self.subTest(field=field), self.assertRaisesRegex(EscrowError, 'canonical restore runtime'):
                self.plan()

    def test_noncanonical_ambiguous_or_malformed_runtime_denied(self):
        raw = self.parts['runtime']['data']
        for data in (raw + b'\n', raw + raw, b'[]', b'\xff',
                     raw[:-1] + b',"version":"8.19.23"}'):
            self.parts['runtime']['data'] = data
            self.manifest['entries'] = _validate_parts(self.manifest['binding'], self.parts)
            with self.subTest(size=len(data)), self.assertRaisesRegex(EscrowError, 'canonical restore runtime'):
                self.plan()

    def test_native_selection_has_no_wildcards_security_substitution_or_traversal(self):
        variants = [{'repository': '../other'}, {'snapshot': '*'}, {'indices': ['*']},
                    {'indices': ['fixture', 'fixture']}, {'indices': ['.security-7']},
                    {'indices': []}, {'location': '/usr/share/elasticsearch/data/snapshot/../other'},
                    {'location': '/usr/share/elasticsearch/data/snapshot-unapproved'}]
        for changes in variants:
            with self.subTest(changes=changes), self.assertRaises(EscrowError):
                RestorePlan(self.manifest, self.parts, **(self.choices | changes))

    def test_repository_path_must_be_replayed_without_rewrite(self):
        self.choices['location'] += '/other'
        with self.assertRaisesRegex(EscrowError, 'repository path must match'):
            self.plan()
        self.choices['location'] = self.runtime['variables']['path.repo']
        self.choices['replay'].remove('path.repo')
        self.choices['omit']['path.repo'] = 'cannot omit and still access native data'
        with self.assertRaisesRegex(EscrowError, 'repository path must match'):
            self.plan()

    def test_post_preparation_mutation_denied_and_properties_cannot_rewrite_plan(self):
        plan = self.plan()
        plan.runtime['variables']['discovery.type'] = 'other'
        plan.selection['indices'].append('other')
        self.choices['indices'].append('other')
        self.assertEqual(plan.native_request()['indices'], 'fixture')
        self.assertEqual(plan.runtime, self.runtime)
        self.parts['native']['data'] = b'changed'
        with self.assertRaisesRegex(EscrowError, 'prepared restore generation changed'):
            plan.check(self.manifest, self.parts)

    def test_invalid_credentials_denied_during_preparation(self):
        self.parts['credentials']['data'] = _encoded({'username': 'elastic', 'password': secrets.token_hex(24)})
        self.manifest['entries'] = _validate_parts(self.manifest['binding'], self.parts)
        with self.assertRaisesRegex(EscrowError, 'exact-service credentials invalid'):
            self.plan()

    def shared_search(self):
        # Original consumer identities, synthetic catalog only. This is not a
        # current production catalog or acceptance of either application.
        roster = [('media/tubearchivist', 'ta_synthetic'), ('services/zoo-cowbell', 'cowbell_synthetic')]
        stores, apps, records = [], [], []
        for app, index in roster:
            name = 'elasticsearch-indices:' + app
            stores.append({'id': name, 'kind': 'external_elasticsearch_indices',
                           'backend_contract': 'database/elasticsearch', 'consumer_contracts': [app]})
            apps.append({'id': app, 'state_dependencies': [name]})
            records.append({'application': app, 'store': name, 'indices': [index]})
        self.choices.update(ledger={'applications': apps, 'physical_stores': stores},
                            consumers=records, indices=[i for _, i in roster])

    def test_shared_search_requires_both_declared_original_consumers(self):
        self.shared_search()
        plan = self.plan()
        receipt = plan.receipt()
        self.assertEqual(receipt['consumer_contract_count'], 2)
        self.assertRegex(receipt['consumer_inventory_sha256'], r'^[0-9a-f]{64}$')
        self.assertFalse(receipt['consumer_queries_verified'])
        self.assertFalse(receipt['production_acceptance'])
        self.assertEqual(plan.native_request()['indices'], 'ta_synthetic,cowbell_synthetic')

    def test_missing_foreign_duplicate_or_incomplete_consumer_coverage_denied(self):
        for mutation in ('missing', 'foreign', 'duplicate', 'incomplete', 'wildcard', 'empty'):
            self.shared_search()
            records = self.choices['consumers']
            if mutation == 'missing':
                records.pop()
            elif mutation == 'foreign':
                records[1]['application'] = 'fixture/foreign'
            elif mutation == 'duplicate':
                records.append(copy.deepcopy(records[0]))
            elif mutation == 'incomplete':
                records[1]['indices'] = ['ta_synthetic']
            elif mutation == 'wildcard':
                records[1]['indices'] = ['cowbell_*']
            else:
                records[1]['indices'] = []
            with self.subTest(mutation=mutation), self.assertRaises(EscrowError):
                self.plan()

    def test_shared_index_requires_each_consumer_without_forcing_disjointness(self):
        self.shared_search()
        self.choices['indices'] = ['ta_synthetic']
        self.choices['consumers'][1]['indices'] = ['ta_synthetic']
        self.assertEqual(self.plan().receipt()['consumer_contract_count'], 2)
        self.choices['consumers'].pop()
        with self.assertRaises(EscrowError):
            self.plan()

    def test_asymmetric_duplicate_or_malformed_ledger_refused(self):
        for mutation in ('reverse', 'forward', 'duplicate_store', 'conflicting_backend',
                         'conflicting_kind', 'duplicate_app', 'duplicate_contract',
                         'no_stores', 'bad_dependencies', 'bad_app', 'bad_store', 'nonserializable'):
            self.shared_search()
            ledger = self.choices['ledger']
            if mutation == 'reverse':
                ledger['applications'].append({'id': 'fixture/undeclared',
                    'state_dependencies': [ledger['physical_stores'][0]['id']]})
            elif mutation == 'forward':
                ledger['applications'][0]['state_dependencies'] = []
            elif mutation == 'duplicate_store':
                ledger['physical_stores'].append(copy.deepcopy(ledger['physical_stores'][0]))
            elif mutation in ('conflicting_backend', 'conflicting_kind'):
                duplicate = copy.deepcopy(ledger['physical_stores'][0])
                duplicate['backend_contract' if mutation == 'conflicting_backend' else 'kind'] = 'other'
                ledger['physical_stores'].append(duplicate)
            elif mutation == 'duplicate_app':
                ledger['applications'].append(copy.deepcopy(ledger['applications'][0]))
            elif mutation == 'duplicate_contract':
                ledger['physical_stores'][0]['consumer_contracts'] *= 2
            elif mutation == 'no_stores':
                ledger['physical_stores'] = []
            elif mutation == 'bad_dependencies':
                ledger['applications'][1]['state_dependencies'] = [None]
            elif mutation == 'bad_app':
                ledger['applications'].append(None)
            elif mutation == 'nonserializable':
                ledger['unrelated_policy'] = {object()}
            else:
                ledger['physical_stores'].append(None)
            with self.subTest(mutation=mutation), self.assertRaises(EscrowError):
                self.plan()

    def test_inventory_receipt_bound_to_source_generation_and_ledger(self):
        self.shared_search()
        first = self.plan().receipt()['consumer_inventory_sha256']
        ledger = self.choices['ledger']
        ledger['physical_stores'][0]['selection'] = 'unresolved until authorized catalog read'
        second = self.plan().receipt()['consumer_inventory_sha256']
        self.assertNotEqual(first, second)
        binding = self.manifest['binding']
        binding['generation'] = 'f' * 32
        for part in self.parts.values():
            part['binding'] = copy.deepcopy(binding)
        self.manifest['entries'] = _validate_parts(binding, self.parts)
        self.assertNotEqual(second, self.plan().receipt()['consumer_inventory_sha256'])


if __name__ == '__main__':
    unittest.main()
