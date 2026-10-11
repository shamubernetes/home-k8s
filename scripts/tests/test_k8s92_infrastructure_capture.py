#!/usr/bin/env python3
"""Run the actual worker/watchdog processes against a local API contract fixture.

The fixture drives a real disposable Docker writer. CSI and Kopiur are simulated,
not claimed as cluster acceptance. Failure tests exercise real process cleanup.
"""
import copy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / 'scripts/k8s92_infrastructure_capture.py'
IMAGE = 'python:3.13-alpine@sha256:399babc8b49529dabfd9c922f2b5eea81d611e4512e3ed250d75bd2e7683f4b0'
WRITER = '''import os,signal,sqlite3,time
os.makedirs('/data',exist_ok=True)
c=sqlite3.connect('/data/state.sqlite')
c.execute('create table if not exists records (n integer)'); c.commit()
def stop(*args):
 c.execute('insert into records values (-1)'); c.commit(); c.close(); raise SystemExit(0)
signal.signal(signal.SIGTERM,stop)
while True:
 c.execute('insert into records values (1)'); c.commit(); time.sleep(.1)
'''


def docker(*args):
    return subprocess.run(['docker', *args], check=True, capture_output=True, text=True, timeout=30).stdout.strip()


class Fixture:
    def __init__(self, directory, failure):
        self.directory, self.failure = Path(directory), failure
        self.name = 'k8s92-drill-' + uuid.uuid4().hex[:10]
        self.ns = '/api/v1/namespaces/drill'
        self.scale = '/apis/apps/v1/namespaces/drill/deployments/writer/scale'
        self.data = {self.scale: {'metadata': {'uid': 'writer-uid', 'resourceVersion': '1'},
                                  'spec': {'replicas': 1}},
            self.ns + '/persistentvolumeclaims/writer': {'metadata': {'uid': 'source-uid', 'resourceVersion': '1'}, 'spec': {'volumeName': 'source-pv', 'resources': {'requests': {'storage': '1Gi'}}}, 'status': {'phase': 'Bound'}}}
        self.children, self.uploads = [], []
        self.pvcs = ['writer', 'writer-auth', 'writer-models'] if failure.startswith('multi-') else ['writer']
        for pvc in self.pvcs[1:]:
            self.data[self.ns + '/persistentvolumeclaims/' + pvc] = copy.deepcopy(self.data[self.ns + '/persistentvolumeclaims/writer'])
        self.mutex = threading.RLock()
        self.config = self.directory / 'config.json'
        self.config.write_text(json.dumps({'namespace': 'drill', 'app': 'writer',
            'workloads': ['deployments/writer'], 'pvcs': self.pvcs, 'image': IMAGE,
            'quiesceSeconds': 18 if failure == 'timeout' else 40}))
        docker('run', '-d', '--network=none', '--name', self.name, IMAGE, 'python3', '-c', WRITER)
        owner = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                pass
            def do_GET(self): self.respond('GET')
            def do_POST(self): self.respond('POST')
            def do_PUT(self): self.respond('PUT')
            def respond(self, method):
                body = json.loads(self.rfile.read(int(self.headers.get('Content-Length', 0))) or 'null')
                try:
                    with owner.mutex:
                        code, result = owner.call(method, self.path, body)
                except Exception:
                    code, result = 500, {'message': 'fixture failure'}
                self.send_response(code)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps(result).encode())
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.data['/apis/batch/v1/namespaces/drill/jobs/fixture-worker'] = {'metadata': {'name': 'fixture-worker', 'resourceVersion': '1'}, 'spec': {}}
        self.env = os.environ | {'CAPTURE_JOB_NAME': 'fixture-worker', 'CAPTURE_API': 'http://127.0.0.1:' + str(self.server.server_port)}

    def launch(self, mode, *args):
        process = subprocess.Popen([sys.executable, str(SCRIPT), mode, '--config', str(self.config), *args],
                                   env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.children.append(process)
        if mode == 'capture':
            self.worker = process
        return process

    def running(self):
        return docker('inspect', '--format', '{{.State.Running}}', self.name) == 'true'

    def call(self, method, path, body):
        if method == 'GET':
            if '/helmreleases/' in path:
                return 200, {'spec': {'suspend': self.failure != 'preflight'}}
            if path == self.scale.removesuffix('/scale'):
                return 200, {'metadata': {'uid': 'writer-uid'}, 'status': {'readyReplicas': int(self.running())}}
            if '/pods?labelSelector=' in path:
                return 200, {'items': [] if self.worker.poll() is not None else [{'metadata': {'name': 'worker'}}]}
            if path.endswith('/pods'):
                pods = [{'spec': {'volumes': [{'persistentVolumeClaim': {'claimName': 'writer'}}]}}] if self.running() else []
                return 200, {'items': pods}
            return (200, copy.deepcopy(self.data[path])) if path in self.data else (404, {})
        if method == 'PUT':
            original = self.data.get(path, {})
            if body['metadata'].get('resourceVersion') != original.get('metadata', {}).get('resourceVersion'):
                return 409, {}
            if path.endswith('/jobs/fixture-worker') and body['spec'].get('suspend'):
                self.worker.kill()
                self.worker.wait(timeout=5)
            if path == self.scale:
                replicas = body['spec']['replicas']
                # Ambiguous stop response: the write happened, client sees failure.
                if replicas == 0:
                    docker('stop', '-t', '5', self.name)
                else:
                    docker('start', self.name)
            body['metadata']['resourceVersion'] = str(int(original['metadata'].get('resourceVersion', '0')) + 1)
            self.data[path] = copy.deepcopy(body)
            if path == self.scale and body['spec']['replicas'] == 0 and self.failure == 'stop':
                return 500, {}
            return 200, copy.deepcopy(body)
        name = body['metadata']['name']
        target = path + '/' + name
        if target in self.data:
            return 409, {}
        body.setdefault('metadata', {})['resourceVersion'] = '1'
        body['metadata']['uid'] = uuid.uuid4().hex
        if path.endswith('/jobs'):
            self.launch('watchdog', '--run', name.split('-resume-')[1])
        if path.endswith('/volumesnapshots'):
            if self.running():
                raise RuntimeError('snapshot while writer running')
            if self.failure == 'csi' or (self.failure == 'multi-csi' and name.endswith('-1')):
                body['status'] = {'error': {'message': 'injected CSI error'}}
            elif self.failure in ('timeout', 'kill', 'pause'):
                body['status'] = {}
            else:
                capture_dir = 'captured' if name.endswith('-0') else 'captured-' + name.rsplit('-', 1)[1]
                docker('cp', self.name + ':/data', str(self.directory / capture_dir))
                body['status'] = {'readyToUse': True, 'boundVolumeSnapshotContentName': 'local-copy'}
        if path.endswith('/snapshots'):
            if not self.running():
                raise RuntimeError('upload started while app stopped')
            self.uploads.append(name)
            # Long transfer starts after resume; transfer failure must not stop app.
            time.sleep(.2)
            body['status'] = {'phase': 'Failed'} if self.failure == 'upload' else {
                'phase': 'Succeeded', 'snapshot': {'kopiaSnapshotID': 'fixture-only', 'identity': {}}}
        self.data[target] = copy.deepcopy(body)
        return 201, copy.deepcopy(body)

    def close(self):
        for child in self.children:
            if child.poll() is None:
                child.terminate()
            try:
                child.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                child.kill(); child.communicate()
        self.server.shutdown()
        self.server.server_close()
        docker('rm', '-f', self.name)


@unittest.skipUnless(os.environ.get('K8S92_DOCKER_TESTS') == '1', 'set K8S92_DOCKER_TESTS=1 for real local processes')
class OrchestrationTests(unittest.TestCase):
    def exercise(self, failure):
        with tempfile.TemporaryDirectory(prefix='k8s92-orchestration-') as directory:
            fixture = Fixture(directory, failure)
            try:
                worker = fixture.launch('capture')
                if failure in ('kill', 'pause'):
                    deadline = time.monotonic() + 30
                    while time.monotonic() < deadline:
                        if any('/volumesnapshots/' in key for key in fixture.data): break
                        time.sleep(.1)
                    else: self.fail('worker did not reach capture')
                    if failure == 'kill':
                        worker.kill()
                    else:
                        worker.send_signal(signal.SIGSTOP)
                    # Expire the fixture lease to exercise watchdog without a long sleep.
                    with fixture.mutex:
                        fixture.data[fixture.ns + '/configmaps/writer-capture-lock']['data']['expires'] = str(time.time() - 1)
                out, err = worker.communicate(timeout=65)
                if failure in ('kill', 'pause'):
                    deadline = time.monotonic() + 15
                    while time.monotonic() < deadline and not fixture.running(): time.sleep(.2)
                self.assertTrue(fixture.running(), 'application not resumed')
                lock = fixture.data.get(fixture.ns + '/configmaps/writer-capture-lock', {}).get('data', {})
                if failure in ('success', 'multi-success'):
                    self.assertEqual(worker.returncode, 0, err.decode())
                    self.assertEqual(lock['phase'], 'complete')
                    self.assertEqual(len(fixture.uploads), len(fixture.pvcs))
                    self.assertEqual(len(json.loads(lock['bundle'])), len(fixture.pvcs))
                    db = sqlite3.connect(str(Path(directory) / 'captured/state.sqlite'))
                    self.assertEqual(db.execute('pragma integrity_check').fetchone()[0], 'ok')
                    self.assertGreater(db.execute('select count(*) from records where n=-1').fetchone()[0], 0)
                    db.close()
                else:
                    self.assertNotEqual(worker.returncode, 0)
                    self.assertNotEqual(lock.get('phase'), 'complete')
                    if failure != 'upload': self.assertEqual(fixture.uploads, [])
            finally:
                fixture.close()

    def test_success_resumes_before_upload(self): self.exercise('success')
    def test_multistore_capture_completes_all_sources(self): self.exercise('multi-success')
    def test_multistore_partial_capture_rejected(self): self.exercise('multi-csi')
    def test_preflight_failure_never_stops(self): self.exercise('preflight')
    def test_ambiguous_stop_failure_resumes(self): self.exercise('stop')
    def test_csi_failure_resumes(self): self.exercise('csi')
    def test_capture_timeout_resumes(self): self.exercise('timeout')
    def test_upload_failure_leaves_running(self): self.exercise('upload')
    def test_sigkill_independent_watchdog_resumes(self): self.exercise('kill')
    def test_paused_worker_fenced_before_resume(self): self.exercise('pause')


if __name__ == '__main__':
    unittest.main(verbosity=2)
