#!/usr/bin/env python3
"""Bounded, multi-PVC offline capture. Runs in an app-scoped Kubernetes Job.

No before/afterSnapshot stop hooks: CSI points become immutable, workloads resume,
then Kopiur uploads immutable clones. A separate Job repairs abandoned scale-down.
"""
import argparse
import json
import os
from pathlib import Path
import signal
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid


class API:
    def __init__(self):
        self.base = os.environ.get('CAPTURE_API', 'https://kubernetes.default.svc')
        self.token_path = Path('/var/run/secrets/kubernetes.io/serviceaccount/token')
        self.context = None
        if self.base.startswith('https:'):
            self.context = ssl.create_default_context(cafile=str(self.token_path.parent / 'ca.crt'))

    def call(self, method, path, body=None):
        headers = {'Content-Type': 'application/json'}
        if self.token_path.exists():
            headers['Authorization'] = 'Bearer ' + self.token_path.read_text().strip()
        req = urllib.request.Request(self.base + path,
            data=None if body is None else json.dumps(body).encode(), headers=headers, method=method)
        with urllib.request.urlopen(req, timeout=5, context=self.context) as response:
            return json.load(response)


class Capture:
    def __init__(self, api, config):
        self.api, self.cfg = api, config
        self.ns = config['namespace']
        self.core = '/api/v1/namespaces/' + self.ns
        self.apps = '/apis/apps/v1/namespaces/' + self.ns
        self.cs = '/apis/snapshot.storage.k8s.io/v1/namespaces/' + self.ns
        self.ks = '/apis/kopiur.home-operations.com/v1alpha1/namespaces/' + self.ns
        self.lock = self.core + '/configmaps/' + config['app'] + '-capture-lock'
        self.run = None

    def read(self):
        return self.api.call('GET', self.lock)

    def archive(self, state):
        if state['phase'] != 'complete' or not json.loads(state['bundle']):
            raise RuntimeError('only complete recovery receipts may be archived')
        name = self.cfg['app'] + '-capture-receipt-' + state['run']
        receipt = {'apiVersion': 'v1', 'kind': 'ConfigMap',
                   'metadata': {'name': name}, 'immutable': True, 'data': state}
        try:
            self.api.call('POST', self.core + '/configmaps', receipt)
        except urllib.error.HTTPError as error:
            if error.code != 409:
                raise
        saved = self.api.call('GET', self.core + '/configmaps/' + name)
        if saved.get('immutable') is not True or saved.get('data') != state:
            raise RuntimeError('archived receipt conflict')

    def acquire(self, lock):
        try:
            self.api.call('POST', self.core + '/configmaps', lock)
            return
        except urllib.error.HTTPError as error:
            if error.code != 409:
                raise
        previous = self.read()
        state = previous['data']
        if state.get('phase') != 'complete':
            raise RuntimeError('unfinished capture requires operator recovery')
        jobs = [state['workerJob'], self.cfg['app'] + '-resume-' + state['run']]
        for name in jobs:
            try:
                job = self.api.call('GET', '/apis/batch/v1/namespaces/' + self.ns + '/jobs/' + name)
            except urllib.error.HTTPError as error:
                if error.code != 404:
                    raise
            else:
                if not any(c.get('type') in ('Complete', 'Failed') and c.get('status') == 'True'
                           for c in job.get('status', {}).get('conditions', [])):
                    raise RuntimeError('previous capture process is not terminal')
            selector = urllib.parse.quote('batch.kubernetes.io/job-name=' + name)
            pods = self.api.call('GET', self.core + '/pods?labelSelector=' + selector)['items']
            if any(p.get('status', {}).get('phase') not in ('Succeeded', 'Failed') for p in pods):
                raise RuntimeError('previous capture has active or terminating pods')
        self.archive(state)
        # Kubernetes resourceVersion fences concurrent successors. Never delete
        # the lock or turn a timeout into permission to steal an unfinished run.
        previous['data'] = lock['data']
        self.api.call('PUT', self.lock, previous)

    def update(self, **values):
        obj = self.read()
        if obj['data']['run'] != self.run:
            raise RuntimeError('capture ownership changed')
        obj['data'].update({k: str(v) for k, v in values.items()})
        return self.api.call('PUT', self.lock, obj)

    def guard(self):
        state = self.read()['data']
        if state['run'] != self.run or state['phase'] != 'capturing':
            raise RuntimeError('capture fenced by recovery')
        if time.time() + 15 >= float(state['expires']):
            raise TimeoutError('quiescence deadline reached')
        for item in json.loads(state['workloads']):
            current = self.api.call('GET', item['path'])
            if current['metadata']['uid'] != item['uid'] or current['spec']['replicas'] != 0:
                raise RuntimeError('workload changed during quiescence')

    def resume(self, state):
        # Resume services before their auxiliary writers. A failure on one target
        # must not prevent attempts to restore every other saved workload.
        failed = []
        for item in reversed(json.loads(state['workloads'])):
            try:
                for attempt in range(6):
                    try:
                        current = self.api.call('GET', item['path'])
                        if current['metadata']['uid'] != item['uid']:
                            raise RuntimeError('refusing to scale replacement workload')
                        if current['spec']['replicas'] == item['replicas']:
                            break
                        if current['spec']['replicas'] != 0:
                            raise RuntimeError('concurrent scale decision requires operator')
                        current['spec']['replicas'] = item['replicas']
                        self.api.call('PUT', item['path'], current)
                        verified = self.api.call('GET', item['path'])
                        if (verified['metadata']['uid'] == item['uid']
                                and verified['spec']['replicas'] == item['replicas']):
                            break
                    except (urllib.error.URLError, TimeoutError):
                        # A lost PUT response may still have changed the scale.
                        # Read again, preserving its UID/resourceVersion guard.
                        if attempt == 5:
                            raise
                    time.sleep(1)
                else:
                    raise RuntimeError('resume did not converge')
            except (urllib.error.URLError, TimeoutError, RuntimeError):
                failed.append(item['path'])
        if failed:
            raise RuntimeError('resume incomplete; operator required for: ' + ', '.join(failed))

    def watchdog(self, run):
        self.run = run
        self.update(watchdogReady='true')
        while True:
            obj = self.read()
            state = obj['data']
            if state['run'] != run or state['phase'] in ('resumed', 'complete'):
                return
            if time.time() >= float(state['expires']):
                # Fence acceptance before resuming. A recovered partial capture
                # can never publish the complete bundle marker.
                obj['data']['phase'] = 'recovering'
                try:
                    self.api.call('PUT', self.lock, obj)
                    # Stop the capture Job before repair. A paused worker must not
                    # wake after repair and submit a delayed scale-down request.
                    job_path = '/apis/batch/v1/namespaces/' + self.ns + '/jobs/' + state['workerJob']
                    job = self.api.call('GET', job_path)
                    job['spec']['suspend'] = True
                    self.api.call('PUT', job_path, job)
                    selector = urllib.parse.quote('batch.kubernetes.io/job-name=' + state['workerJob'])
                    self.wait(lambda: not self.api.call('GET', self.core + '/pods?labelSelector=' + selector)['items'], seconds=60)
                    self.resume(state)
                    self.update(phase='recovered')
                    return
                except (urllib.error.URLError, TimeoutError):
                    time.sleep(2)
                    continue
            time.sleep(1)

    def wait(self, predicate, seconds=600, quiesced=False):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if quiesced:
                self.guard()
            result = predicate()
            if result:
                return result
            time.sleep(1)
        raise TimeoutError('bounded wait expired')

    def start_watchdog(self):
        name = self.cfg['app'] + '-resume-' + self.run
        pod = {'serviceAccountName': self.cfg['app'] + '-capture', 'restartPolicy': 'OnFailure',
               'securityContext': {'runAsNonRoot': True, 'runAsUser': 65534, 'runAsGroup': 65534,
                                   'seccompProfile': {'type': 'RuntimeDefault'}},
               'containers': [{'name': 'resume', 'image': self.cfg['image'],
                   'command': ['python3', '/capture/capture.py', 'watchdog', '--run', self.run],
                   'securityContext': {'allowPrivilegeEscalation': False,
                       'readOnlyRootFilesystem': True, 'capabilities': {'drop': ['ALL']}},
                   'resources': {'requests': {'cpu': '10m', 'memory': '32Mi'}, 'limits': {'memory': '128Mi'}},
                   'volumeMounts': [{'name': 'code', 'mountPath': '/capture', 'readOnly': True}]}],
               'volumes': [{'name': 'code', 'configMap': {'name': self.cfg['app'] + '-capture'}}]}
        job = {'apiVersion': 'batch/v1', 'kind': 'Job', 'metadata': {'name': name},
               'spec': {'activeDeadlineSeconds': 900, 'backoffLimit': 8, 'ttlSecondsAfterFinished': 86400,
                        'template': {'spec': pod}}}
        self.api.call('POST', '/apis/batch/v1/namespaces/' + self.ns + '/jobs', job)
        self.wait(lambda: self.read()['data'].get('watchdogReady') == 'true', seconds=120)

    def preflight(self):
        # Flux drift correction or a concurrent reconciliation can restart writers.
        # Parent owns the maintenance window and suspension, never this program.
        hr = self.api.call('GET', '/apis/helm.toolkit.fluxcd.io/v2/namespaces/' + self.ns
                           + '/helmreleases/' + self.cfg['app'])
        if hr['spec'].get('suspend') is not True:
            raise RuntimeError('parent must suspend this HelmRelease before capture')
        workloads = []
        for resource in self.cfg['workloads']:
            path = self.apps + '/' + resource + '/scale'
            obj = self.api.call('GET', path)
            replicas = obj['spec']['replicas']
            expected = self.cfg.get('expectedReplicas', {}).get(resource)
            if expected is not None and replicas != expected:
                raise RuntimeError('replica inventory changed; review recovery scope')
            workload = self.api.call('GET', path.removesuffix('/scale'))
            template = workload.get('spec', {}).get('template', {}).get('spec', {})
            claims = {v['persistentVolumeClaim']['claimName'] for v in template.get('volumes', [])
                      if 'persistentVolumeClaim' in v}
            name = resource.split('/')[1]
            for claim in workload.get('spec', {}).get('volumeClaimTemplates', []):
                claims.update(f"{claim['metadata']['name']}-{name}-{i}" for i in range(replicas))
            if claims - set(self.cfg['pvcs']):
                raise RuntimeError('workload has unprotected PVCs; review recovery scope')
            if replicas < 1:
                raise RuntimeError('refusing an already stopped workload')
            workloads.append({'path': path, 'uid': obj['metadata']['uid'], 'replicas': replicas,
                              'images': [c['image'] for c in template.get('containers', [])]})
        for pvc in self.cfg['pvcs']:
            claim = self.api.call('GET', self.core + '/persistentvolumeclaims/' + pvc)
            if claim.get('status', {}).get('phase') != 'Bound':
                raise RuntimeError('source claim is not Bound')
        return workloads

    def capture(self):
        workloads = self.preflight()
        worker_job = os.environ['CAPTURE_JOB_NAME']
        if not worker_job:
            raise RuntimeError('capture must run in an identifiable Job')
        self.run = uuid.uuid4().hex
        lock = {'apiVersion': 'v1', 'kind': 'ConfigMap',
                'metadata': {'name': self.cfg['app'] + '-capture-lock'},
                'data': {'run': self.run, 'phase': 'arming', 'workerJob': worker_job,
                         'expires': str(time.time() + 300), 'workloads': json.dumps(workloads)}}
        self.acquire(lock)
        points = []
        attempted = False
        try:
            self.start_watchdog()
            self.update(phase='capturing', expires=time.time() + self.cfg.get('quiesceSeconds', 180))
            attempted = True
            for item in workloads:
                state = self.read()['data']
                if state['phase'] != 'capturing' or time.time() + 15 >= float(state['expires']):
                    raise TimeoutError('stop deadline expired')
                obj = self.api.call('GET', item['path'])
                if obj['metadata']['uid'] != item['uid'] or obj['spec']['replicas'] != item['replicas']:
                    raise RuntimeError('concurrent workload change')
                obj['spec']['replicas'] = 0
                self.api.call('PUT', item['path'], obj)
            sources = set(self.cfg['pvcs'])
            def stopped():
                pods = self.api.call('GET', self.core + '/pods')['items']
                return not any(v.get('persistentVolumeClaim', {}).get('claimName') in sources
                               for p in pods for v in p['spec'].get('volumes', []))
            self.wait(stopped, quiesced=True)
            # All writers, including OAuth refresh and Ollama, are now stopped.
            for source in self.cfg['pvcs']:
                self.guard()
                claim = self.api.call('GET', self.core + '/persistentvolumeclaims/' + source)
                lineage = {'uid': claim['metadata']['uid'],
                           'resourceVersion': claim['metadata']['resourceVersion'],
                           'volumeName': claim['spec']['volumeName']}
                name = self.cfg['app'] + '-' + self.run + '-' + str(len(points))
                obj = {'apiVersion': 'snapshot.storage.k8s.io/v1', 'kind': 'VolumeSnapshot',
                       'metadata': {'name': name, 'labels': {'recovery.home.arpa/run': self.run}},
                       'spec': {'volumeSnapshotClassName': 'csi-ceph-blockpool',
                                'source': {'persistentVolumeClaimName': source}}}
                self.api.call('POST', self.cs + '/volumesnapshots', obj)
                points.append({'source': source, 'snapshot': name, 'sourcePVC': lineage})
            for point in points:
                def ready():
                    obj = self.api.call('GET', self.cs + '/volumesnapshots/' + point['snapshot'])
                    status = obj.get('status', {})
                    if obj['spec']['source']['persistentVolumeClaimName'] != point['source']:
                        raise RuntimeError('CSI source lineage changed')
                    if status.get('error'):
                        raise RuntimeError('CSI capture failed')
                    if status.get('readyToUse') and status.get('boundVolumeSnapshotContentName'):
                        return status | {'snapshotUID': obj['metadata']['uid']}
                    return None
                point['status'] = self.wait(ready, quiesced=True)
            self.guard()
        finally:
            if attempted:
                self.resume(self.read()['data'])
            state = self.read()['data']
            if state['phase'] in ('arming', 'capturing'):
                self.update(phase='resumed')
        if self.read()['data']['phase'] != 'resumed':
            raise RuntimeError('watchdog intervened; reject partial bundle')
        # Long clone provisioning and uploads happen ONLY after the workload is Ready.
        # Restart latency is outside the frozen interval, but it still gates upload.
        def resumed_ready():
            for item in workloads:
                obj = self.api.call('GET', item['path'].removesuffix('/scale'))
                if obj['metadata']['uid'] != item['uid']:
                    raise RuntimeError('workload replaced after capture')
                if obj.get('status', {}).get('readyReplicas', 0) < item['replicas']:
                    return False
            return True
        self.wait(resumed_ready, seconds=1200)
        result = self.upload(points)
        self.update(phase='complete', bundle=json.dumps(result))
        self.archive(self.read()['data'])
        print(json.dumps({'run': self.run, 'phase': 'complete', 'volumes': len(result)}))

    def upload(self, points):
        results = []
        for point in points:
            source = point['source']
            name = point['snapshot']
            claim = self.api.call('GET', self.core + '/persistentvolumeclaims/' + source)
            if (claim['metadata']['uid'] != point['sourcePVC']['uid']
                    or claim['spec']['volumeName'] != point['sourcePVC']['volumeName']):
                raise RuntimeError('source PVC replaced after CSI capture')
            pvc = {'apiVersion': 'v1', 'kind': 'PersistentVolumeClaim', 'metadata': {'name': name},
                   'spec': {'storageClassName': 'ceph-block', 'accessModes': ['ReadWriteOnce'],
                            'resources': {'requests': {'storage': claim['spec']['resources']['requests']['storage']}},
                            'dataSource': {'apiGroup': 'snapshot.storage.k8s.io', 'kind': 'VolumeSnapshot', 'name': name}}}
            self.api.call('POST', self.core + '/persistentvolumeclaims', pvc)
            policy = {'apiVersion': 'kopiur.home-operations.com/v1alpha1', 'kind': 'SnapshotPolicy',
                'metadata': {'name': name}, 'spec': {
                    'repository': {'kind': 'Repository', 'name': self.cfg['app'] + '-nas-smb'},
                    'identity': {'username': self.cfg['app'], 'hostname': self.ns},
                    'sources': [{'pvc': {'name': name}, 'readOnly': True, 'sourcePathOverride': '/pvc/' + source}],
                    'copyMethod': 'Direct', 'defaultDeletionPolicy': 'Retain',
                    'retention': {'keepDaily': 7, 'keepWeekly': 4},
                    'mover': {'privilegedMode': True}}}
            self.api.call('POST', self.ks + '/snapshotpolicies', policy)
            snapshot = {'apiVersion': 'kopiur.home-operations.com/v1alpha1', 'kind': 'Snapshot',
                        'metadata': {'name': name}, 'spec': {'policyRef': {'name': name},
                            'deletionPolicy': 'Retain', 'tags': {'recovery-run': self.run},
                            'failurePolicy': {'activeDeadlineSeconds': 1800, 'backoffLimit': 0,
                                              'podStartupDeadlineSeconds': 300}}}
            self.api.call('POST', self.ks + '/snapshots', snapshot)
            results.append(point | {'kopiurSnapshot': name})
        # Capture completion is distinct from a repository backup. Inspect all CRs
        # before retaining a bundle receipt; no successful partial bundles.
        for result in results:
            def completed():
                obj = self.api.call('GET', self.ks + '/snapshots/' + result['kopiurSnapshot'])
                status = obj.get('status', {})
                phase = status.get('phase')
                if phase in ('Failed', 'Aborted'):
                    raise RuntimeError('Kopiur upload failed; retained CSI points remain usable')
                if phase in ('Succeeded', 'Unchanged') and status.get('snapshot', {}).get('kopiaSnapshotID'):
                    return status['snapshot']
                return None
            result['backup'] = self.wait(completed, seconds=2100)
        return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=['capture', 'watchdog'])
    parser.add_argument('--config', default='/capture/config.json')
    parser.add_argument('--run')
    args = parser.parse_args()
    def interrupted(signum, frame):
        raise InterruptedError('capture interrupted')
    signal.signal(signal.SIGTERM, interrupted)
    config = json.loads(Path(args.config).read_text())
    worker = Capture(API(), config)
    if args.mode == 'watchdog':
        worker.watchdog(args.run)
    else:
        worker.capture()


if __name__ == '__main__':
    main()
