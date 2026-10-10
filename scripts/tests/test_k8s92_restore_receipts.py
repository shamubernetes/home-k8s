"""Synthetic receipt validation only, not live CSI or application acceptance."""
import copy
import importlib.util
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts'))
spec = importlib.util.spec_from_file_location('restore', ROOT / 'scripts/k8s92_infrastructure_restore.py')
assert spec is not None and spec.loader is not None
restore = importlib.util.module_from_spec(spec)
spec.loader.exec_module(restore)

APP = 'atproto-pds'
RUN = 'a' * 32
NAS = 'b' * 32
R2 = 'c' * 32


class RestoreReceipts(unittest.TestCase):
    def setUp(self):
        self.point = {'source': APP, 'snapshot': APP + '-' + RUN + '-0',
                      'kopiurSnapshot': APP + '-' + RUN + '-0',
                      'sourcePVC': {'uid': 'fixture-pvc', 'resourceVersion': '1', 'volumeName': 'fixture-pv'},
                      'status': {'snapshotUID': 'fixture-csi', 'boundVolumeSnapshotContentName': 'fixture-content', 'restoreSize': '1Gi'},
                      'backup': {'kopiaSnapshotID': NAS, 'identity': {'username': APP, 'hostname': 'services', 'sourcePath': '/pvc/' + APP}}}
        self.receipt = {'immutable': True,
                        'metadata': {'name': APP + '-capture-receipt-' + RUN, 'namespace': 'services'},
                        'data': {'run': RUN, 'phase': 'complete', 'bundle': json.dumps([self.point])}}
        self.mapping = {'run': RUN, 'namespace': 'services', 'repository': APP + '-r2',
                        'points': {APP: {'sourceNASID': NAS, 'snapshotID': R2}}}

    def render(self, tier='nas-smb'):
        return restore.render(APP, self.receipt, 'pds-recovery-fixture', tier,
                              self.mapping if tier == 'r2' else None)

    def store(self, points=None):
        self.receipt['data']['bundle'] = json.dumps([self.point] if points is None else points)

    def test_nas_exact_point_and_owner_lineage(self):
        obj = self.render()['items'][-1]
        self.assertEqual(obj['spec']['source']['identity']['snapshotID'], NAS)
        self.assertEqual(obj['metadata']['annotations']['recovery.home.arpa/source-pvc-uid'], 'fixture-pvc')
        self.assertEqual(obj['spec']['target']['pvc']['capacity'], '1Gi')
        self.assertFalse(obj['spec']['options']['skipOwners'])
        self.assertFalse(obj['spec']['options']['ignoreErrors'])
        self.assertEqual(self.render()['items'][0]['spec']['egress'], [])

    def test_r2_exact_destination_point(self):
        obj = self.render('r2')['items'][-1]
        self.assertEqual(obj['spec']['source']['identity']['snapshotID'], R2)
        self.assertEqual(obj['spec']['repository']['name'], APP + '-r2')

    def test_mutable_or_wrong_namespace_receipt_rejected(self):
        original = copy.deepcopy(self.receipt)
        for mutate in [lambda r: r.update(immutable=False), lambda r: r['metadata'].update(namespace='other'),
                       lambda r: r['metadata'].update(name=APP + '-capture-lock'), lambda r: r['data'].update(phase='failed')]:
            self.receipt = copy.deepcopy(original)
            mutate(self.receipt)
            with self.assertRaises(ValueError):
                self.render()

    def test_partial_or_duplicate_source_rejected(self):
        for points in [[], [self.point, self.point]]:
            self.store(points)
            with self.assertRaises(ValueError):
                self.render()

    def test_missing_lineage_rejected(self):
        for section in ['sourcePVC', 'status']:
            original = self.point.pop(section)
            self.store()
            with self.assertRaises(ValueError):
                self.render()
            self.point[section] = original

    def test_wrong_snapshot_generation_rejected(self):
        self.point['snapshot'] = APP + '-' + 'd' * 32 + '-0'
        self.store()
        with self.assertRaises(ValueError):
            self.render()

    def test_wrong_native_identity_rejected(self):
        self.point['backup']['identity']['hostname'] = 'other'
        self.store()
        with self.assertRaises(ValueError):
            self.render()

    def test_malformed_id_rejected(self):
        self.point['backup']['kopiaSnapshotID'] = 'latest'
        self.store()
        with self.assertRaises(ValueError):
            self.render()

    def test_r2_mixed_generation_rejected(self):
        self.mapping['run'] = 'd' * 32
        with self.assertRaises(ValueError):
            self.render('r2')

    def test_r2_wrong_nas_point_rejected(self):
        self.mapping['points'][APP]['sourceNASID'] = 'd' * 32
        with self.assertRaises(ValueError):
            self.render('r2')

    def test_production_namespace_and_unknown_tier_rejected(self):
        for namespace, tier in [('services', 'nas-smb'), ('pds-recovery-fixture', 'unknown')]:
            with self.assertRaises(ValueError):
                restore.render(APP, self.receipt, namespace, tier)


if __name__ == '__main__':
    unittest.main(verbosity=2)
