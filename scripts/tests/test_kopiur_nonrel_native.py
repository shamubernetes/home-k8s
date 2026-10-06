"""Host-only parsing/admission regressions, never native engine proof."""
import io
import sys
import tarfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import kopiur_nonrel_native as native


class AdmissionTests(unittest.TestCase):
    def test_unknown_service(self):
        with self.assertRaises(ValueError):
            native.Fixture('production')

    def test_host_runtime_denied(self):
        with patch.dict(native.os.environ, {'RUNNER_NAME': ''}):
            with self.assertRaises(RuntimeError):
                native.Fixture('dragonfly')

    def test_other_scale_set_denied(self):
        with patch.dict(native.os.environ, {'RUNNER_NAME': 'ghar-set-maudecode-fixture'}):
            with self.assertRaises(RuntimeError):
                native.Fixture('rabbitmq-server')

    def test_images_are_pinned(self):
        self.assertEqual(set(native.IMAGES), {'dragonfly', 'elasticsearch', 'rabbitmq-server'})
        for image in native.IMAGES.values():
            self.assertRegex(image, r'@sha256:[0-9a-f]{64}$')


class ServerRetirementTests(unittest.TestCase):
    def fixture(self):
        fixture = native.Fixture.__new__(native.Fixture)
        fixture.containers = ['owned-source']
        fixture.container_ids = {'owned-source': 'a' * 64}
        return fixture

    def test_exact_server_removal_read_back(self):
        fixture = self.fixture()
        with patch.object(fixture, 'run', side_effect=[b'a' * 64 + b'\n', b'', b'b' * 64 + b'\n']) as run:
            proof = fixture.remove('owned-source')
        self.assertEqual([call.args for call in run.call_args_list], [
            ('inspect', '--format', '{{.Id}}', 'owned-source'),
            ('rm', '-fv', 'a' * 64),
            ('ps', '-a', '--no-trunc', '--format', '{{.ID}}')])
        self.assertEqual(proof, {'container_id': 'a' * 64, 'daemon_inventory_absent': True,
                                'production_mutation_cessation_qualified': False})
        self.assertEqual(fixture.containers, [])
        self.assertEqual(fixture.container_ids, {})

    def test_replaced_name_never_removed(self):
        fixture = self.fixture()
        with patch.object(fixture, 'run', return_value=b'b' * 64) as run:
            with self.assertRaisesRegex(RuntimeError, 'identity changed'):
                fixture.remove('owned-source')
        self.assertEqual(run.call_count, 1)
        self.assertEqual(fixture.containers, ['owned-source'])

    def test_server_still_present_denies_restore_proof(self):
        fixture = self.fixture()
        with patch.object(fixture, 'run', side_effect=[b'a' * 64, b'', b'a' * 64 + b'\n']):
            with self.assertRaisesRegex(RuntimeError, 'remains after removal'):
                fixture.remove('owned-source')
        self.assertEqual(fixture.container_ids, {'owned-source': 'a' * 64})
        self.assertEqual(fixture.containers, ['owned-source'])

    def test_unregistered_server_denied(self):
        fixture = self.fixture()
        fixture.container_ids.clear()
        with patch.object(fixture, 'run') as run:
            with self.assertRaisesRegex(RuntimeError, 'identity changed'):
                fixture.remove('owned-source')
        run.assert_not_called()

    def test_inventory_failure_never_becomes_absence(self):
        fixture = self.fixture()
        with patch.object(fixture, 'run', side_effect=[b'a' * 64, b'', RuntimeError('daemon unavailable')]):
            with self.assertRaisesRegex(RuntimeError, 'daemon unavailable'):
                fixture.remove('owned-source')
        self.assertEqual(fixture.container_ids, {'owned-source': 'a' * 64})


    def test_lost_removal_acknowledgement_remains_unresolved(self):
        fixture = self.fixture()
        with patch.object(fixture, 'run', side_effect=[b'a' * 64, RuntimeError('ack lost')]) as run:
            with self.assertRaisesRegex(RuntimeError, 'ack lost'):
                fixture.remove('owned-source')
        self.assertEqual(run.call_count, 2)
        self.assertEqual(fixture.containers, ['owned-source'])

    def test_cleanup_never_removes_by_replaced_name(self):
        fixture = self.fixture()
        fixture.network = 'owned-network'
        with patch.object(native.subprocess, 'run') as run:
            fixture.cleanup()
        self.assertEqual([call.args[0] for call in run.call_args_list], [
            ['docker', 'rm', '-fv', 'a' * 64],
            ['docker', 'network', 'rm', 'owned-network']])
        self.assertEqual(fixture.container_ids, {})

    def test_cleanup_missing_identity_never_falls_back_to_name(self):
        fixture = self.fixture()
        fixture.container_ids.clear()
        with patch.object(native.subprocess, 'run') as run:
            with self.assertRaisesRegex(RuntimeError, 'lacks immutable'):
                fixture.cleanup()
        run.assert_not_called()


class ElasticsearchAdmissionTests(unittest.TestCase):
    def test_prior_setting_preserved(self):
        for block in (None, 'false', 'true'):
            values = {'index.uuid': 'original-index'}
            if block is not None:
                values['index.blocks.write'] = block
            self.assertEqual(native.elasticsearch_admission({'fixture': {'settings': values}}),
                             {'index_uuid': 'original-index', 'write_block': block})

    def test_incomplete_or_ambiguous_observation_denied(self):
        for settings in (None, {}, {'alias': {'settings': {'index.uuid': 'original'}}},
                         {'fixture': None}, {'fixture': {'settings': {}}},
                         {'fixture': {'settings': {'index.uuid': ''}}},
                         {'fixture': {'settings': {'index.uuid': True}}},
                         {'fixture': {}, 'other': {}}):
            with self.subTest(settings=settings), self.assertRaises(RuntimeError):
                native.elasticsearch_admission(settings)

    def test_malformed_block_denied(self):
        for block in (None, True, False, 0, 1, 'TRUE', '', [], {}):
            with self.subTest(block=block), self.assertRaises(RuntimeError):
                native.elasticsearch_admission({'fixture': {'settings': {
                    'index.uuid': 'original', 'index.blocks.write': block}}})


class ElasticsearchRestoreTests(unittest.TestCase):
    def test_complete_restore(self):
        self.assertEqual(native.validate_elasticsearch_restore({
            'snapshot': {'indices': ['fixture'],
                         'shards': {'total': 1, 'successful': 1, 'failed': 0}}}), 1)

    def test_partial_or_missing_restore_denied(self):
        for shards in ({}, {'total': 0, 'successful': 0, 'failed': 0},
                       {'total': 2, 'successful': 1, 'failed': 1},
                       {'total': 2, 'successful': 1, 'failed': 0},
                       {'total': True, 'successful': True, 'failed': 0},
                       {'total': 1, 'successful': 1, 'failed': False}):
            with self.subTest(shards=shards), self.assertRaises(RuntimeError):
                native.validate_elasticsearch_restore({'snapshot': {'indices': ['fixture'], 'shards': shards}})
        with self.assertRaises(RuntimeError):
            native.validate_elasticsearch_restore({})

    def test_unexpected_indices_denied(self):
        with self.assertRaises(RuntimeError):
            native.validate_elasticsearch_restore({'snapshot': {
                'indices': ['other'], 'shards': {'total': 1, 'successful': 1, 'failed': 0}}})

    def test_security_feature_shards_counted(self):
        self.assertEqual(native.validate_elasticsearch_restore({'snapshot': {
            'indices': ['fixture', '.security-7'],
            'shards': {'total': 2, 'successful': 2, 'failed': 0}}}), 2)
        for indices in (['fixture', 'other'], ['fixture', None], 'fixture'):
            with self.subTest(indices=indices), self.assertRaises(RuntimeError):
                native.validate_elasticsearch_restore({'snapshot': {
                    'indices': indices, 'shards': {'total': 2, 'successful': 2, 'failed': 0}}})


class RespTests(unittest.TestCase):
    def test_binary_bulk(self):
        self.assertEqual(native.resp_read(io.BytesIO(b'$3\r\n\x00\xffx\r\n')), b'\x00\xffx')

    def test_nested_array(self):
        self.assertEqual(native.resp_read(io.BytesIO(b'*3\r\n+OK\r\n:2\r\n$-1\r\n')), [b'OK', 2, None])

    def test_server_error_suppresses_content(self):
        with self.assertRaises(RuntimeError) as caught:
            native.resp_read(io.BytesIO(b'-fixture-password\r\n'))
        self.assertNotIn('fixture-password', str(caught.exception))

    def test_invalid_frames(self):
        frames = (b'$-2\r\n', b'$1048577\r\n', b'*4097\r\n', b'$3\r\nx\r\n',
                  b'$1\r\nxzz', b'hello\r\n', b'+truncated', b'*' + b'1\r\n*' * 17 + b'0\r\n')
        for frame in frames:
            with self.subTest(frame=frame[:20]), self.assertRaises(ValueError):
                native.resp_read(io.BytesIO(frame))


class ArchiveTests(unittest.TestCase):
    def archive(self, entries):
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode='w') as archive:
            for name, kind in entries:
                member = tarfile.TarInfo(name)
                member.type = kind
                member.linkname = '/outside' if kind in (tarfile.SYMTYPE, tarfile.LNKTYPE) else ''
                if kind == tarfile.REGTYPE:
                    member.size = 3
                    archive.addfile(member, io.BytesIO(b'abc'))
                else:
                    archive.addfile(member)
        return stream.getvalue()

    def test_regular_archive(self):
        self.assertRegex(native.validate_archive(self.archive([('fixture', tarfile.REGTYPE)])), r'^[0-9a-f]{64}$')

    def test_unsafe_members(self):
        entries = ([('/outside', tarfile.REGTYPE)], [('../outside', tarfile.REGTYPE)],
                   [('link', tarfile.SYMTYPE)], [('link', tarfile.LNKTYPE)],
                   [('pipe', tarfile.FIFOTYPE)], [('duplicate', tarfile.REGTYPE)] * 2)
        for members in entries:
            with self.subTest(members=members), self.assertRaises(ValueError):
                native.validate_archive(self.archive(members))

    def test_empty_archive(self):
        with self.assertRaises(ValueError):
            native.validate_archive(self.archive([]))

    def test_repository_envelope(self):
        data = self.archive([('.', tarfile.DIRTYPE), ('./index-0', tarfile.REGTYPE)])
        wrapped = native.repository_envelope(data)
        with tarfile.open(fileobj=io.BytesIO(wrapped), mode='r:') as archive:
            self.assertEqual(archive.getnames(), ['snapshot', 'snapshot/index-0'])
            self.assertEqual(archive.extractfile('snapshot/index-0').read(), b'abc')
            self.assertTrue(archive.getmember('snapshot').isdir())

    def test_archive_size_bound(self):
        with patch.object(native, 'MAX_ARCHIVE', 1):
            with self.assertRaises(ValueError):
                native.validate_archive(b'xx')


if __name__ == '__main__':
    unittest.main()
