#!/usr/bin/env python3
"""Regressions for the bounded Books-aware controller native consumer."""
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parent


def load(name):
    spec = importlib.util.spec_from_file_location(name, HERE / (name + '.py'))
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


prepare = load('consumer-prepare')
verify = load('consumer-verify')


class Response(io.BytesIO):
    def __init__(self, body, status):
        super().__init__(json.dumps(body).encode())
        self.status = status


class BooksConsumerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR'))
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def archive(self, entries):
        source = self.root / 'original.tar'
        with tarfile.open(source, 'w') as archive:
            for name, kind, mode in entries:
                member = tarfile.TarInfo(name)
                member.type = kind
                member.mode = mode
                member.uid = os.getuid()
                member.gid = os.getgid()
                member.uname = 'untrusted-name'
                member.gname = 'untrusted-group'
                if kind == tarfile.REGTYPE:
                    member.size = 5
                    archive.addfile(member, io.BytesIO(b'bytes'))
                else:
                    archive.addfile(member)
        return source

    def test_validated_archive_uses_numeric_owners(self):
        source = self.archive([('.', tarfile.DIRTYPE, 0o2750),
                               ('bookdrop', tarfile.DIRTYPE, 0o2755),
                               ('bookdrop/example', tarfile.REGTYPE, 0o640)])
        target = self.root / 'books'
        with patch.object(tarfile.TarFile, 'extractall', autospec=True) as extract:
            prepare.extract_archive(source, target)
            self.assertTrue(extract.call_args.kwargs['numeric_owner'])
        # Exercise actual validation/extraction on a credential-free local archive.
        prepare.extract_archive(source, target)
        self.assertEqual((target / 'bookdrop/example').read_bytes(), b'bytes')

    def test_unsafe_members_are_rejected_before_extraction(self):
        for name, kind, mode in [('../escape', tarfile.REGTYPE, 0o600),
                                  ('/absolute', tarfile.REGTYPE, 0o600),
                                  ('link', tarfile.SYMTYPE, 0o777),
                                  ('hard', tarfile.LNKTYPE, 0o600),
                                  ('device', tarfile.CHRTYPE, 0o600),
                                  ('suid', tarfile.REGTYPE, 0o4600),
                                  ('sgid-file', tarfile.REGTYPE, 0o2600)]:
            with self.subTest(name=name):
                source = self.archive([('.', tarfile.DIRTYPE, 0o750), (name, kind, mode)])
                with patch.object(tarfile.TarFile, 'extractall') as extract:
                    with self.assertRaises(AssertionError):
                        prepare.extract_archive(source, self.root / 'target')
                    extract.assert_not_called()

    def test_duplicate_canonical_paths_fail(self):
        source = self.archive([('same', tarfile.REGTYPE, 0o600), ('./same', tarfile.REGTYPE, 0o600)])
        with self.assertRaises(AssertionError):
            prepare.extract_archive(source, self.root / 'target')

    def test_wrong_application_owner_fails(self):
        source = self.archive([('.', tarfile.DIRTYPE, 0o750)])
        with self.assertRaises(AssertionError):
            prepare.extract_archive(source, self.root / 'target', app_owners=True)

    def test_setup_api_requires_actual_boolean_false(self):
        for status, body, expected in [(200, {'status': 200, 'data': False}, True),
                                       (200, {'status': 200, 'data': 0}, False),
                                       (200, {'status': 200, 'data': True}, False),
                                       (503, {'status': 200, 'data': False}, False),
                                       (200, {'status': 500, 'data': False}, False)]:
            with self.subTest(body=body, status=status):
                response = Response(body, status)
                with patch.object(verify.urllib.request, 'urlopen', return_value=response):
                    self.assertEqual(verify.setup_status(), expected)

    def test_setup_transport_failure_stays_false(self):
        with patch.object(verify.urllib.request, 'urlopen', side_effect=RuntimeError('private')):
            self.assertFalse(verify.setup_status())

    def test_rendered_consumers_are_isolated_and_mount_books(self):
        render = subprocess.run(['kubectl', 'kustomize', str(HERE)], check=True, text=True, capture_output=True)
        parsed = subprocess.run(['yq', '-o=json', '-I=0', '.'], input=render.stdout,
                                check=True, text=True, capture_output=True)
        docs = [json.loads(line) for line in parsed.stdout.splitlines() if line.startswith('{')]
        jobs = [d for d in docs if d.get('kind') == 'Job' and
                d['metadata']['name'].startswith('grimmory-k8s92-books-consumer-')]
        self.assertEqual(len(jobs), 2)
        for job in jobs:
            pod = job['spec']['template']['spec']
            self.assertFalse(pod['automountServiceAccountToken'])
            self.assertNotIn('fsGroup', pod['securityContext'])
            self.assertFalse(any('secret' in v or 'nfs' in v for v in pod['volumes']))
            original = next(v for v in pod['volumes'] if v['name'] == 'original')
            self.assertTrue(original['persistentVolumeClaim']['readOnly'])
            self.assertIn('books-original-', original['persistentVolumeClaim']['claimName'])
            app = next(c for c in pod['initContainers'] if c['name'] == 'application')
            books = [v for v in app['volumeMounts'] if v['name'] == 'books']
            self.assertEqual({v['mountPath'] for v in books}, {'/books', '/bookdrop'})
            self.assertTrue(all(v['readOnly'] for v in books))
        contract = next(d for d in docs if d.get('kind') == 'ConfigMap' and
                        d['metadata']['name'] == 'grimmory-books-consumer-contract')
        self.assertTrue({'restore.sh', 'runtime.sh', 'books.sh'} <= set(contract['data']))
        policy = next(d for d in docs if d.get('kind') == 'NetworkPolicy' and
                      d['metadata']['name'] == 'grimmory-k8s92-books-consumer')
        self.assertEqual(policy['spec']['ingress'], [])
        self.assertEqual(policy['spec']['egress'], [])


if __name__ == '__main__':
    unittest.main()
