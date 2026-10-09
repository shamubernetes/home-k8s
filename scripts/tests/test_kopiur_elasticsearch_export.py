"""Consumer-bound capture/export failures, not fabricated provider acceptance."""
import copy
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from kopiur_elasticsearch_escrow import EscrowError
from kopiur_elasticsearch_source import PreparedSnapshotExport
import test_kopiur_elasticsearch_prepared_export as prepared_cases


class ConsumerExportTests(unittest.TestCase):
    def setUp(self):
        self.case = prepared_cases.PreparedExportTests()
        self.case.setUp()
        self.contract, self.contracts = self.case.prepare_query_capture()
        self.case.binding['source_queries'] = self.case.prepared.capture_queries(
            ledger=self.contract['ledger'], backend=self.contract['backend'],
            consumers=self.contract['consumers'], contracts=self.contracts)
        self.case.source.binding = copy.deepcopy(self.case.binding)
        for part in self.case.parts.values():
            part['binding'] = copy.deepcopy(self.case.binding)
        self.case.prepared = PreparedSnapshotExport(self.case.source, repository='fixture', snapshot='generation',
            location='/usr/share/elasticsearch/data/snapshot', indices=['fixture'],
            snapshot_uuid='created-uuid', snapshot_version='8.19.0-8.19.1')
        self.case.calls = []
        self.args = self.contract | {'contracts': self.contracts}
        self.composed = self.case.prepared.prepare_consumers(**self.args)
        self.assertEqual(self.case.calls, [])

    def export(self, encrypt):
        return self.case.prepared.export_consumers(**self.args, encrypt_export=encrypt)

    def change_query_document(self):
        self.case.query_routes['/fixture/_search?allow_partial_search_results=false']['hits']['hits'][0]['_source'] = {'number': 2}

    def drift_on(self, suffix, mutate):
        original = self.case.source.run
        def read(argv, data=None):
            value = original(argv, data=data)
            if argv[-len(suffix):] == suffix:
                mutate()
            return value
        self.case.source.run = read

    def test_complete_native_runtime_configuration_and_consumer_capture(self):
        parts = self.composed.capture(self.case.source.configuration)
        self.assertEqual(parts['native']['data'], self.case.native)
        self.assertEqual(parts['config/elasticsearch.keystore'], self.case.parts['config/elasticsearch.keystore'])
        self.assertEqual(parts['runtime']['binding'], self.case.binding)
        self.assertNotIn('ELASTIC_PASSWORD', json.loads(parts['runtime']['data'])['variables'])
        self.assertGreater(sum(argv[-1].endswith('/_search?allow_partial_search_results=false')
                               for argv, _ in self.case.calls), 2)

    def test_missing_bound_queries_denied_before_io(self):
        self.case.prepared.binding.pop('source_queries')
        with self.assertRaises(EscrowError):
            self.case.prepared.prepare_consumers(**self.args)
        self.assertEqual(self.case.calls, [])

    def test_foreign_query_contract_denied_before_io(self):
        self.contracts[0]['queries'][0]['body']['size'] = 9
        with self.assertRaisesRegex(EscrowError, 'prepared consumer query contracts differ'):
            self.case.prepared.prepare_consumers(**self.args)
        self.assertEqual(self.case.calls, [])

    def test_foreign_index_selection_denied_before_io(self):
        self.case.prepared.adapter.indices = {'foreign'}
        with self.assertRaises(EscrowError):
            self.case.prepared.prepare_consumers(**self.args)
        self.assertEqual(self.case.calls, [])

    def test_no_authority_denies_before_native_config_or_encrypt(self):
        self.case.authority.return_value = False
        encrypt = Mock()
        with self.assertRaises(EscrowError):
            self.export(encrypt)
        self.assertEqual(self.case.calls, [])
        encrypt.assert_not_called()

    def test_native_phase_query_drift_denies_before_runtime(self):
        self.drift_on(['tar', '-C', '/usr/share/elasticsearch/data/snapshot', '-cf', '-', '.'],
                      self.change_query_document)
        with self.assertRaisesRegex(EscrowError, 'bound consumer evidence changed'):
            self.composed.capture(self.case.source.configuration)
        self.assertFalse(any(argv[-1] == '/proc/1/environ' for argv, _ in self.case.calls))

    def test_config_phase_catalog_drift_never_reaches_encrypt(self):
        self.drift_on(['tar', '-C', '/usr/share/elasticsearch/config', '-cf', '-', '.'],
                      lambda: self.case.query_routes['/fixture/_count'].update(count=2))
        encrypt = Mock()
        with self.assertRaisesRegex(EscrowError, 'source catalog changed'):
            self.export(encrypt)
        encrypt.assert_not_called()

    def test_query_drift_during_encrypt_denies_returned_receipt(self):
        encrypt = Mock(side_effect=lambda manifest, parts: self.change_query_document())
        with self.assertRaisesRegex(EscrowError, 'bound consumer evidence changed'):
            self.export(encrypt)
        encrypt.assert_called_once()

    def test_bound_export_requires_explicit_caller_contracts(self):
        encrypt = Mock()
        with self.assertRaisesRegex(EscrowError, 'explicit bound consumer export contracts required'):
            self.case.prepared.export(encrypt_export=encrypt)
        encrypt.assert_not_called()

    def test_runtime_restart_denies_before_configuration(self):
        self.drift_on(['cat', '/proc/1/environ'], lambda: self.case.pod['status']['containerStatuses'][0].update(restartCount=1))
        with self.assertRaisesRegex(EscrowError, 'lifetime or credential-provider version changed'):
            self.composed.capture(self.case.source.configuration)
        self.assertFalse(any(argv[-6:] == ['tar', '-C', '/usr/share/elasticsearch/config', '-cf', '-', '.']
                             for argv, _ in self.case.calls))

    def test_exporter_failure_redacted_without_fake_ciphertext(self):
        for error in (RuntimeError('PRIVATE source'), EscrowError('PRIVATE credentials')):
            encrypt = Mock(side_effect=error)
            with self.subTest(error_type=type(error).__name__), self.assertRaisesRegex(
                    EscrowError, '^bound consumer capture/export operation failed$') as failure:
                self.export(encrypt)
            self.assertIsNone(failure.exception.__cause__)
            self.assertTrue(failure.exception.__suppress_context__)
            encrypt.assert_called_once()

    def test_lower_level_bound_export_denied_before_source_io(self):
        encrypt = Mock()
        with self.assertRaisesRegex(EscrowError, 'explicit bound consumer export contracts required'):
            self.case.source.export_snapshot(self.case.prepared.adapter, encrypt_export=encrypt)
        self.assertEqual(self.case.calls, [])
        encrypt.assert_not_called()


if __name__ == '__main__':
    unittest.main()
