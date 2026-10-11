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
DATABASE_APPS = {
    'arrs': ('bazarr', 'radarr', 'radarr-3d', 'sonarr', 'whisparr'),
    'media': ('grimmory', 'tubearchivist'),
}


def mover_labels(app):
    return [('config', app), ('config', app + '-nas-smb'),
            ('config', app + '-r2'), ('maintenance', app + '-nas-smb'),
            ('maintenance', app + '-r2'),
            ('snapshot-replication', app + '-nas-to-r2'), ('verify', app)]


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
            for key, value in mover_labels(app):
                with self.subTest(key=key, value=value):
                    self.assertEqual(self.mutated_deadline(key, value), 3600)

    def test_media_cohort_job_types_are_capped(self):
        for app in ('kometa', 'audiobookshelf'):
            labels = mover_labels(app) + [('op', 'snapshot-delete-batch')]
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

    def test_database_cohort_job_types_are_capped(self):
        for namespace, apps in DATABASE_APPS.items():
            for app in apps:
                for key, value in mover_labels(app):
                    with self.subTest(namespace=namespace, key=key, value=value):
                        self.assertEqual(self.mutated_deadline(key, value, namespace), 3600)

    def test_database_allowlists_are_namespace_and_manager_scoped(self):
        namespaces = ('arrs', 'media', 'kopiur-canary', 'observability', 'services', 'default')
        for namespace, apps in DATABASE_APPS.items():
            excluded = [(other, 'kopiur') for other in namespaces if other != namespace]
            excluded += [(namespace, manager) for manager in ('other-controller', 'volsync')]
            for app in apps:
                for key, value in mover_labels(app):
                    for other, manager in excluded:
                        with self.subTest(key=key, value=value, namespace=other, manager=manager):
                            self.assertEqual(
                                self.mutated_deadline(key, value, other, manager), 172800)

    def test_database_allowlists_require_exact_label_values(self):
        for namespace, apps in DATABASE_APPS.items():
            for app in apps:
                labels = [('config', app + '-unrelated'), ('maintenance', app),
                          ('maintenance', app + '-nas'), ('snapshot-replication', app),
                          ('verify', app + '-r2'), ('unrelated-label', app)]
                for key, value in labels:
                    with self.subTest(namespace=namespace, key=key, value=value):
                        self.assertEqual(self.mutated_deadline(key, value, namespace), 172800)

    def test_deletion_coverage_and_volsync_exclusion_are_preserved(self):
        for namespace in ('arrs', 'media', 'kopiur-canary', 'observability', 'services'):
            for manager, expected in [('kopiur', 3600), ('volsync', 172800)]:
                with self.subTest(namespace=namespace, manager=manager):
                    self.assertEqual(self.mutated_deadline(
                        'op', 'snapshot-delete-batch', namespace, manager), expected)
        self.assertEqual(self.mutated_deadline('op', 'snapshot-delete-batch', 'default'), 172800)

    def test_original_replications_have_existing_scoped_cap(self):
        for app in ('radarr', 'whisparr'):
            value = app + '-k8s92-original-to-r2'
            with self.subTest(app=app):
                self.assertEqual(self.mutated_deadline('snapshot-replication', value), 3600)
            for arguments in [('snapshot-replication', value, 'default'),
                              ('snapshot-replication', value, 'media'),
                              ('snapshot-replication', value, 'arrs', 'volsync'),
                              ('snapshot-replication', value + '-unrelated'),
                              ('config', value)]:
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
