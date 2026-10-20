import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('vm_edge', ROOT / 'kubernetes/apps/observability/victoria-metrics-ha/recovery/native-point.py')
assert spec and spec.loader
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class NativePoint(unittest.TestCase):
    def test_delete_class_cannot_create_native_point(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(module, 'get', side_effect=[{'metadata': {'uid': 'pvc1'}}, {'deletionPolicy': 'Delete'}]), patch.object(module, 'api') as api:
            with self.assertRaises(ValueError):
                module.prepare('http://127.0.0.1:8428', Path(tmp) / 'state', 'delete-class')
            api.assert_not_called()

    def test_lost_create_response_is_not_retried(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(module, 'get', side_effect=lambda k,n: {'metadata': {'uid': 'pvc1'}} if k == 'pvc' else {'deletionPolicy': 'Retain'}), patch.object(module, 'api', side_effect=TimeoutError) as api:
            path = Path(tmp) / 'state'
            with self.assertRaises(TimeoutError):
                module.prepare('http://127.0.0.1:8428', path, 'retain-class')
            self.assertEqual(json.loads(path.read_text())['state'], 'creating')
            with self.assertRaises(FileExistsError):
                module.prepare('http://127.0.0.1:8428', path, 'retain-class')
            self.assertEqual(api.call_count, 1)

    def test_release_requires_ready_and_matching_retained_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'state'
            with patch.object(module, 'get', side_effect=lambda k,n: {'metadata': {'uid': 'pvc1'}} if k == 'pvc' else {'deletionPolicy': 'Retain'}), patch.object(module, 'api', return_value={'status': 'ok', 'snapshot': '20261020-1234'}):
                snap = module.prepare('http://127.0.0.1:8428', path, 'retain-class')
            snap['metadata']['uid'] = 'snap1'
            snap['status'] = {'readyToUse': False, 'boundVolumeSnapshotContentName': 'content1'}
            content = {'metadata': {'name': 'content1'}, 'spec': {'deletionPolicy': 'Retain', 'volumeSnapshotRef': {'uid': 'snap1'}}, 'status': {'readyToUse': True}}
            def get(kind, name):
                return {'volumesnapshot': snap, 'pvc': {'metadata': {'uid': 'pvc1'}}, 'volumesnapshotcontent': content}[kind]
            with patch.object(module, 'get', get), patch.object(module, 'api', return_value={'status': 'ok', 'snapshots': []}) as api:
                with self.assertRaises(ValueError):
                    module.release('http://127.0.0.1:8428', path)
                api.assert_not_called()
                snap['status']['readyToUse'] = True
                content['spec']['volumeSnapshotRef']['uid'] = 'unrelated'
                with self.assertRaises(ValueError):
                    module.release('http://127.0.0.1:8428', path)
                api.assert_not_called()
                content['spec']['volumeSnapshotRef']['uid'] = 'snap1'
                result = module.release('http://127.0.0.1:8428', path)
                self.assertEqual(result['state'], 'released')
                self.assertEqual(api.call_count, 2)
                self.assertIn('snapshot=20261020-1234', api.call_args_list[0].args[1])
                self.assertEqual(api.call_args_list[1].args[1], '/snapshot/list')


if __name__ == '__main__':
    unittest.main()
