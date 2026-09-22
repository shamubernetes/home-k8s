"""Real Linux permission regression using disposable Docker storage.

Run explicitly: python3 scripts/tests/test_kopiur_private_file_access.py
Docker and yq are required. No production files or credentials are read.
"""
import io
import json
from pathlib import Path
import subprocess
import tarfile
import unittest
import uuid

IMAGE = 'alpine@sha256:14358309a308569c32bdc37e2e0e9694be33a9d99e68afb0f5ff33cc1f695dce'
REPO = Path(__file__).resolve().parents[2]


class PrivateFileAccess(unittest.TestCase):
    def test_cwa_mover_reads_private_files_without_write_override(self):
        policy = REPO / 'kubernetes/apps/arrs/cwa-bdl/app/kopiur-policy.yaml'
        mover = json.loads(subprocess.check_output([
            'yq', '-o=json', 'select(.kind == "SnapshotPolicy") | .spec.mover', str(policy)]))
        security = mover['securityContext']
        self.assertEqual(security['capabilities'], {'drop': ['ALL'], 'add': ['DAC_READ_SEARCH']})
        self.assertFalse(security['allowPrivilegeEscalation'])
        self.assertEqual(mover['podSecurityContext']['runAsUser'], 0)
        volume = 'kopiur-access-test-' + uuid.uuid4().hex
        subprocess.run(['docker', 'volume', 'create', volume], check=True, capture_output=True)
        try:
            archive = io.BytesIO()
            with tarfile.open(fileobj=archive, mode='w') as tar:
                for name, owner in [('.flask_secret', 568), ('hook-copy', 0)]:
                    data = b'non-secret-fixture\n'
                    info = tarfile.TarInfo(name)
                    info.uid = info.gid = owner
                    info.mode = 0o600
                    info.size = len(data)
                    tar.addfile(info, io.BytesIO(data))
            subprocess.run(['docker', 'run', '--rm', '-i', '--network=none',
                            '-v', volume + ':/fixture', IMAGE, 'tar', 'xf', '-', '-C', '/fixture'],
                           input=archive.getvalue(), check=True, capture_output=True)
            base = ['docker', 'run', '--rm', '--network=none', '--user=0:0',
                    '--cap-drop=ALL', '--security-opt=no-new-privileges',
                    '-v', volume + ':/fixture:ro']
            old = subprocess.run(base + [IMAGE, 'cat', '/fixture/.flask_secret'], capture_output=True)
            self.assertNotEqual(old.returncode, 0)
            self.assertIn(b'Permission denied', old.stderr)
            new = subprocess.run(base + ['--cap-add=' + security['capabilities']['add'][0],
                                         IMAGE, 'sh', '-ec',
                                         'cat /fixture/.flask_secret /fixture/hook-copy >/dev/null; '
                                         'stat -c "%u:%g:%a" /fixture/.flask_secret'],
                                 capture_output=True, text=True)
            self.assertEqual(new.returncode, 0, new.stderr)
            self.assertEqual(new.stdout.strip(), '568:568:600')
            # Even on a writable mount the read-only DAC capability grants no write bypass.
            writable = base.copy()
            writable[writable.index(volume + ':/fixture:ro')] = volume + ':/fixture'
            denied = subprocess.run(writable + ['--cap-add=DAC_READ_SEARCH', IMAGE,
                                                'sh', '-c', 'printf x >> /fixture/.flask_secret'],
                                    capture_output=True)
            self.assertNotEqual(denied.returncode, 0)
            self.assertIn(b'Permission denied', denied.stderr)
        finally:
            subprocess.run(['docker', 'volume', 'rm', volume], check=True, capture_output=True)


if __name__ == '__main__':
    unittest.main(verbosity=2)
