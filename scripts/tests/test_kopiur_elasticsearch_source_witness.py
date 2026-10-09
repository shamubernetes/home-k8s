"""ARC-only runtime tests for reviewed immutable artifact/source mapping."""
import copy
import io
import json
import os
import platform
from pathlib import Path
import subprocess
import signal
import sys
import tarfile
import tempfile
import unittest
import urllib.request
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from kopiur_elasticsearch_consumer_authority import ConsumerAuthority
from kopiur_elasticsearch_escrow import EscrowError, _digest, _encoded
from kopiur_elasticsearch_source_witness import SOURCE_PATHS, SOURCE_URL, archive_digests, image_digests

IMAGE = 'docker.io/bbilly1/tubearchivist@sha256:ba1c846ddd0c6fdd0f040727129d2466e03b7a0c26499223839d450c7586ac09'
REVISION = '1f480944013a406b9e4d28725f529564c43afc3b'
# Independently compared public release bytes, not service credentials.
REVIEWED_DIGESTS = {
    'docker_assets/run.sh': '0ec3c184265a2130b1ab444fe7b8068a54c792a353b2a22e900401e53b6452fa',
    'docker_assets/beat_auto_spawn.sh': 'b87b04c1ec9e94e03d740fa4674c2d67c84c6c5ba9826e8030c79f11779608e9',
    'docker_assets/backend_start.py': 'ba3dfb1e0de9c92b60b4a79c328221c98bf0ce5550e38585f5c76f452fffd3f4',
    'backend/appsettings/index_mapping.json': 'daa13ee8c636fcdaa064a75696f0508c9424babbfa4ec54f8eec60faf57c3d30',
    'backend/appsettings/src/index_setup.py': '83f913056cc5dcd66d33ba3286c6f50fe5c296695859474192f213aaa355230c',
    'backend/common/src/env_settings.py': '075a053b1630c9886e92ed3de09d639486de7154ab68b95996a7500998c07e4b',
    'backend/common/src/es_connect.py': 'b69bd1b886a0a88c73b838907101c886cd71366742c328a650cb90dede14d568',
    'backend/common/src/index_generic.py': 'ec1573a19ac1ca4bbf889f8edef31efaecf9e214d8b09cb307488a88a8519676',
    'backend/common/src/searching.py': 'f949269b055c97b65b1975ab2504605d3a5df31d9a94508b15818724094121f5',
    'backend/config/settings.py': '922b5a2bc6a86ee51ef96303e367b8e5ef85d03c5f661f8d9b671d5eed50c1d6',
}


def plan():
    files = {p: _digest(p.encode()) for p in SOURCE_PATHS}
    p = {'application': 'media/tubearchivist', 'store': 'elasticsearch-indices:media/tubearchivist',
         'image': IMAGE, 'declared_image': IMAGE, 'source_url': SOURCE_URL,
         'source_revision': REVISION, 'source_checkout': '/source', 'source_files': files,
         'selectors': ['ta_*'], 'required_indices': ['ta_config'],
         'expected_selection': None, 'default_selection': None}
    p['source_witness'] = {'image': IMAGE, 'source_url': SOURCE_URL, 'source_revision': REVISION,
                          'files': [{'source': s, 'artifact': a, 'sha256': files[s]}
                                    for s, a in SOURCE_PATHS.items()]}
    return p


class WitnessTests(unittest.TestCase):
    def setUp(self):
        self.plan = plan()
        self.grant = Mock(return_value=True)
        self.calls = []
        self.labels = {}
        self.observed = {a: self.plan['source_files'][s] for s, a in SOURCE_PATHS.items()}

    def read(self, argv, data=None):
        self.calls.append(argv)
        if argv[0] == 'crane': return _encoded({'config': {'Labels': self.labels}})
        if argv[0] == 'git': return argv[-1].split(':', 1)[1].encode()
        if '--image' in argv: return _encoded(self.observed)
        raise AssertionError('unexpected consumer/process operation')

    def authority(self):
        return ConsumerAuthority([self.plan], require_authority=self.grant, run=self.read)

    def test_complete_content_mapping_without_labels_qualifies_selected_bytes(self):
        receipt = self.authority().release(self.plan)
        self.assertEqual(receipt['source_evidence'], 'selected-artifact-bytes')
        self.assertEqual(receipt['source_witness_sha256'], _digest(_encoded(self.plan['source_witness'])))
        self.assertEqual(len([c for c in self.calls if c[0] == 'git']), 10)
        self.assertEqual(len([c for c in self.calls if '--image' in c]), 1)

    def test_reordered_complete_witness_is_valid(self):
        self.plan['source_witness']['files'].reverse()
        self.authority().release(self.plan)

    def test_partial_duplicate_or_changed_mapping_denied_before_io(self):
        original = copy.deepcopy(self.plan)
        for change in ('partial', 'duplicate', 'foreign', 'digest', 'extra', 'source'):
            with self.subTest(change=change):
                self.plan = copy.deepcopy(original)
                files = self.plan['source_witness']['files']
                if change == 'partial': files.pop()
                if change == 'duplicate': files[-1] = copy.deepcopy(files[0])
                if change == 'foreign': files[0]['artifact'] = 'foreign/run.sh'
                if change == 'digest': files[0]['sha256'] = 'b'*64
                if change == 'extra': files[0]['extra'] = True
                if change == 'source': self.plan['source_files'].pop(files[0]['source'])
                with self.assertRaises(EscrowError): self.authority()
        self.assertEqual(self.calls, [])

    def test_foreign_image_revision_url_or_application_denied_before_io(self):
        original = copy.deepcopy(self.plan)
        for field, value in [('image', IMAGE[:-1]+'0'), ('source_revision', 'b'*40),
                             ('source_url', 'https://github.com/example/foreign'),
                             ('application', 'services/zoo-cowbell')]:
            self.plan = copy.deepcopy(original)
            self.plan[field] = value
            with self.assertRaises(EscrowError): self.authority()
        self.assertEqual(self.calls, [])

    def test_conflicting_labels_never_fall_back_to_content_witness(self):
        self.labels = {'org.opencontainers.image.revision': 'b'*40}
        with self.assertRaises(EscrowError): self.authority().release(self.plan)
        self.assertEqual(len(self.calls), 1)

    def test_matching_labels_do_not_skip_supplied_content_witness(self):
        self.labels = {'org.opencontainers.image.source': SOURCE_URL,
                       'org.opencontainers.image.revision': REVISION}
        self.observed['app/run.sh'] = 'b'*64
        with self.assertRaises(EscrowError): self.authority().release(self.plan)

    def test_missing_changed_extra_or_typed_artifact_result_denied_before_processes(self):
        original = copy.deepcopy(self.observed)
        for change in ('missing', 'changed', 'extra', 'typed'):
            self.observed = copy.deepcopy(original)
            if change == 'missing': self.observed.pop('app/run.sh')
            if change == 'changed': self.observed['app/run.sh'] = 'b'*64
            if change == 'extra': self.observed['app/foreign'] = 'b'*64
            if change == 'typed': self.observed['app/run.sh'] = True
            with self.assertRaises(EscrowError): self.authority().observe()
        self.assertFalse(any(c[0] == 'kubectl' for c in self.calls))

    def test_source_bytes_change_prevents_artifact_and_process_reads(self):
        self.plan['source_files']['docker_assets/run.sh'] = 'b'*64
        self.plan['source_witness']['files'][0]['sha256'] = 'b'*64
        with self.assertRaises(EscrowError): self.authority().observe()
        self.assertFalse(any('--image' in c or c[0] == 'kubectl' for c in self.calls))

    def test_revocation_discards_artifact_result_and_prevents_process_reads(self):
        original = self.read
        def revoke(argv, data=None):
            result = original(argv, data)
            if '--image' in argv: self.grant.return_value = False
            return result
        a = self.authority(); a.run = revoke
        with self.assertRaises(EscrowError): a.observe()
        self.assertFalse(any(c[0] == 'kubectl' for c in self.calls))

    def test_source_checkpoint_composed_before_artifact_reads(self):
        a = self.authority().with_checkpoint(lambda: False)
        with self.assertRaises(EscrowError): a.observe()
        self.assertEqual(self.calls, [])

    def archive(self, extra=None, omit=None):
        out = io.BytesIO()
        with tarfile.open(fileobj=out, mode='w') as archive:
            for source, target in SOURCE_PATHS.items():
                if target == omit: continue
                data = source.encode(); entry = tarfile.TarInfo(target); entry.size = len(data)
                archive.addfile(entry, io.BytesIO(data))
            if extra is not None:
                archive.addfile(extra, io.BytesIO(b'x' * extra.size) if extra.isfile() else None)
        out.seek(0)
        return out

    def test_archive_digest_parser_complete_real_tar(self):
        self.assertEqual(archive_digests(self.archive()), self.observed)

    def test_archive_missing_duplicate_or_nonregular_selected_member_denied(self):
        with self.assertRaises(EscrowError): archive_digests(self.archive(omit='app/run.sh'))
        for kind in (tarfile.REGTYPE, tarfile.SYMTYPE, tarfile.LNKTYPE):
            entry = tarfile.TarInfo('app/run.sh'); entry.type = kind
            entry.linkname = 'foreign'; entry.size = 1 if kind == tarfile.REGTYPE else 0
            with self.assertRaises(EscrowError): archive_digests(self.archive(extra=entry))

    def test_archive_path_alias_and_symlink_ancestor_denied(self):
        for path, kind in [('app/../app/run.sh', tarfile.REGTYPE),
                           ('/app/run.sh', tarfile.REGTYPE), ('app', tarfile.SYMTYPE),
                           ('/app', tarfile.SYMTYPE), ('app/./common', tarfile.SYMTYPE),
                           ('app/common/../config', tarfile.LNKTYPE)]:
            entry = tarfile.TarInfo(path); entry.type = kind; entry.linkname = 'foreign'
            entry.size = 1 if kind == tarfile.REGTYPE else 0
            with self.assertRaises(EscrowError): archive_digests(self.archive(extra=entry))

    def test_unlabelled_release_without_explicit_witness_still_denied(self):
        self.plan.pop('source_witness')
        with self.assertRaises(EscrowError): self.authority().release(self.plan)

    def test_exporter_success_drains_and_checks_exit(self):
        process = Mock(stdout=self.archive())
        process.wait.return_value = 0; process.poll.return_value = 0
        with patch('kopiur_elasticsearch_source_witness.subprocess.Popen', return_value=process):
            self.assertEqual(image_digests(IMAGE), self.observed)
        process.wait.assert_called_once_with(timeout=30)
        process.kill.assert_not_called()
        self.assertTrue(process.stdout.closed)

    def test_exporter_nonzero_exit_cannot_be_hidden_by_complete_tar(self):
        process = Mock(stdout=self.archive())
        process.wait.return_value = 1; process.poll.return_value = 1
        with patch('kopiur_elasticsearch_source_witness.subprocess.Popen', return_value=process):
            with self.assertRaises(EscrowError): image_digests(IMAGE)
        self.assertTrue(process.stdout.closed)

    def test_exporter_parser_failure_kills_and_reaps_live_exporter(self):
        process = Mock(stdout=self.archive(omit='app/run.sh'))
        process.poll.return_value = None
        with patch('kopiur_elasticsearch_source_witness.subprocess.Popen', return_value=process):
            with self.assertRaises(EscrowError): image_digests(IMAGE)
        process.kill.assert_called_once(); process.wait.assert_called_once_with()
        self.assertTrue(process.stdout.closed)

    def test_exporter_timeout_handler_kills_and_reaps_and_restores_handler(self):
        process = Mock(stdout=io.BytesIO()); process.poll.return_value = None
        original = signal.getsignal(signal.SIGALRM)
        def timeout(stream):
            handler = signal.getsignal(signal.SIGALRM)
            self.assertTrue(callable(handler))
            if callable(handler): handler(signal.SIGALRM, None)
        with patch('kopiur_elasticsearch_source_witness.subprocess.Popen', return_value=process), \
                patch('kopiur_elasticsearch_source_witness.archive_digests', side_effect=timeout):
            with self.assertRaisesRegex(EscrowError, 'timed out'): image_digests(IMAGE)
        process.kill.assert_called_once(); process.wait.assert_called_once_with()
        self.assertEqual(signal.getsignal(signal.SIGALRM), original)


@unittest.skipUnless(sys.platform == 'linux' and os.environ.get('RUNNER_NAME', '').startswith('ghar-set-zoo-'),
                     'immutable artifact reader qualifies only on existing ARC')
class NativeWitnessTests(unittest.TestCase):
    def test_actual_immutable_image_and_exact_public_source_commit(self):
        with tempfile.TemporaryDirectory(prefix='k8s92-source-witness-') as checkout:
            self.assertEqual(platform.machine(), 'x86_64', 'qualified existing ARC architecture required')
            # Isolated tooling, exact repo-pinned version and independently read
            # upstream release-asset digest. No Mise candidate hooks, credentials,
            # global installation or modified trusted workflow are involved.
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            url = ('https://github.com/google/go-containerregistry/releases/download/v0.22.1/'
                   'go-containerregistry_Linux_x86_64.tar.gz')
            with opener.open(url, timeout=30) as response:
                payload = response.read(32 * 1024 * 1024 + 1)
            self.assertLessEqual(len(payload), 32 * 1024 * 1024)
            self.assertEqual(_digest(payload), '0ab7a1d6932a213aed964ce97666c3077fe691c8606413674a8b3e0b9ec4cda0')
            with tarfile.open(fileobj=io.BytesIO(payload), mode='r:gz') as tools:
                matches = [m for m in tools.getmembers() if m.name == 'crane']
                self.assertEqual(len(matches), 1)
                member = matches[0]
                self.assertTrue(member.isfile())
                self.assertLessEqual(member.size, 64 * 1024 * 1024)
                stream = tools.extractfile(member)
                self.assertIsNotNone(stream)
                if stream is None: self.fail('pinned crane payload missing')
                binary = Path(checkout) / 'crane'
                binary.write_bytes(stream.read()); binary.chmod(0o700)
            def git(*args):
                result = subprocess.run(['git', '-C', checkout, *args], capture_output=True, timeout=180)
                self.assertEqual(result.returncode, 0, 'public source checkout failed')
            git('init')
            git('fetch', '--depth', '1', SOURCE_URL + '.git', REVISION)
            p = plan(); p['source_checkout'] = checkout; p['source_files'] = copy.deepcopy(REVIEWED_DIGESTS)
            for item in p['source_witness']['files']: item['sha256'] = REVIEWED_DIGESTS[item['source']]
            authority = ConsumerAuthority([p], require_authority=lambda _: True)
            with patch.dict(os.environ, {'PATH': checkout + os.pathsep + os.environ['PATH']}):
                receipt = authority.release(p)
            self.assertEqual(receipt['source_evidence'], 'selected-artifact-bytes')
            self.assertEqual(receipt['source_witness_sha256'], _digest(_encoded(p['source_witness'])))
            # This is public artifact qualification, no original process/export grant.
            self.assertIsNone(authority.expected)


if __name__ == '__main__':
    unittest.main()
