#!/usr/bin/env python3
"""Deployment parity for the qualified UID568 Chaptarr startup boundary."""
import copy
import json
from pathlib import Path
import subprocess
import unittest
ROOT = Path(__file__).resolve().parents[5]
APP = Path(__file__).resolve().parent

def yaml_json(text):
    return json.loads(subprocess.run(['yq', '-o=json', '.', '-'], input=text,
                                    text=True, capture_output=True, check=True).stdout)

class StartupDeployment(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.hr = yaml_json((APP / 'helmrelease.yaml').read_text())
        cls.values = cls.hr['spec']['values']

    def test_qualified_artifact_is_unchanged(self):
        self.assertEqual((APP / 'kopiur-startup-hold.sh').read_bytes(),
                         (ROOT / 'scripts/kopiur-chaptarr-startup-hold.sh').read_bytes())

    def test_expired_deadline_preserves_uid_and_native_args(self):
        app = self.values['controllers']['chaptarr']['containers']['app']
        self.assertEqual(app['command'], ['/bin/bash', '/backup-contract/startup-hold.sh'])
        self.assertEqual(app['args'], ['start', '0'])
        self.assertEqual(app['env']['KOPIUR_POD_UID']['valueFrom']['fieldRef']['fieldPath'], 'metadata.uid')
        security = self.values['defaultPodOptions']['securityContext']
        self.assertEqual([security['runAsUser'], security['runAsGroup']], [568, 568])
        self.assertTrue(security['runAsNonRoot'])
        self.assertEqual(app['securityContext']['capabilities'], {'drop': ['ALL']})

    def test_parent_and_direct_aliases_preserved(self):
        mounts = self.values['persistence']['media']['advancedMounts']['chaptarr']
        expected = [{'path': '/media'}, {'path': '/audiobooks', 'subPath': 'Audiobooks'},
                    {'path': '/ebooks', 'subPath': 'Books'}, {'path': '/downloads', 'subPath': 'Downloads'}]
        self.assertEqual(mounts['app'], expected)
        for init in ['init-db-main', 'init-db-log', 'init-db-cache']:
            self.assertEqual(mounts[init], [dict(m, readOnly=True) for m in expected])

    def test_guard_mount_is_app_only_readonly(self):
        p = self.values['persistence']['backup-contract']
        self.assertEqual(p['defaultMode'], 0o555)
        self.assertEqual(p['advancedMounts'], {'chaptarr': {'app': [{'path': '/backup-contract', 'readOnly': True}]}})

    def test_unrelated_release_fields_unchanged(self):
        relative = str((APP / 'helmrelease.yaml').relative_to(ROOT))
        original = yaml_json(subprocess.run(['git', 'show', 'origin/main:' + relative], cwd=ROOT,
                                           capture_output=True, text=True, check=True).stdout)
        def normalized(hr):
            hr = copy.deepcopy(hr)
            values = hr['spec']['values']
            app = values['controllers']['chaptarr']['containers']['app']
            app.pop('command', None)
            app.pop('args', None)
            app['env'].pop('KOPIUR_POD_UID', None)
            values['persistence'].pop('backup-contract', None)
            media = values['persistence']['media']
            if 'advancedMounts' in media:
                media['globalMounts'] = media.pop('advancedMounts')['chaptarr']['app']
            return hr
        self.assertEqual(normalized(self.hr), normalized(original))

    def test_kustomize_contains_exact_configmap_and_retains_resources(self):
        output = subprocess.run(['kubectl', 'kustomize', str(APP)], capture_output=True, text=True, check=True).stdout
        documents = subprocess.run(['yq', '-o=json', '-I=0', '.', '-'], input=output,
                                   capture_output=True, text=True, check=True).stdout
        resources = [json.loads(line) for line in documents.splitlines()]
        cms = [r for r in resources if r['kind'] == 'ConfigMap' and r['metadata']['name'] == 'chaptarr-kopiur-startup']
        self.assertEqual(len(cms), 1)
        self.assertEqual(cms[0]['metadata']['annotations']['kustomize.toolkit.fluxcd.io/substitute'], 'disabled')
        self.assertEqual(cms[0]['data']['startup-hold.sh'], (APP / 'kopiur-startup-hold.sh').read_text())
        self.assertTrue(any(r['kind'] == 'ReplicationSource' for r in resources))
        self.assertTrue(any(r['kind'] == 'HelmRelease' and r['metadata']['name'] == 'chaptarr' for r in resources))

if __name__ == '__main__':
    unittest.main()
