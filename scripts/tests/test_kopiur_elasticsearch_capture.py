"""Source capture adapter regressions. Run only on the existing ARC runner.

Synthetic Kubernetes responses test binding, guard ordering and fail-closed
restart behavior, not a production capture. The real original-source export
authorization and native engine drill remain separate gates.
"""
import copy
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from kopiur_elasticsearch_capture import (SnapshotCapture, credentials_from_bytes,
                                         snapshot_metadata, synthetic_snapshot_archive)
from kopiur_elasticsearch_escrow import CONFIG_FILES, EscrowError, _binding, _encoded


# snapshot_metadata exercises the same metadata the engine contract embeds in
# every native snapshot: generation, source identity and pod binding.
def snp():
    return {'snapshot': 'generation', 'uuid': 'c' * 32, 'state': 'SUCCESS',
            'include_global_state': True, 'version': '8.19.1',
            'metadata': {'generation': 'a' * 32, 'source_uid': 'containerd://' + 'b' * 64,
                         'source_pod_uid': 'pod-' + 'c' * 32},
            'indices': ['fixture', '.security-7'], 'shards': {'total': 1, 'successful': 1,
            'failed': 0}, 'feature_states': [{'feature_name': 'security',
            'indices': ['.security-7']}]}


def source_binding():
    return _binding({'generation': 'a' * 32, 'source_uid': 'containerd://' + 'b' * 64,
        'source_pod_uid': 'pod-' + 'c' * 32, 'engine_image': 'engine@sha256:' + 'd' * 64,
        'runtime_version': '8.19.1', 'config_paths': sorted(CONFIG_FILES),
        'source_lifetime': {'namespace': 'database', 'pod': 'elasticsearch-0', 'container': 'app',
            'runtime_image': 'engine@sha256:' + 'e' * 64, 'restart_count': 0,
            'started_at': '2026-10-01T00:00:00Z'},
        'credential_versions': {'external-secret-uid': 'provider-uid'}})


def credentials():
    return {'elastic_username': 'elastic', 'elastic_password': 'synthetic-password'}


def build_synthetic_archive():
    return synthetic_snapshot_archive()


class CaptureAdapterTests(unittest.TestCase):
    def setUp(self):
        self.binding = source_binding()
        self.authority = Mock(return_value=True)
        self.credential_bytes = Mock(return_value=_encoded(credentials()))
        self.identity = Mock(return_value={'username': 'elastic', 'roles': ['superuser']})
        self.github_archive = Mock(return_value=b'archive-bytes')
        self.capture = SnapshotCapture(self.binding, guard=self.authority,
            read_credentials=self.credential_bytes, request=self.request,
            read_archive=self.github_archive, repository='owned-snapshot',
            snapshot='generation', location='/usr/share/elasticsearch/data/snapshot',
            indices=['fixture'])
        self.request_setup()

    def snp(self):
        return {'snapshot': 'generation', 'uuid': '9' * 32, 'state': 'SUCCESS',
                'include_global_state': True, 'version': '8.19.1',
                'metadata': snapshot_metadata(self.binding),
                'indices': ['fixture', '.security-7'],
                'shards': {'total': 1, 'successful': 1, 'failed': 0},
                'failures': [], 'feature_states': [{'feature_name': 'security',
                'indices': ['.security-7']}]}

    def request(self, path, value):
        self.requests.setdefault(path, []).append(value)
        return self.routes[path]

    def request_setup(self):
        self.requests = {}
        self.routes = {'/': {'version': {'number': '8.19.1'}},
            '/_security/_authenticate': self.identity.return_value,
            '/_snapshot/owned-snapshot': {'owned-snapshot': {'type': 'fs',
                'settings': {'location': '/usr/share/elasticsearch/data/snapshot'}}},
            '/_snapshot/owned-snapshot/generation': {'snapshots': [self.snp()]}}

    def credentials_part(self):
        self.identity.return_value = {'username': 'elastic', 'roles': ['superuser']}
        self.request_setup()
        return self.capture.credentials(self.binding)

    def test_credential_part_binding_and_encoding(self):
        part = self.credentials_part()
        self.assertEqual(part, {'binding': self.binding, 'mode': 0o600, 'uid': 1000,
                                'gid': 1000, 'data': _encoded(credentials())})
        self.assertEqual(self.authority.call_count, 4)

    def test_wrong_identity_denied_before_any_data_read(self):
        self.identity.return_value = {'username': 'other', 'roles': ['superuser']}
        self.request_setup()
        with self.assertRaises(EscrowError):
            self.capture.credentials(self.binding)
        self.github_archive.assert_not_called()

    def test_authentication_failure_swallows_secret_chain(self):
        self.credential_bytes.side_effect = ValueError('PRIVATE synthetic-password bytes')
        with self.assertRaises(EscrowError) as caught:
            self.credentials_part()
        self.assertNotIn('synthetic', str(caught.exception))

    def test_guard_revocation_around_every_request(self):
        self.authority.side_effect = [True, True, True, False]
        self.request_setup()
        with self.assertRaises(EscrowError):
            self.capture.credentials(self.binding)
        # No path received its second guarded request after revocation.
        self.assertTrue(all(len(calls) <= 1 for calls in self.requests.values()))

    def test_runtime_version_mismatch_denies_native_capture(self):
        self.request_setup()
        self.routes['/'] = {'version': {'number': '8.0.0'}}
        with self.assertRaises(EscrowError):
            self.capture.native(self.binding)
        self.github_archive.assert_not_called()

    def test_repository_location_snapshot_state_and_lineage_denied(self):
        for mutation in (lambda: self.routes['/_snapshot/owned-snapshot'].update(
                {'owned-snapshot': {'type': 'fs', 'settings': {'location': '/other'}}}),
            lambda: self.routes['/_snapshot/owned-snapshot/generation']['snapshots'][0].update(
                state='PARTIAL'),
            lambda: self.routes['/_snapshot/owned-snapshot/generation']['snapshots'][0].update(
                include_global_state=False),
            lambda: self.routes['/_snapshot/owned-snapshot/generation']['snapshots'][0].update(
                metadata={'generation': 'elsewhere'}),
            lambda: self.routes['/_snapshot/owned-snapshot/generation']['snapshots'][0].update(
                shards={'total': 2, 'successful': 1, 'failed': 1}),
            lambda: self.routes['/_snapshot/owned-snapshot/generation']['snapshots'][0].update(
                feature_states=[]),
            lambda: self.routes['/_snapshot/owned-snapshot/generation']['snapshots'][0].update(
                uuid=''),
            lambda: self.routes['/_snapshot/owned-snapshot/generation']['snapshots'][0].update(
                snapshot='other-name'),
            lambda: self.routes['/_snapshot/owned-snapshot/generation']['snapshots'][0].update(
                version='8.0.0'),
            lambda: self.routes['/_snapshot/owned-snapshot/generation']['snapshots'][0].update(
                indices=['other-index']),
            lambda: self.routes['/_snapshot/owned-snapshot/generation']['snapshots'][0].update(
                failures=[{'shard': 0}])):
            self.setUp()
            mutation()
            with self.subTest(mutation=mutation), self.assertRaises(EscrowError):
                self.capture.native(self.binding)
            self.github_archive.assert_not_called()

    def test_native_capture_reads_archive_between_equal_snapshot_state(self):
        self.capture.read_archive = lambda location: build_synthetic_archive()
        native = self.capture.native(self.binding)
        self.assertIsInstance(native['data'], bytes)
        self.assertEqual(len(self.requests['/_snapshot/owned-snapshot/generation']), 2)
        self.assertEqual(len(self.requests['/_snapshot/owned-snapshot']), 2)

    def test_changed_snapshot_metadata_after_archive_read_denied(self):
        self.capture.read_archive = lambda location: build_synthetic_archive()

        def changing(changed_path, value):
            if (changed_path == '/_snapshot/owned-snapshot/generation'
                    and len(self.requests.get(changed_path, [])) >= 1):
                # A real change: the engine contract binds generation and state
                # metadata, so any mutation must invalidate the archive read.
                self.routes[changed_path] = {'snapshots': [self.snp()], 'changed': True}
            self.requests.setdefault(changed_path, []).append(value)
            return self.routes[changed_path]

        self.capture.request = changing
        with self.assertRaises(EscrowError):
            self.capture.native(self.binding)

    def test_constructor_rejects_bad_inventory_location_and_binding(self):
        arguments = (dict(repository='../escape'), dict(snapshot='Bad Name'),
                     dict(location='/usr/share/elasticsearch/data/nowhere'),
                     dict(indices=[]), dict(indices=['fixture', 'fixture']))
        for override in arguments:
            built = dict(guard=self.authority, read_credentials=self.credential_bytes,
                         request=self.request, read_archive=self.github_archive,
                         repository='owned-snapshot', snapshot='generation',
                         location='/usr/share/elasticsearch/data/snapshot', indices=['fixture'])
            built.update(override)
            with self.subTest(override=override), self.assertRaises(EscrowError):
                SnapshotCapture(self.binding, **built)
        stale = copy.deepcopy(self.binding)
        stale['generation'] = 'f' * 32
        with self.assertRaises(EscrowError):
            self.capture.native(stale)
        self.github_archive.assert_not_called()

    def test_binding_mismatch_denies_every_call(self):
        stale = dict(self.binding, generation='f' * 32)
        for call in (lambda: self.capture.credentials(stale),
                     lambda: self.capture.native(stale)):
            with self.subTest(call=call), self.assertRaises(EscrowError):
                call()
        self.credential_bytes.assert_not_called()
        self.github_archive.assert_not_called()

    def test_credential_bytes_shape_rejections(self):
        for bad in (None, 12, b'[]', json.dumps(credentials()).encode() + b' ',
                    json.dumps({'elastic_username': 'root', 'elastic_password': 'x'}).encode(),
                    json.dumps({'elastic_username': 'elastic', 'elastic_password': ''}).encode(),
                    json.dumps({'elastic_username': 'elastic',
                                'elastic_password': 'x', 'extra': 1}).encode(),
                    b'{"elastic_username": "elastic", "elastic_username": "other",'
                    b' "elastic_password": "x"}'):
            with self.subTest(bad=bad), self.assertRaises(EscrowError):
                credentials_from_bytes(bad)


def patch_request(capture, callback):
    capture.request = callback


if __name__ == '__main__':
    unittest.main()
