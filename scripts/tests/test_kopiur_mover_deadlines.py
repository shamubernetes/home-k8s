"""Exercise the real Kyverno mutation offline. Requires kyverno and yq.

mise exec -- python3 -m unittest discover -s scripts/tests -p test_kopiur_mover_deadlines.py -v
"""
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[2]
POLICY = REPO / 'kubernetes/apps/kyverno/kyverno/policies/kopiur-movers.yaml'
PREFIX = 'kopiur.home-operations.com/'
APPS = ('cwa-bdl', 'sabnzbd', 'listenarr')


class MoverDeadlineTests(unittest.TestCase):
    def mutated_deadline(self, key, value, namespace='arrs', manager='kopiur'):
        job = {
            'apiVersion': 'batch/v1', 'kind': 'Job',
            'metadata': {'name': 'deadline-fixture', 'namespace': namespace,
                         'labels': {'app.kubernetes.io/managed-by': manager,
                                    PREFIX + key: value}},
            'spec': {'activeDeadlineSeconds': 172800,
                     'template': {'spec': {'restartPolicy': 'Never', 'containers': [
                         {'name': 'mover', 'image': 'fixture.invalid/not-executed:v1'}]}}},
        }
        with tempfile.TemporaryDirectory() as tmp:
            resource = Path(tmp) / 'resource.json'
            output = Path(tmp) / 'mutated.yaml'
            resource.write_text(json.dumps(job))
            result = subprocess.run(
                ['kyverno', 'apply', str(POLICY), '--resource', str(resource),
                 '--output', str(output), '--remove-color'],
                capture_output=True, text=True, timeout=30,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertTrue(output.exists(), result.stdout + result.stderr)
            self.assertTrue(output.read_text().strip(), result.stdout + result.stderr)
            parsed = subprocess.run(
                ['yq', '-o=json', '-I=0', 'select(. != null)', str(output)],
                capture_output=True, text=True, check=True,
            )
            documents = [json.loads(line) for line in parsed.stdout.splitlines() if line.strip()]
            self.assertEqual(len(documents), 1)
            return documents[0]['spec']['activeDeadlineSeconds']

    def test_cohort_job_types_are_capped(self):
        for app in APPS:
            labels = [('config', app), ('config', app + '-nas-smb'),
                      ('config', app + '-r2'), ('maintenance', app + '-nas-smb'),
                      ('maintenance', app + '-r2'),
                      ('snapshot-replication', app + '-nas-to-r2'), ('verify', app)]
            for key, value in labels:
                with self.subTest(key=key, value=value):
                    self.assertEqual(self.mutated_deadline(key, value), 3600)

    def test_media_cohort_job_types_are_capped(self):
        for app in ('kometa', 'audiobookshelf'):
            labels = [('config', app), ('config', app + '-nas-smb'),
                      ('config', app + '-r2'), ('maintenance', app + '-nas-smb'),
                      ('maintenance', app + '-r2'),
                      ('snapshot-replication', app + '-nas-to-r2'), ('verify', app),
                      ('op', 'snapshot-delete-batch')]
            for key, value in labels:
                with self.subTest(key=key, value=value):
                    self.assertEqual(self.mutated_deadline(key, value, 'media'), 3600)

    def test_media_allowlist_does_not_expand_other_namespaces(self):
        for arguments in [('config', 'kometa', 'arrs'),
                          ('config', 'audiobookshelf', 'default'),
                          ('config', 'unrelated', 'media'),
                          ('config', 'tunarr', 'media'),
                          ('maintenance', 'tunarr-nas-smb', 'media'),
                          ('snapshot-replication', 'tunarr-nas-to-r2', 'media'),
                          ('verify', 'tunarr', 'media'),
                          ('config', 'kometa', 'media', 'other-controller')]:
            with self.subTest(arguments=arguments):
                self.assertEqual(self.mutated_deadline(*arguments), 172800)

    def test_existing_coverage_is_preserved(self):
        for key, value in [('config', 'seerr'), ('maintenance', 'profilarr-nas-smb'),
                           ('snapshot-replication', 'seerr-nas-to-r2'),
                           ('verify', 'profilarr'), ('op', 'snapshot-delete-batch')]:
            with self.subTest(key=key, value=value):
                self.assertEqual(self.mutated_deadline(key, value), 3600)

    def test_unrelated_jobs_are_unchanged(self):
        for arguments in [('config', 'unrelated'), ('config', 'cwa-bdl', 'default'),
                          ('config', 'cwa-bdl', 'arrs', 'other-controller'),
                          ('unrelated-label', 'cwa-bdl')]:
            with self.subTest(arguments=arguments):
                self.assertEqual(self.mutated_deadline(*arguments), 172800)


if __name__ == '__main__':
    unittest.main()
