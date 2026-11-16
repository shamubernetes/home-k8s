#!/usr/bin/env python3
"""Exercise the registry-only gate with a deterministic crane process."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
IMAGE = 'ghcr.io/example/app:v1@sha256:' + 'a' * 64
FAKE = '''#!/usr/bin/env python3
import json, os, sys, time
from pathlib import Path
command, image = sys.argv[1:3]
log = Path(os.environ['LOG'])
lines = log.read_text().splitlines() if log.exists() else []
log.write_text('\\n'.join([*lines, ' '.join(sys.argv[1:])]) + '\\n')
mode = os.environ['MODE']
if mode in ('missing', 'auth') or mode == 'retry' and not lines:
    print('registry denied or missing', file=sys.stderr)
    sys.exit(1)
if mode == 'timeout':
    time.sleep(2)
if command == 'digest':
    print('sha256:' + ('b' if mode == 'mismatch' else 'a') * 64)
elif mode == 'malformed':
    print('{')
elif command == 'config':
    assert sys.argv[3:] == ['--platform', 'linux/amd64']
    print(json.dumps({'os': 'linux', 'architecture': 'arm64' if mode == 'wrong-platform' else 'amd64'}))
elif command == 'manifest':
    assert sys.argv[3:] == ['--platform', 'linux/amd64']
    if mode == 'unresolved-index':
        print(json.dumps({'schemaVersion': 2, 'mediaType': 'application/vnd.oci.image.index.v1+json', 'manifests': []}))
    else:
        descriptor = {'digest': 'sha256:' + 'c' * 64, 'size': 42, 'mediaType': 'application/vnd.oci.image.config.v1+json'}
        layer = dict(descriptor, mediaType='application/vnd.oci.image.layer.v1.tar+gzip')
        manifest = {'schemaVersion': 2, 'mediaType': 'application/vnd.oci.image.manifest.v1+json', 'config': descriptor, 'layers': [layer]}
        if mode == 'bad-descriptor':
            manifest['layers'] = [{}]
        if mode == 'docker':
            manifest['mediaType'] = 'application/vnd.docker.distribution.manifest.v2+json'
        print(json.dumps(manifest))
else:
    sys.exit(99)
'''


class AvailabilityTests(unittest.TestCase):
    def run_gate(self, mode='single', images=None):
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            crane = temp / 'crane'
            crane.write_text(FAKE)
            crane.chmod(0o755)
            log = temp / 'log'
            env = dict(os.environ, CRANE=str(crane), LOG=str(log), MODE=mode)
            result = subprocess.run([
                sys.executable, str(ROOT / 'scripts/check-registry-image-availability'),
                '--images-json', json.dumps([IMAGE] if images is None else images),
                '--timeout', '0.2', '--retry-delay', '0.001',
            ], env=env, capture_output=True, text=True, timeout=10)
            return result, log.read_text().splitlines() if log.exists() else []

    def test_single_and_docker_manifest(self):
        for mode in ('single', 'docker'):
            with self.subTest(mode=mode):
                result, calls = self.run_gate(mode)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(len(calls), 3)
                self.assertIn('available linux/amd64', result.stdout)

    def test_multi_platform_resolution_is_explicit(self):
        # Crane resolves an index to its requested child. The fake models that
        # contract by returning the child only for explicit platform requests.
        # An unresolved index must never be accepted as an image.
        result, calls = self.run_gate('multi')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(all('--platform linux/amd64' in call for call in calls[1:]))
        result, _ = self.run_gate('unresolved-index')
        self.assertNotEqual(result.returncode, 0)

    def test_fail_closed(self):
        for mode in ('missing', 'auth', 'timeout', 'mismatch', 'malformed', 'wrong-platform', 'bad-descriptor'):
            with self.subTest(mode=mode):
                result, calls = self.run_gate(mode)
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertEqual(len(calls), 3 if mode in ('missing', 'auth', 'timeout') else 1 if mode == 'mismatch' else 2 if mode in ('malformed', 'wrong-platform') else 3)

    def test_bounded_retry_succeeds(self):
        result, calls = self.run_gate('retry')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(calls), 4)

    def test_empty_delta_never_contacts_registry(self):
        result, calls = self.run_gate('missing', [])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(calls, [])

    def test_invalid_input_never_contacts_registry(self):
        for images in (None, {}, ['ghcr.io/example/app:latest'], [False], [IMAGE, 'invalid']):
            if images is None:
                continue
            result, calls = self.run_gate(images=images)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(calls, [])

    def test_every_new_image_checked_and_duplicates_deduplicated(self):
        result, calls = self.run_gate(images=[IMAGE, IMAGE.replace('/app:', '/other:'), IMAGE])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(calls), 6)


if __name__ == '__main__':
    unittest.main()
