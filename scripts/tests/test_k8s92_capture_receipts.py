"""API-contract regressions, not live Kubernetes or provider acceptance."""
import copy
from email.message import Message
import importlib.util
import json
from pathlib import Path
import unittest
import urllib.error

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('capture', ROOT / 'scripts/k8s92_infrastructure_capture.py')
assert spec is not None and spec.loader is not None
capture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(capture)


class API:
    def __init__(self):
        self.data = {}
        self.active = False
        self.job_terminal = True
        self.conflict = False
        self.writes = []

    def call(self, method, path, body=None):
        if method == 'GET':
            if '/pods?' in path:
                return {'items': [{'status': {'phase': 'Running'}}] if self.active else []}
            if '/jobs/' in path:
                return {'status': {'conditions': [{'type': 'Complete', 'status': 'True'}]} if self.job_terminal else {}}
            if path not in self.data:
                raise urllib.error.HTTPError(path, 404, 'not found', Message(), None)
            return copy.deepcopy(self.data[path])
        assert body is not None
        self.writes.append((method, path))
        target = path + '/' + body['metadata']['name'] if method == 'POST' else path
        if method == 'POST' and target in self.data:
            raise urllib.error.HTTPError(path, 409, 'exists', Message(), None)
        if method == 'PUT' and (self.conflict or body['metadata']['resourceVersion'] != self.data[target]['metadata']['resourceVersion']):
            raise urllib.error.HTTPError(path, 409, 'conflict', Message(), None)
        obj = copy.deepcopy(body)
        obj['metadata']['resourceVersion'] = str(int(obj['metadata'].get('resourceVersion', '0')) + 1)
        self.data[target] = obj
        return copy.deepcopy(obj)


class Receipts(unittest.TestCase):
    def setUp(self):
        self.api = API()
        self.worker = capture.Capture(self.api, {'app': 'atproto-pds', 'namespace': 'drill'})
        self.worker.run = 'new'
        self.state = {'run': 'old', 'phase': 'complete', 'workerJob': 'old-worker',
                      'bundle': json.dumps([{'source': 'atproto-pds', 'backup': {'kopiaSnapshotID': 'point'}}])}
        self.api.data[self.worker.lock] = {'metadata': {'name': 'atproto-pds-capture-lock', 'resourceVersion': '4'}, 'data': self.state}
        self.lock = {'metadata': {'name': 'atproto-pds-capture-lock'}, 'data': {'run': 'new', 'phase': 'arming'}}

    def test_completed_run_archived_before_compare_and_swap(self):
        self.worker.acquire(self.lock)
        archive = self.api.data[self.worker.core + '/configmaps/atproto-pds-capture-receipt-old']
        self.assertTrue(archive['immutable'])
        self.assertEqual(archive['data'], self.state)
        self.assertEqual(self.api.data[self.worker.lock]['data'], self.lock['data'])
        self.assertEqual(self.api.data[self.worker.lock]['metadata']['resourceVersion'], '5')

    def test_failed_or_unfinished_runs_never_stolen(self):
        for phase in ['arming', 'quiescing', 'resumed', 'resume-failed', 'watchdog-resumed']:
            self.api.data[self.worker.lock]['data']['phase'] = phase
            with self.assertRaisesRegex(RuntimeError, 'operator recovery'):
                self.worker.acquire(self.lock)
            self.assertEqual(self.api.data[self.worker.lock]['data']['run'], 'old')

    def test_second_completed_generation_retains_both_receipts(self):
        self.worker.acquire(self.lock)
        completed = self.state | {'run': 'new', 'workerJob': 'new-worker'}
        self.api.data[self.worker.lock]['data'] = completed
        third = {'metadata': {'name': 'atproto-pds-capture-lock'},
                 'data': {'run': 'third', 'phase': 'arming'}}
        self.worker.acquire(third)
        for run in ['old', 'new']:
            saved = self.api.data[self.worker.core + '/configmaps/atproto-pds-capture-receipt-' + run]
            self.assertEqual(saved['data']['run'], run)
            self.assertTrue(saved['immutable'])
        self.assertEqual(self.api.data[self.worker.lock]['data']['run'], 'third')

    def test_running_job_rejected(self):
        self.api.job_terminal = False
        with self.assertRaisesRegex(RuntimeError, 'not terminal'):
            self.worker.acquire(self.lock)
        self.assertEqual(self.api.data[self.worker.lock]['data']['run'], 'old')

    def test_running_or_terminating_pod_rejected(self):
        self.api.active = True
        with self.assertRaisesRegex(RuntimeError, 'active or terminating'):
            self.worker.acquire(self.lock)
        self.assertEqual(self.api.data[self.worker.lock]['data']['run'], 'old')

    def test_concurrent_successor_conflict_preserves_lock(self):
        self.api.conflict = True
        with self.assertRaises(urllib.error.HTTPError):
            self.worker.acquire(self.lock)
        self.assertEqual(self.api.data[self.worker.lock]['data']['run'], 'old')

    def test_archive_is_idempotent_and_read_back(self):
        self.worker.archive(self.state)
        self.worker.archive(self.state)
        self.assertEqual(len(self.api.data), 2)
        self.api.data[self.worker.core + '/configmaps/atproto-pds-capture-receipt-old']['data']['run'] = 'other'
        with self.assertRaisesRegex(RuntimeError, 'receipt conflict'):
            self.worker.archive(self.state)

    def test_empty_receipt_rejected(self):
        with self.assertRaisesRegex(RuntimeError, 'complete recovery'):
            self.worker.archive(self.state | {'bundle': '[]'})

    def test_replaced_source_pvc_denied_before_clone_creation(self):
        path = self.worker.core + '/persistentvolumeclaims/atproto-pds'
        self.api.data[path] = {'metadata': {'uid': 'replacement'}, 'spec': {'volumeName': 'replacement-pv'}}
        with self.assertRaisesRegex(RuntimeError, 'PVC replaced'):
            self.worker.upload([{'source': 'atproto-pds', 'snapshot': 'point', 'sourcePVC': {'uid': 'original', 'volumeName': 'original-pv'}}])
        self.assertEqual(self.api.writes, [])


if __name__ == '__main__':
    unittest.main(verbosity=2)
