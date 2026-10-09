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
from kopiur_elasticsearch_engine_fixture import EngineRestore
from elasticsearch_restore_cases import restore_inputs


class RestorePlanTests(unittest.TestCase):
    def setUp(self):
        self.manifest, self.parts, self.runtime, self.credentials = restore_inputs()
        self.choices = {'replay': list(self.runtime['variables']), 'omit': {}, 'repository': 'fixture',
                        'snapshot': 'generation', 'location': '/usr/share/elasticsearch/data/snapshot',
                        'indices': ['fixture']}

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
        self.assertEqual(plan.receipt(), {'generation': 'a' * 32, 'runtime_replayed_count': 7,
            'runtime_omitted_count': 0, 'native_index_count': 1,
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


if __name__ == '__main__':
    unittest.main()
