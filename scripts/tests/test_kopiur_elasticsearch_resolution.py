"""ARC-only native resolution regressions. Mock responses are not engine proof."""
import copy
from pathlib import Path
import secrets
import sys
import unittest
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from elasticsearch_restore_cases import restore_inputs
from kopiur_elasticsearch_escrow import EscrowError, _digest, _encoded
from kopiur_elasticsearch_resolution import (
    IndexResolution, LoopbackResolutionIO, resolution_path, validate_contracts,
)


def selection(application, selectors, required):
    return {'application': application, 'store': 'elasticsearch-indices:' + application,
            'selectors': selectors, 'required_indices': required,
            'release': {'image': 'example.test/consumer@sha256:' + 'a' * 64,
                        'source_revision': 'b' * 40},
            'runtime': {'pod_uid': application + '-pod', 'container_id': application + '-container',
                'started_at': '2026-10-08T00:00:00Z', 'restart_count': 0,
                'selection_sha256': _digest(_encoded({'selectors': selectors, 'required_indices': required}))}}


def native(names):
    return {'indices': [{'name': n, 'attributes': ['open'], 'aliases': []} for n in names],
            'aliases': [], 'data_streams': []}


class ResolutionTests(unittest.TestCase):
    def setUp(self):
        self.binding = restore_inputs()[0]['binding']
        self.contracts = [selection('media/tubearchivist', ['ta_*'], []),
            selection('services/zoo-cowbell', ['cowbell-media-v4', 'cowbell-media-v4-state'],
                      ['cowbell-media-v4', 'cowbell-media-v4-state'])]
        self.ledger = {'applications': [{'id': c['application'], 'state_dependencies': [c['store']]}
                                       for c in self.contracts],
            'physical_stores': [{'id': c['store'], 'kind': 'external_elasticsearch_indices',
                'backend_contract': 'database/elasticsearch', 'consumer_contracts': [c['application']]}
                for c in self.contracts]}
        self.routes = {resolution_path(self.contracts[0]['selectors']): native(['ta_video', 'ta_config']),
            resolution_path(self.contracts[1]['selectors']): native(['cowbell-media-v4', 'cowbell-media-v4-state'])}
        self.credentials = {'elastic_username': 'elastic', 'elastic_password': secrets.token_hex(24)}
        self.guard = Mock(return_value=True)
        self.request = Mock(side_effect=lambda path, credentials: copy.deepcopy(self.routes[path]))
        self.resolution = self.build()

    def build(self):
        return IndexResolution(self.binding, ledger=self.ledger, backend='database/elasticsearch',
            contracts=self.contracts, guard=self.guard, request=self.request)

    def test_complete_two_consumer_expansion_preserves_generation_state(self):
        result = self.resolution.capture(self.credentials)
        self.assertEqual(result['indices'], ['cowbell-media-v4', 'cowbell-media-v4-state', 'ta_config', 'ta_video'])
        self.assertEqual(len(result['consumers']), 2)
        self.assertIs(result['production_acceptance'], False)
        self.assertEqual(self.request.call_count, 4)
        for args, _ in self.guard.call_args_list:
            self.assertEqual(args[0], validate_contracts(self.contracts))
        self.assertNotIn(self.credentials['elastic_password'], _encoded(result).decode())

    def test_fresh_expansion_includes_previously_unknown_prefix_index(self):
        self.routes[resolution_path(['ta_*'])]['indices'].append(native(['ta_new'])['indices'][0])
        self.assertIn('ta_new', self.resolution.capture(self.credentials)['indices'])

    def test_no_consumer_authority_means_no_native_io(self):
        for value in (None, False, 1):
            self.guard.return_value = value
            with self.subTest(value=value), self.assertRaises(EscrowError):
                self.resolution.capture(self.credentials)
        self.request.assert_not_called()

    def test_revocation_after_every_native_read_discards_response(self):
        for point in range(1, 5):
            self.setUp()
            # Each observe ends with a check, in addition to checks around I/O.
            successful_checks = {1: 1, 2: 3, 3: 6, 4: 8}[point]
            self.guard.side_effect = [True] * successful_checks + [False]
            with self.subTest(point=point), self.assertRaises(EscrowError):
                self.resolution.capture(self.credentials)
            self.assertEqual(self.request.call_count, point)

    def test_new_index_between_passes_fails_closed(self):
        calls = 0
        def request(path, credentials):
            nonlocal calls
            calls += 1
            if calls == 3:
                self.routes[path]['indices'].append(native(['ta_new'])['indices'][0])
            return copy.deepcopy(self.routes[path])
        self.resolution.request = Mock(side_effect=request)
        with self.assertRaisesRegex(EscrowError, 'changed during capture'):
            self.resolution.capture(self.credentials)

    def test_missing_generation_state_fails_closed(self):
        path = resolution_path(self.contracts[1]['selectors'])
        self.routes[path] = native(['cowbell-media-v4'])
        with self.assertRaisesRegex(EscrowError, 'generation/state index absent'):
            self.resolution.capture(self.credentials)

    def test_foreign_system_closed_duplicate_or_empty_index_denied(self):
        for payload in (native(['foreign']), native(['.security-7']), native(['ta_video', 'ta_video']),
                        native([]), native(['ta_*']), native(['ta_video/other'])):
            self.routes[resolution_path(['ta_*'])] = payload
            with self.subTest(payload=payload), self.assertRaises(EscrowError):
                self.resolution.capture(self.credentials)
        self.routes[resolution_path(['ta_*'])] = native(['ta_video'])
        self.routes[resolution_path(['ta_*'])]['indices'][0]['attributes'] = ['closed']
        with self.assertRaises(EscrowError):
            self.resolution.capture(self.credentials)

    def test_alias_data_stream_extra_or_malformed_response_denied(self):
        for payload in ({}, [], native(['ta_video']) | {'aliases': [{'name': 'ta_alias'}]},
                        native(['ta_video']) | {'data_streams': [{'name': 'ta_stream'}]},
                        native(['ta_video']) | {'extra': True}):
            self.routes[resolution_path(['ta_*'])] = payload
            with self.subTest(payload=payload), self.assertRaises(EscrowError):
                self.resolution.capture(self.credentials)

    def test_matching_alias_metadata_cannot_add_indices(self):
        path = resolution_path(['ta_*'])
        self.routes[path]['aliases'] = [{'name': 'ta_current', 'indices': ['ta_video']}]
        self.routes[path]['indices'][0]['aliases'] = ['ta_current']
        self.assertEqual(self.resolution.capture(self.credentials)['indices'],
                         ['cowbell-media-v4', 'cowbell-media-v4-state', 'ta_config', 'ta_video'])

    def test_alias_targets_cannot_widen_selection_or_replace_physical_indices(self):
        for alias in ({'name': 'ta_current', 'indices': ['foreign']},
                      {'name': 'ta_current', 'indices': ['ta_unselected']},
                      {'name': 'ta_current', 'indices': []},
                      {'name': 'ta_current', 'indices': ['ta_video', 'ta_video']},
                      {'name': 'ta_current', 'indices': ['.security-7']},
                      {'name': 'foreign', 'indices': ['ta_video']},
                      {'name': 'ta_video', 'indices': ['ta_video']},
                      {'name': 'ta_current', 'indices': ['ta_video'], 'extra': True}):
            self.setUp()
            self.routes[resolution_path(['ta_*'])]['aliases'] = [alias]
            with self.subTest(alias=alias), self.assertRaises(EscrowError):
                self.resolution.capture(self.credentials)
        self.setUp()
        self.routes[resolution_path(['ta_*'])] = native([]) | {
            'aliases': [{'name': 'ta_current', 'indices': ['ta_video']}]}
        with self.assertRaises(EscrowError):
            self.resolution.capture(self.credentials)

    def test_duplicate_alias_names_denied(self):
        self.routes[resolution_path(['ta_*'])]['aliases'] = [
            {'name': 'ta_current', 'indices': ['ta_video']}] * 2
        with self.assertRaises(EscrowError):
            self.resolution.capture(self.credentials)

    def test_external_errors_redacted_including_escrowerror(self):
        for error in (RuntimeError(self.credentials['elastic_password']), EscrowError('PRIVATE')):
            self.resolution.request = Mock(side_effect=error)
            with self.subTest(error=type(error).__name__), self.assertRaisesRegex(
                    EscrowError, '^authenticated native index resolution failed$') as caught:
                self.resolution.capture(self.credentials)
            self.assertTrue(caught.exception.__suppress_context__)
            self.assertIsNone(caught.exception.__cause__)

    def test_missing_or_extra_roster_denied_before_io(self):
        for mutation in (lambda: self.contracts.pop(),
                         lambda: self.ledger['applications'][0].update(state_dependencies=[]),
                         lambda: self.ledger['physical_stores'].append(copy.deepcopy(self.ledger['physical_stores'][0]))):
            self.setUp()
            mutation()
            with self.assertRaises(EscrowError):
                self.build()
            self.request.assert_not_called()

    def test_invalid_unbound_source_selection_denied_before_io(self):
        mutations = [lambda c: c['release'].update(image='example.test/consumer:latest'),
            lambda c: c['release'].update(source_revision='unknown'),
            lambda c: c['runtime'].update(restart_count=True),
            lambda c: c['runtime'].update(started_at=''),
            lambda c: c['runtime'].update(selection_sha256='a' * 64),
            lambda c: c.update(selectors=['*']), lambda c: c.update(selectors=['ta_*_other']),
            lambda c: c.update(selectors=['ta_?']), lambda c: c.update(selectors=['/_security']),
            lambda c: c.update(selectors=['ta_*', 'ta_*']),
            lambda c: c.update(required_indices=['foreign'])]
        for mutate in mutations:
            self.setUp()
            mutate(self.contracts[0])
            with self.subTest(mutate=mutate), self.assertRaises(EscrowError):
                self.build()
            self.request.assert_not_called()

    def test_exact_selection_cannot_omit_state_requirement(self):
        self.contracts[1]['required_indices'].pop()
        with self.assertRaisesRegex(EscrowError, 'generation/state index coverage differs'):
            self.build()
        self.request.assert_not_called()

    def test_result_inputs_and_guard_arguments_do_not_alias(self):
        captured = self.resolution.capture(self.credentials)
        self.contracts[0]['selectors'][0] = 'foreign*'
        self.ledger.clear()
        captured['consumers'].clear()
        def guard(contracts):
            contracts.clear()
            return True
        self.resolution.guard = Mock(side_effect=guard)
        self.assertEqual(len(self.resolution.capture(self.credentials)['consumers']), 2)

    def test_prepared_expansion_revalidation_detects_new_indices(self):
        captured = self.resolution.capture(self.credentials)
        self.routes[resolution_path(['ta_*'])]['indices'].append(native(['ta_new'])['indices'][0])
        with self.assertRaisesRegex(EscrowError, 'prepared native consumer index expansion changed'):
            self.resolution.verify(captured, self.credentials)

    def test_no_alias_field_native_response_is_accepted(self):
        for payload in self.routes.values():
            for item in payload['indices']:
                del item['aliases']
        self.assertEqual(len(self.resolution.capture(self.credentials)['indices']), 4)

    def test_revoked_checkpoint_denies_credential_acquisition_and_native_io(self):
        expected = self.resolution.capture(self.credentials)
        self.request.reset_mock()
        self.guard.return_value = False
        read_credentials = Mock(return_value=self.credentials)
        with self.assertRaises(EscrowError):
            self.resolution.revalidate(expected, read_credentials)
        read_credentials.assert_not_called()
        self.request.assert_not_called()

    def test_revocation_during_credentials_discards_returned_value_before_native_io(self):
        expected = self.resolution.capture(self.credentials)
        self.request.reset_mock()
        def read():
            self.guard.return_value = False
            return self.credentials
        with self.assertRaises(EscrowError):
            self.resolution.revalidate(expected, read)
        self.request.assert_not_called()

    def test_external_authority_errors_are_redacted(self):
        self.guard.side_effect = EscrowError('PRIVATE')
        with self.assertRaisesRegex(EscrowError, '^engine and consumer selection authority check failed$') as caught:
            self.resolution.capture(self.credentials)
        self.assertTrue(caught.exception.__suppress_context__)
        self.request.assert_not_called()

    def test_native_order_does_not_change_resolution(self):
        captured = self.resolution.capture(self.credentials)
        for payload in self.routes.values():
            payload['indices'].reverse()
        self.assertIs(self.resolution.verify(captured, self.credentials), True)


class ResolutionTransportTests(unittest.TestCase):
    def setUp(self):
        self.contract = selection('fixture/reader', ['fixture*'], ['fixture'])
        self.execute = Mock(return_value=_encoded(native(['fixture'])))
        self.io = LoopbackResolutionIO(exec_read=self.execute, contracts=[self.contract])
        self.credentials = {'elastic_username': 'elastic', 'elastic_password': secrets.token_hex(24)}

    def test_authenticated_loopback_resolution_uses_stdin_only(self):
        self.assertEqual(self.io.request(resolution_path(['fixture*']), self.credentials), native(['fixture']))
        args, kwargs = self.execute.call_args
        self.assertEqual(args[-1], 'http://127.0.0.1:9200' + resolution_path(['fixture*']))
        self.assertIn('--noproxy', args)
        self.assertIn('-q', args)
        self.assertNotIn(self.credentials['elastic_password'], repr(args))
        self.assertIn(self.credentials['elastic_password'].encode(), kwargs['data'])

    def test_unqualified_path_url_write_or_archive_denied_before_io(self):
        for path in ('/_resolve/index/*?expand_wildcards=all', '/_resolve/index/foreign',
                     '/_snapshot/fixture/generation/_restore', 'http://example.test/', None):
            with self.subTest(path=path), self.assertRaises(EscrowError):
                self.io.request(path, self.credentials)
        with self.assertRaises(EscrowError):
            self.io.read_archive('/private')
        self.execute.assert_not_called()

    def test_duplicate_response_keys_denied(self):
        self.execute.return_value = b'{"indices":[],"indices":[]}'
        with self.assertRaises(EscrowError):
            self.io.request(resolution_path(['fixture*']), self.credentials)


class OriginalResolvedExportTests(unittest.TestCase):
    def setUp(self):
        from test_kopiur_elasticsearch_export import ConsumerExportTests
        self.helper = ConsumerExportTests()
        self.helper.setUp()
        self.case = self.helper.case
        self.selections = [selection('fixture/reader', ['fixture*'], ['fixture'])]
        self.consumer_authority = Mock(return_value=True)
        self.payload = native(['fixture'])
        original = self.case.source.run
        def read(argv, data=None):
            if argv[-1] == 'http://127.0.0.1:9200' + resolution_path(['fixture*']):
                self.case.calls.append((argv, data))
                return _encoded(self.payload)
            return original(argv, data=data)
        self.case.source.run = read
        self.args = {'ledger': self.helper.contract['ledger'],
            'backend': self.helper.contract['backend'], 'selections': self.selections,
            'require_consumer_authority': self.consumer_authority}

    def export(self, encrypt):
        return self.case.prepared.export_resolved_consumers(**self.args,
            contracts=self.helper.contracts, encrypt_export=encrypt)

    def test_original_resolution_uses_source_guard_and_complete_snapshot_selection(self):
        _, result = self.case.prepared.capture_resolution(**self.args)
        self.assertEqual(result['indices'], ['fixture'])
        self.assertEqual(result['consumers'], self.helper.contract['consumers'])
        self.assertGreater(self.case.authority.call_count, 0)
        self.assertGreater(self.consumer_authority.call_count, 0)

    def test_consumer_guard_denial_before_any_original_source_io(self):
        self.consumer_authority.return_value = False
        encrypt = Mock()
        with self.assertRaisesRegex(EscrowError, 'consumer selection authority required'):
            self.export(encrypt)
        self.assertFalse(any(argv[1] == 'exec' or argv[-1] == 'jsonpath={.data.ELASTIC_PASSWORD}'
                             for argv, _ in self.case.calls))
        encrypt.assert_not_called()

    def test_source_guard_denial_before_any_original_source_io(self):
        self.case.authority.return_value = False
        with self.assertRaises(EscrowError):
            self.case.prepared.capture_resolution(**self.args)
        self.assertEqual(self.case.calls, [])

    def test_missing_consumer_selection_denies_credentials_and_native_io(self):
        self.selections.clear()
        with self.assertRaises(EscrowError):
            self.case.prepared.capture_resolution(**self.args)
        self.assertEqual(self.case.calls, [])

    def test_revocation_after_initial_resolution_denies_checkpoint_credentials(self):
        capture = self.case.prepared.capture_resolution
        def revoke(**args):
            value = capture(**args)
            self.consumer_authority.return_value = False
            self.case.calls.clear()
            return value
        self.case.prepared.capture_resolution = Mock(side_effect=revoke)
        encrypt = Mock()
        with self.assertRaises(EscrowError):
            self.export(encrypt)
        self.assertFalse(any(argv[1] == 'exec' or argv[-1] == 'jsonpath={.data.ELASTIC_PASSWORD}'
                             for argv, _ in self.case.calls))
        encrypt.assert_not_called()

    def test_snapshot_must_include_every_new_prefix_index(self):
        self.payload['indices'].append(native(['fixture-new'])['indices'][0])
        with self.assertRaisesRegex(EscrowError, 'snapshot differs from fresh complete native expansion'):
            self.case.prepared.capture_resolution(**self.args)

    def test_native_phase_new_index_denies_before_runtime_and_encryption(self):
        self.helper.drift_on(['tar', '-C', '/usr/share/elasticsearch/data/snapshot', '-cf', '-', '.'],
            lambda: self.payload['indices'].append(native(['fixture-new'])['indices'][0]))
        encrypt = Mock()
        with self.assertRaisesRegex(EscrowError, 'prepared native consumer index expansion changed'):
            self.export(encrypt)
        self.assertFalse(any(argv[-1] == '/proc/1/environ' for argv, _ in self.case.calls))
        encrypt.assert_not_called()

    def test_configuration_phase_consumer_restart_denies_encryption(self):
        self.helper.drift_on(['tar', '-C', '/usr/share/elasticsearch/config', '-cf', '-', '.'],
            lambda: setattr(self.consumer_authority, 'return_value', False))
        encrypt = Mock()
        with self.assertRaisesRegex(EscrowError, 'consumer selection authority required'):
            self.export(encrypt)
        encrypt.assert_not_called()

    def test_encryption_phase_expansion_drift_discards_receipt(self):
        encrypt = Mock(side_effect=lambda manifest, parts:
            self.payload['indices'].append(native(['fixture-new'])['indices'][0]))
        with self.assertRaisesRegex(EscrowError, 'prepared native consumer index expansion changed'):
            self.export(encrypt)
        encrypt.assert_called_once()

    def test_original_consumer_adapter_required_before_source_io(self):
        with self.assertRaisesRegex(EscrowError, 'concrete original consumer authority'):
            self.case.prepared.export_original_consumers(ledger=self.args['ledger'],
                backend=self.args['backend'], consumer_authority=Mock(),
                contracts=self.helper.contracts, encrypt_export=Mock())
        self.assertEqual(self.case.calls, [])

    def test_original_source_authority_denies_concrete_consumer_reads(self):
        from test_kopiur_elasticsearch_consumer_authority import AuthorityTests
        consumer = AuthorityTests(); consumer.setUp()
        self.case.authority.return_value = False
        with self.assertRaises(EscrowError):
            self.case.prepared.export_original_consumers(ledger=self.args['ledger'],
                backend=self.args['backend'], consumer_authority=consumer.authority,
                contracts=self.helper.contracts, encrypt_export=Mock())
        self.assertEqual(consumer.calls, [])
        self.assertEqual(self.case.calls, [])

    def test_original_source_revocation_during_consumer_prepare_denies_process_reads(self):
        from test_kopiur_elasticsearch_consumer_authority import AuthorityTests
        consumer = AuthorityTests(); consumer.setUp()
        original = consumer.run_read
        def revoke(argv, data=None):
            result = original(argv, data)
            if argv[:3] == ['kubectl', 'get', 'pods']:
                self.case.authority.return_value = False
            return result
        consumer.authority.run = revoke
        with self.assertRaises(EscrowError):
            self.case.prepared.export_original_consumers(ledger=self.args['ledger'],
                backend=self.args['backend'], consumer_authority=consumer.authority,
                contracts=self.helper.contracts, encrypt_export=Mock())
        self.assertEqual(consumer.exec_calls(), [])
        self.assertFalse(any(argv[1] == 'exec' or argv[-1] == 'jsonpath={.data.ELASTIC_PASSWORD}'
                             for argv, _ in self.case.calls))

    def test_missing_original_consumer_provenance_denies_source_credentials(self):
        from test_kopiur_elasticsearch_consumer_authority import AuthorityTests
        consumer = AuthorityTests(); consumer.setUp()
        consumer.config['config']['Labels'] = {}
        with self.assertRaisesRegex(EscrowError, 'provenance absent'):
            self.case.prepared.export_original_consumers(ledger=self.args['ledger'],
                backend=self.args['backend'], consumer_authority=consumer.authority,
                contracts=self.helper.contracts, encrypt_export=Mock())
        self.assertEqual(consumer.exec_calls(), [])
        self.assertFalse(any(argv[1] == 'exec' or argv[-1] == 'jsonpath={.data.ELASTIC_PASSWORD}'
                             for argv, _ in self.case.calls))

    def test_resolved_export_reaches_only_explicit_encrypt_boundary(self):
        encrypt = Mock(side_effect=RuntimeError('PRIVATE'))
        with self.assertRaisesRegex(EscrowError, '^bound consumer capture/export operation failed$'):
            self.export(encrypt)
        encrypt.assert_called_once()
        self.assertGreater(sum(argv[-1].endswith(resolution_path(['fixture*']))
                               for argv, _ in self.case.calls), 4)


if __name__ == '__main__':
    unittest.main()
