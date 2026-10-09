"""Owned synthetic archive adapter tests, intended for existing ARC runners."""
import copy
import io
import json
from pathlib import Path
import sys
import tarfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from kopiur_elasticsearch_escrow import CONFIG_FILES, EscrowError
from kopiur_elasticsearch_escrow_fixture import capture_configuration, configuration_archive, configuration_parts


class ConfigurationArchiveTests(unittest.TestCase):
    def setUp(self):
        self.binding = {
            'generation': 'a' * 32, 'source_uid': 'synthetic-source',
            'source_pod_uid': 'synthetic-pod', 'engine_image': 'engine@sha256:' + 'b' * 64,
            'runtime_version': 'synthetic-runtime',
            'credential_versions': {'synthetic-credential': 'version-1'},
            'config_paths': sorted(CONFIG_FILES | {'jvm.options.d/fixture.options'}),
        }
        self.parts = {
            name: {'binding': copy.deepcopy(self.binding),
                   'data': ('synthetic-' + name).encode(), 'mode': 0o640, 'uid': 1000, 'gid': 0}
            for name in {'native', 'runtime', 'credentials'} |
            {'config/' + path for path in self.binding['config_paths']}
        }
        self.parts['config/users']['data'] = b''

    def decode(self, archive):
        return configuration_parts(self.binding, archive, **{
            name: self.parts[name] for name in ('native', 'runtime', 'credentials')})

    def augmented(self, name, kind=tarfile.REGTYPE):
        output = io.BytesIO()
        with tarfile.open(fileobj=output, mode='w') as target:
            with tarfile.open(fileobj=io.BytesIO(configuration_archive(self.binding, self.parts))) as source:
                for member in source:
                    target.addfile(member, source.extractfile(member))
            member = tarfile.TarInfo(name)
            member.type = kind
            member.linkname = 'elasticsearch.yml' if kind != tarfile.REGTYPE else ''
            target.addfile(member)
        return output.getvalue()

    def test_exact_bytes_metadata_nested_dependency_and_empty_users_round_trip(self):
        self.assertEqual(self.decode(configuration_archive(self.binding, self.parts)), self.parts)

    def test_empty_directory_inventory_metadata_round_trip(self):
        self.binding['config_directories'] = ['.', 'jvm.options.d', 'empty-certs']
        for part in self.parts.values():
            part['binding'] = copy.deepcopy(self.binding)
        for path in self.binding['config_directories']:
            self.parts['config-dir/' + path] = {
                'binding': copy.deepcopy(self.binding), 'data': b'',
                'mode': 0o750, 'uid': 1000, 'gid': 0,
            }
        self.assertEqual(self.decode(configuration_archive(self.binding, self.parts)), self.parts)

        del self.parts['config-dir/empty-certs']
        with self.assertRaises(EscrowError):
            configuration_archive(self.binding, self.parts)

    def test_uninventoried_empty_directory_denied(self):
        with self.assertRaises(EscrowError):
            self.decode(self.augmented('empty-certs', tarfile.DIRTYPE))

    def test_restore_callback_configuration_without_native_is_supported(self):
        configuration = {name: part for name, part in self.parts.items() if name != 'native'}
        self.assertEqual(configuration_archive(self.binding, configuration),
                         configuration_archive(self.binding, self.parts))
        del configuration['runtime']
        with self.assertRaises(EscrowError):
            configuration_archive(self.binding, configuration)

    def test_uninventoried_dependency_and_traversal_rejected(self):
        for name in ('new-config.yml', '../elasticsearch.yml', '/elasticsearch.yml'):
            with self.subTest(name=name), self.assertRaises(EscrowError):
                self.decode(self.augmented(name))

    def test_links_and_duplicate_entries_rejected(self):
        for name, kind in (('elasticsearch.yml', tarfile.REGTYPE),
                           ('link', tarfile.SYMTYPE), ('link', tarfile.LNKTYPE)):
            with self.subTest(name=name, kind=kind), self.assertRaises(EscrowError):
                self.decode(self.augmented(name, kind))

    def test_missing_configuration_and_stale_binding_rejected(self):
        del self.parts['config/elasticsearch.keystore']
        with self.assertRaises(EscrowError):
            configuration_archive(self.binding, self.parts)
        self.setUp()
        self.parts['runtime']['binding']['runtime_version'] = 'stale'
        with self.assertRaises(EscrowError):
            configuration_archive(self.binding, self.parts)

    def test_invalid_archive_rejected(self):
        with self.assertRaises(EscrowError):
            self.decode(b'not-a-tar')

    def capture(self, drill, **overrides):
        arguments = {
            **{name: self.parts[name] for name in ('native', 'runtime', 'credentials')},
            'observe': lambda: copy.deepcopy(self.binding),
            'require_capture_authority': lambda binding: True, 'synthetic': True,
        }
        arguments.update(overrides)
        with mock.patch('kopiur_elasticsearch_escrow_fixture.sys.platform', 'linux'), \
                mock.patch.dict('os.environ', {'RUNNER_NAME': 'ghar-set-zoo-test'}):
            return capture_configuration(drill, 'owned-source', self.binding, **arguments)

    def drill(self):
        drill = mock.Mock(service='elasticsearch', prefix='owned')
        drill.registered_id.return_value = self.binding['source_uid']
        identity = json.dumps({'Id': self.binding['source_uid'], 'Name': '/owned-source',
                               'Config': {'Image': self.binding['engine_image']}}).encode()
        archive = configuration_archive(self.binding, self.parts)
        drill.run.side_effect = lambda *args: identity if args[0] == 'inspect' else archive
        return drill

    def test_owned_docker_configuration_capture_preserves_exact_parts(self):
        drill = self.drill()
        self.assertEqual(self.capture(drill), self.parts)
        self.assertEqual(drill.run.call_args_list, [
            mock.call('inspect', '--format', '{{json .}}', self.binding['source_uid']),
            mock.call('cp', self.binding['source_uid'] + ':/usr/share/elasticsearch/config/.', '-'),
            mock.call('inspect', '--format', '{{json .}}', self.binding['source_uid']),
        ])

    def test_revoked_capture_and_nonsynthetic_deny_before_docker(self):
        for overrides in ({'synthetic': False}, {'require_capture_authority': lambda binding: False}):
            drill = self.drill()
            with self.subTest(overrides=overrides), self.assertRaises(EscrowError):
                self.capture(drill, **overrides)
            drill.run.assert_not_called()

    def test_wrong_service_or_registered_source_deny_before_docker(self):
        for wrong_service in (True, False):
            drill = self.drill()
            if wrong_service:
                drill.service = 'redis'
            else:
                drill.registered_id.return_value = 'replacement'
            with self.subTest(wrong_service=wrong_service), self.assertRaises(EscrowError):
                self.capture(drill)
            drill.run.assert_not_called()

    def test_inspected_runtime_mismatch_denies_archive_read(self):
        drill = self.drill()
        drill.run.side_effect = None
        drill.run.return_value = json.dumps({'Id': self.binding['source_uid'],
                                            'Name': '/owned-source', 'Config': {'Image': 'wrong'}}).encode()
        with self.assertRaises(EscrowError):
            self.capture(drill)
        self.assertEqual(drill.run.call_count, 1)

    def test_capture_revocation_after_copy_denies_returning_bytes(self):
        drill = self.drill()
        with self.assertRaises(EscrowError):
            self.capture(drill, require_capture_authority=mock.Mock(side_effect=[True, True, False]))
        self.assertEqual(drill.run.call_count, 2)


if __name__ == '__main__':
    unittest.main()
