import copy
import importlib.util
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('vt_edge', ROOT / 'kubernetes/apps/observability/victoria-traces/recovery/disk-point.py')
assert spec and spec.loader
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class DiskPoint(unittest.TestCase):
    def setUp(self):
        self.pvc = {'kind': 'PersistentVolumeClaim', 'metadata': {'name': module.PVC, 'namespace': 'observability', 'uid': 'pvc1'}, 'status': {'phase': 'Bound'}}
        self.klass = {'kind': 'VolumeSnapshotClass', 'metadata': {'name': 'retained-rbd'}, 'deletionPolicy': 'Retain', 'driver': 'rook-ceph.rbd.csi.ceph.com'}

    def test_whole_volume_capture_without_partition_detach(self):
        result = module.render(self.pvc, self.klass)
        self.assertEqual(result['kind'], 'VolumeSnapshot')
        self.assertEqual(result['spec']['source']['persistentVolumeClaimName'], module.PVC)
        self.assertEqual(result['metadata']['annotations']['kopiur.home.arpa/recovery-state'], 'unqualified')
        self.assertNotIn('detach', repr(result))

    def test_unbound_wrong_pvc_and_delete_class_are_rejected(self):
        for path, value in [('name', 'wrong'), ('namespace', 'media')]:
            pvc = copy.deepcopy(self.pvc)
            pvc['metadata'][path] = value
            with self.assertRaises(ValueError):
                module.render(pvc, self.klass)
        self.klass['deletionPolicy'] = 'Delete'
        with self.assertRaises(ValueError):
            module.render(self.pvc, self.klass)


if __name__ == '__main__':
    unittest.main()
