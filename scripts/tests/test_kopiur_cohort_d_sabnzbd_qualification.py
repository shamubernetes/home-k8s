"""Cohort D SABnzbd source-only qualification, synthetic fixtures only."""
from contextlib import closing
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
QUAL = ROOT / 'kubernetes/apps/arrs/sabnzbd/qualification'
SPEC = importlib.util.spec_from_file_location('sab_original', QUAL / 'verify-original.py')
assert SPEC is not None and SPEC.loader is not None
VERIFY = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(VERIFY)


class OriginalQualificationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.nas = Path(self.tmp.name) / 'nas'
        self.r2 = Path(self.tmp.name) / 'r2'
        stage = self.nas / '.kopiur-consistent'
        stage.mkdir(parents=True)
        database = stage / 'database.sqlite3'
        with closing(sqlite3.connect(database)) as connection:
            connection.execute('CREATE TABLE history (id INTEGER PRIMARY KEY, state TEXT)')
            connection.execute("INSERT INTO history VALUES (1, 'synthetic-persisted-history')")
            connection.commit()
        (stage / 'manifest.json').write_text(json.dumps({
            'version': 1, 'database': 'admin/history1.db', 'integrity_check': 'ok',
            'sha256': hashlib.sha256(database.read_bytes()).hexdigest()}))
        (self.nas / 'sabnzbd.ini').write_text('[misc]\nconfig_version = 19\n')
        shutil.copytree(self.nas, self.r2)

    def test_identical_originals_are_not_retirement_acceptance(self):
        result = VERIFY.verify(self.nas, self.r2)
        self.assertTrue(result['nativeFilesystemEquality'])
        self.assertEqual(result['evidence']['rows'], 1)
        for gate in ['applicationBootProved', 'runtimeKeyEscrowProved',
                     'processContinuityProved', 'retirementAllowed']:
            self.assertFalse(result[gate])

    def test_same_root_rejected(self):
        with self.assertRaises(ValueError):
            VERIFY.verify(self.nas, self.nas)

    def test_changed_config_rejected(self):
        (self.r2 / 'sabnzbd.ini').write_text('[misc]\nchanged = yes\n')
        with self.assertRaises(ValueError):
            VERIFY.verify(self.nas, self.r2)

    def test_changed_db_rejected(self):
        database = self.r2 / '.kopiur-consistent/database.sqlite3'
        with closing(sqlite3.connect(database)) as connection:
            connection.execute("INSERT INTO history VALUES (2, 'different')")
            connection.commit()
        with self.assertRaises(ValueError):
            VERIFY.verify(self.nas, self.r2)

    def test_missing_config_rejected(self):
        (self.r2 / 'sabnzbd.ini').unlink()
        with self.assertRaises(ValueError):
            VERIFY.verify(self.nas, self.r2)

    def test_symlink_member_rejected(self):
        (self.r2 / 'symlink').symlink_to(self.r2 / 'sabnzbd.ini')
        with self.assertRaises(ValueError):
            VERIFY.verify(self.nas, self.r2)

    def test_sidecars_rejected_even_when_both_roots_match(self):
        for root in [self.nas, self.r2]:
            (root / '.kopiur-consistent/database.sqlite3-wal').write_bytes(b'leftover')
        with self.assertRaises(ValueError):
            VERIFY.verify(self.nas, self.r2)

    def test_invalid_manifest_rejected(self):
        path = self.r2 / '.kopiur-consistent/manifest.json'
        data = json.loads(path.read_text())
        data['database'] = '../unsafe'
        path.write_text(json.dumps(data))
        with self.assertRaises(ValueError):
            VERIFY.verify(self.nas, self.r2)

    def test_manifests_exact_ids_strict_options_and_new_targets(self):
        result = subprocess.run(['yq', '-o=json', '-I=0', '.', str(QUAL / 'restores.yaml')],
                                capture_output=True, text=True, check=True)
        docs = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
        self.assertEqual(len(docs), 2)
        contract = json.loads((QUAL / 'cutover-gates.json').read_text())
        self.assertFalse(contract['retirementAllowed'])
        for backend, doc in zip(['nas', 'r2'], docs):
            spec = doc['spec']
            self.assertEqual(spec['source']['identity']['snapshotID'],
                             contract['sourcePair'][backend + 'SnapshotID'])
            self.assertNotEqual(spec['target']['pvc']['name'], 'sabnzbd')
            self.assertFalse(spec['options']['ignoreErrors'])
            self.assertFalse(spec['options']['ignorePermissionErrors'])
            self.assertFalse(spec['credentialProjection']['enabled'])
            self.assertEqual(spec['failurePolicy']['backoffLimit'], 0)
        kustomization = (QUAL.parent / 'app/kustomization.yaml').read_text()
        self.assertIn('- ../qualification', kustomization)
        qualification = (QUAL / 'kustomization.yaml').read_text()
        self.assertIn('- ./restores.yaml', qualification)
        self.assertIn('- ./native-proof.yaml', qualification)
        self.assertIn('../../../../templates/volsync', kustomization)


if __name__ == '__main__':
    unittest.main()
