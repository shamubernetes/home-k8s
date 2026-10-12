#!/usr/bin/env python3
"""CWA-BDL deployment contract, isolated from CI verifier changes."""
import hashlib
import json
from pathlib import Path
import subprocess
import unittest

APP = Path(__file__).resolve().parent
ROOT = APP.parents[4]


def yaml_json(text):
    return json.loads(subprocess.run(['yq', '-o=json', '.'], input=text,
                                    text=True, capture_output=True, check=True).stdout)


class StartupDeploymentTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.release = yaml_json((APP / 'helmrelease.yaml').read_text())
        baseline = subprocess.check_output(['git', 'show', 'origin/main:kubernetes/apps/arrs/cwa-bdl/app/helmrelease.yaml'], text=True, cwd=ROOT)
        cls.baseline = yaml_json(baseline)

    def test_holder_is_unchanged_qualified_source(self):
        source = ROOT / 'kubernetes/apps/media/calibre-web-automated/app/kopiur-startup-hold.py'
        self.assertEqual(hashlib.sha256(source.read_bytes()).digest(), hashlib.sha256((APP / 'kopiur-startup-hold.py').read_bytes()).digest())

    def test_shim_restores_exact_native_entrypoint(self):
        self.assertEqual((APP / 'kopiur-init.sh').read_text(), '#!/bin/sh\nexec /usr/bin/dumb-init -- /app/entrypoint.sh\n')
        controller = self.release['spec']['values']['controllers']['cwa-bdl']
        self.assertEqual(controller['containers']['app']['command'], ['/app/.venv/bin/python3', '/backup-contract/startup-hold.py', 'start', '0'])
        self.assertEqual(controller['containers']['app']['env']['KOPIUR_POD_UID']['valueFrom']['fieldRef']['fieldPath'], 'metadata.uid')

    def test_only_native_app_can_write_books_and_config(self):
        persistence = self.release['spec']['values']['persistence']
        for name in ('config', 'books'):
            mounts = persistence[name]['advancedMounts']['cwa-bdl']
            self.assertEqual(set(mounts), {'app', 'books-monitor', 'books-canary'})
            self.assertFalse(mounts['app'][0].get('readOnly', False))
            for reader in ('books-monitor', 'books-canary'):
                self.assertIs(mounts[reader][0]['readOnly'], True)
            self.assertNotIn('globalMounts', persistence[name])
        for mounts in persistence['books']['advancedMounts']['cwa-bdl'].values():
            self.assertEqual(mounts[0]['subPath'], 'Books/ingest')

    def test_contract_is_readonly_app_only_executable(self):
        contract = self.release['spec']['values']['persistence']['backup-contract']
        self.assertEqual(contract['defaultMode'], 0o555)
        self.assertEqual(contract['name'], 'cwa-bdl-kopiur-startup')
        mounts = contract['advancedMounts']['cwa-bdl']
        self.assertEqual(set(mounts), {'app'})
        self.assertTrue(all(m['readOnly'] for m in mounts['app']))
        self.assertEqual(mounts['app'][1], {'path':'/init', 'subPath':'native-init.sh', 'readOnly':True})

    def test_all_noncontract_release_fields_preserved(self):
        candidate = json.loads(json.dumps(self.release))
        values = candidate['spec']['values']
        app = values['controllers']['cwa-bdl']['containers']['app']
        app.pop('command')
        app['env'].pop('KOPIUR_POD_UID')
        values['persistence'] = self.baseline['spec']['values']['persistence']
        self.assertEqual(candidate, self.baseline)

    def test_kustomize_emits_exact_guard_and_native_shim(self):
        rendered = subprocess.check_output(['kubectl','kustomize',str(APP)], text=True)
        docs = subprocess.run(['yq','-o=json','-I=0','select(.kind == "ConfigMap" and .metadata.name == "cwa-bdl-kopiur-startup")'], input=rendered, text=True, capture_output=True, check=True)
        cm = json.loads(docs.stdout)
        self.assertEqual(cm['data']['startup-hold.py'], (APP/'kopiur-startup-hold.py').read_text())
        self.assertEqual(cm['data']['native-init.sh'], (APP/'kopiur-init.sh').read_text())


if __name__ == '__main__':
    unittest.main()
