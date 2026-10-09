"""ARC-only original release preparation and no-I/O completeness checks."""
import copy
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from elasticsearch_restore_cases import restore_inputs
from kopiur_elasticsearch_consumer_authority import ConsumerAuthority
from kopiur_elasticsearch_consumer_plan import (
    build_original_plans, preflight_consumer_export, preflight_original_export,
)
from kopiur_elasticsearch_escrow import EscrowError, _digest, _encoded
from kopiur_elasticsearch_catalog import IDENTITY, SCHEMA as CATALOG_SCHEMA
from kopiur_elasticsearch_queries import SCHEMA as QUERY_SCHEMA, contract_digests, query_records
from test_kopiur_elasticsearch_resolution import selection

TA = 'media/tubearchivist'
COW = 'services/zoo-cowbell'


def coverage(selections):
    ledger = {'applications': [{'id': p['application'], 'state_dependencies': [p['store']]}
                              for p in selections],
        'physical_stores': [{'id': p['store'], 'kind': 'external_elasticsearch_indices',
            'backend_contract': 'database/elasticsearch', 'consumer_contracts': [p['application']]}
            for p in selections]}
    body = {'query': {'match_all': {}}, 'sort': [{'number': 'asc'}], 'size': 10,
            'track_total_hits': True, '_source': True}
    queries = [{'application': p['application'], 'store': p['store'],
        'queries': [{'id': 'complete-' + str(i), 'index': name, 'body': copy.deepcopy(body)}
                    for i, name in enumerate(p['required_indices'])]} for p in selections]
    indices = sorted({name for p in selections for name in p['required_indices']})
    return ledger, queries, indices


def bound_test_evidence(binding, ledger, queries, indices):
    """Synthetic empty catalog/results for authority-boundary regressions only."""
    binding = copy.deepcopy(binding)
    consumers = sorted([{'application': q['application'], 'store': q['store'],
        'indices': sorted({query['index'] for query in q['queries']})} for q in queries],
        key=lambda r: (r['application'], r['store']))
    catalog = {'schema': CATALOG_SCHEMA, 'source': {k: binding[k] for k in IDENTITY},
        'ledger_sha256': _digest(_encoded(ledger)), 'consumers': consumers,
        'indices': {i: {'settings': {}, 'mappings': {}, 'aliases': {}, 'count': 0} for i in indices}}
    ordered = query_records(queries, catalog)
    binding['source_catalog'] = catalog
    binding['source_queries'] = {'schema': QUERY_SCHEMA, 'catalog_sha256': _digest(_encoded(catalog)),
        'contracts': contract_digests(ordered),
        'results': [{'application': r['application'], 'store': r['store'], 'id': q['id'],
                    'sha256': _digest(_encoded([])), 'count': 0} for r in ordered for q in r['queries']]}
    return binding


class OriginalPlanTests(unittest.TestCase):
    def setUp(self):
        self.recipes = json.loads((Path(__file__).resolve().parents[1] /
                                  'kopiur-elasticsearch-original-consumers.json').read_text())
        self.checkouts = {TA: '/reviewed/tubearchivist', COW: '/reviewed/cowbell'}
        self.inputs: dict[str, object] = {TA: None, COW: None}

    def build(self):
        return build_original_plans(self.recipes, checkouts=self.checkouts,
                                    startup_selections=self.inputs)

    def test_reviewed_both_releases_include_every_native_state_index(self):
        plans = self.build()
        self.assertEqual([p['application'] for p in plans], [TA, COW])
        self.assertEqual(set(plans[0]['required_indices']), {'ta_config', 'ta_channel',
            'ta_video', 'ta_download', 'ta_playlist', 'ta_subtitle', 'ta_comment'})
        self.assertEqual(plans[1]['selectors'], ['cowbell-media-v4', 'cowbell-media-v4-state'])
        self.assertEqual(len(plans[0]['source_files']), 10)
        self.assertEqual(len(plans[1]['source_files']), 3)

    def test_no_subprocess_registry_process_or_secret_reads_during_build(self):
        with patch('subprocess.run') as run, patch('subprocess.Popen') as popen:
            self.build()
        run.assert_not_called(); popen.assert_not_called()

    def test_explicit_override_preserves_original_raw_startup_input(self):
        self.inputs[COW] = '  custom-catalog  '
        plan = self.build()[1]
        self.assertEqual(plan['expected_selection'], '  custom-catalog  ')
        self.assertEqual(plan['selectors'], ['custom-catalog', 'custom-catalog-state'])

    def test_explicit_blank_uses_reviewed_default_but_is_not_absent_input(self):
        self.inputs[COW] = '   '
        plan = self.build()[1]
        self.assertEqual(plan['expected_selection'], '   ')
        self.assertEqual(plan['selectors'][0], 'cowbell-media-v4')

    def test_partial_duplicate_foreign_or_extra_recipes_denied(self):
        original = copy.deepcopy(self.recipes)
        for change in ('missing', 'duplicate', 'foreign', 'extra', 'store', 'source-type'):
            self.recipes = copy.deepcopy(original)
            r = self.recipes['consumers']
            if change == 'missing': r.pop()
            if change == 'duplicate': r[1] = copy.deepcopy(r[0])
            if change == 'foreign': r[1]['application'] = 'fixture/reader'
            if change == 'extra': r[1]['private'] = 'PRIVATE'
            if change == 'store': r[1]['store'] = 'foreign'
            if change == 'source-type': r[0]['source_files'] = None
            with self.subTest(change=change), self.assertRaises(EscrowError): self.build()

    def test_no_implicit_startup_or_checkout_defaults(self):
        old_input = self.inputs.pop(COW)
        with self.assertRaises(EscrowError): self.build()
        self.inputs[COW] = old_input
        old_checkout = self.checkouts.pop(COW)
        with self.assertRaises(EscrowError): self.build()
        self.checkouts[COW] = old_checkout

    def test_fixed_tubearchivist_selection_cannot_be_rewritten(self):
        for changes in ({'selectors': ['foreign*']}, {'extra': True}):
            with self.subTest(changes=changes):
                recipes = copy.deepcopy(self.recipes)
                recipes['consumers'][0]['selection'].update(changes)
                with self.assertRaises(EscrowError):
                    build_original_plans(recipes, checkouts=self.checkouts, startup_selections=self.inputs)
        self.inputs[TA] = 'ta_*'
        with self.assertRaises(EscrowError): self.build()

    def test_complete_tubearchivist_artifact_witness_required(self):
        for change in ('missing', 'partial', 'digest', 'foreign'):
            recipes = copy.deepcopy(self.recipes)
            ta = recipes['consumers'][0]
            if change == 'missing': ta.pop('source_witness')
            if change == 'partial': ta['source_witness']['files'].pop()
            if change == 'digest': ta['source_witness']['files'][0]['sha256'] = 'a'*64
            if change == 'foreign': ta['source_url'] = 'https://github.com/example/foreign'
            with self.subTest(change=change), self.assertRaises(EscrowError):
                build_original_plans(recipes, checkouts=self.checkouts, startup_selections=self.inputs)

    def test_invalid_cowbell_prefix_never_reaches_io(self):
        for value in (True, 'foreign/*', '.security', '', 'a,b'):
            if value == '': continue  # Explicit empty input has reviewed default semantics.
            self.inputs[COW] = value
            with self.subTest(value=value), self.assertRaises(EscrowError): self.build()

    def test_input_and_returned_plan_mutations_are_independent(self):
        plans = self.build()
        self.recipes['consumers'][0]['selection']['required_indices'].clear()
        self.checkouts[TA] = '/changed'
        self.assertEqual(len(plans[0]['required_indices']), 7)
        self.assertEqual(plans[0]['source_checkout'], '/reviewed/tubearchivist')


class PreflightTests(unittest.TestCase):
    def setUp(self):
        self.binding = restore_inputs()[0]['binding']
        self.selections = [selection(TA, ['ta_*'], ['ta_config', 'ta_video']),
            selection(COW, ['cowbell-media-v4', 'cowbell-media-v4-state'],
                      ['cowbell-media-v4', 'cowbell-media-v4-state'])]
        self.ledger, self.queries, self.indices = coverage(self.selections)

    def check(self):
        return preflight_consumer_export(self.binding, ledger=self.ledger,
            backend='database/elasticsearch', selections=self.selections,
            contracts=self.queries, indices=self.indices)

    def test_complete_original_roster_returns_only_hashes_not_acceptance(self):
        result = self.check()
        self.assertEqual(set(result), {'consumer_inventory_sha256', 'selection_contracts_sha256',
                                      'query_contracts_sha256', 'production_recovery_accepted'})
        self.assertIs(result['production_recovery_accepted'], False)
        self.assertNotIn('number', json.dumps(result))

    def test_permutation_of_indices_queries_and_selections_is_stable(self):
        expected = self.check()
        self.indices.reverse(); self.queries.reverse(); self.selections.reverse()
        for r in self.queries: r['queries'].reverse()
        self.assertEqual(self.check(), expected)

    def test_missing_generation_or_tubearchivist_state_denied(self):
        original = self.indices[:]
        for name in ('cowbell-media-v4-state', 'ta_config'):
            self.indices = [i for i in original if i != name]
            with self.subTest(name=name), self.assertRaises(EscrowError): self.check()

    def test_new_prefix_index_requires_complete_explicit_query_coverage(self):
        self.indices.append('ta_new')
        with self.assertRaises(EscrowError): self.check()
        self.queries[0]['queries'].append({'id': 'new-index', 'index': 'ta_new',
                                          'body': copy.deepcopy(self.queries[0]['queries'][0]['body'])})
        self.check()

    def test_omitted_or_duplicate_query_roster_denied(self):
        original = copy.deepcopy(self.queries)
        for change in ('omit', 'duplicate', 'foreign', 'state', 'body', 'id'):
            self.queries = copy.deepcopy(original)
            if change == 'omit': self.queries.pop()
            if change == 'duplicate': self.queries[1] = copy.deepcopy(self.queries[0])
            if change == 'foreign': self.queries[1]['store'] = 'foreign'
            if change == 'state': self.queries[1]['queries'].pop()
            if change == 'body': self.queries[0]['queries'][0]['body']['size'] = 1001
            if change == 'id': self.queries[0]['queries'][1]['id'] = self.queries[0]['queries'][0]['id']
            with self.subTest(change=change), self.assertRaises(EscrowError): self.check()

    def test_foreign_hidden_duplicate_or_invalid_indices_denied(self):
        original = self.indices[:]
        for name in ('foreign', '.security', self.indices[0], True):
            self.indices = original + [name]
            with self.subTest(name=name), self.assertRaises(EscrowError): self.check()

    def test_missing_reciprocal_dependency_or_duplicate_ledger_denied(self):
        self.ledger['applications'][0]['state_dependencies'].clear()
        with self.assertRaises(EscrowError): self.check()
        self.ledger['applications'][0]['state_dependencies'] = [self.selections[0]['store']]
        self.ledger['applications'].append(copy.deepcopy(self.ledger['applications'][0]))
        with self.assertRaises(EscrowError): self.check()

    def test_mutating_query_body_changes_bound_preflight_digest(self):
        expected = self.check()
        self.queries[0]['queries'][0]['body']['query'] = {'term': {'number': 1}}
        changed = self.check()
        self.assertNotEqual(expected['query_contracts_sha256'], changed['query_contracts_sha256'])
        self.assertEqual(expected['consumer_inventory_sha256'], changed['consumer_inventory_sha256'])

    def test_original_preflight_requires_concrete_adapter(self):
        with self.assertRaises(EscrowError):
            preflight_original_export(self.binding, ledger=self.ledger, backend='database/elasticsearch',
                consumer_authority=Mock(), contracts=self.queries, indices=self.indices)

    def test_complete_recipe_preflight_never_observes_original_source(self):
        recipe_case = OriginalPlanTests(); recipe_case.setUp()
        plans = recipe_case.build()
        run, grant = Mock(), Mock(return_value=False)
        authority = ConsumerAuthority(plans, require_authority=grant, run=run)
        ledger, queries, indices = coverage(plans)
        binding = bound_test_evidence(self.binding, ledger, queries, indices)
        result = preflight_original_export(binding, ledger=ledger, backend='database/elasticsearch',
            consumer_authority=authority, contracts=queries, indices=indices)
        self.assertIs(result['production_recovery_accepted'], False)
        run.assert_not_called(); grant.assert_not_called()
        self.assertIsNone(authority.expected)


if __name__ == '__main__':
    unittest.main()
