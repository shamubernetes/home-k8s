#!/usr/bin/env python3
"""Prepare one VM native point, then release it only after retained CSI proof.

The integration owner applies the emitted VolumeSnapshot. The private state file
must live on durable storage. An ambiguous create result leaves it fail-closed.
This helper never stops the process or deletes a CSI/remote recovery point.
"""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time
import urllib.parse
import urllib.request
import uuid

PVC = 'server-volume-victoria-metrics-ha-0'
NAMESPACE = 'observability'


def api(endpoint, path):
    parsed = urllib.parse.urlsplit(endpoint)
    if (parsed.scheme not in ('http', 'https') or not parsed.hostname
            or parsed.username or parsed.password or parsed.query or parsed.fragment
            or parsed.path not in ('', '/')):
        raise ValueError('invalid native endpoint')
    with urllib.request.urlopen(endpoint.rstrip('/') + path, timeout=60) as response:
        result = json.load(response)
    if result.get('status') != 'ok':
        raise RuntimeError('native snapshot API failed')
    return result


def get(kind, name):
    return json.loads(subprocess.check_output(
        ['kubectl', '-n', NAMESPACE, 'get', kind, name, '-o', 'json'],
        timeout=30, stderr=subprocess.DEVNULL))


def save(path, state, exclusive=False):
    path = Path(path)
    if exclusive:
        # Reserve the transaction durably before any native mutation.
        with path.open('x') as handle:
            os.chmod(path, 0o600)
            handle.write('{}\n')
            handle.flush()
            os.fsync(handle.fileno())
    fd, temporary = tempfile.mkstemp(prefix='.native-point-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as handle:
            json.dump(state, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def prepare(endpoint, state_path, snapshot_class):
    # Never overwrite an unresolved or completed transaction. Each point owns
    # its own durable file and CSI name. Parent lifecycle must bound their age.
    pvc = get('pvc', PVC)
    klass = get('volumesnapshotclass', snapshot_class)
    if klass.get('deletionPolicy') != 'Retain':
        raise ValueError('capture requires an existing Retain snapshot class')
    state = {'generation': uuid.uuid4().hex, 'state': 'creating',
             'endpoint': endpoint.rstrip('/'),
             'pvc_uid': pvc['metadata']['uid'], 'created_at': time.time()}
    save(state_path, state, exclusive=True)
    # Do not retry this request. A lost response can have created a native point.
    name = api(endpoint, '/snapshot/create')['snapshot']
    if not re.fullmatch(r'[A-Za-z0-9_.-]+', name):
        raise ValueError('unsafe native name')
    state.update(native=name, state='prepared',
                 csi='vm-native-' + state['generation'])
    save(state_path, state)
    return {'apiVersion': 'snapshot.storage.k8s.io/v1', 'kind': 'VolumeSnapshot',
            'metadata': {'name': state['csi'], 'namespace': NAMESPACE,
                         'annotations': {'kustomize.toolkit.fluxcd.io/prune': 'disabled',
                                         'kopiur.home.arpa/native-snapshot': name,
                                         'kopiur.home.arpa/transaction': state['generation']}},
            'spec': {'volumeSnapshotClassName': snapshot_class,
                     'source': {'persistentVolumeClaimName': PVC}}}


def release(endpoint, state_path):
    state = json.loads(Path(state_path).read_text())
    if state['state'] != 'prepared' or state['endpoint'] != endpoint.rstrip('/'):
        raise ValueError('transaction requires explicit reconciliation')
    snap = get('volumesnapshot', state['csi'])
    annotations = snap['metadata'].get('annotations', {})
    if (snap['spec']['source'].get('persistentVolumeClaimName') != PVC
            or annotations.get('kopiur.home.arpa/native-snapshot') != state['native']
            or annotations.get('kopiur.home.arpa/transaction') != state['generation']
            or snap['metadata'].get('deletionTimestamp')
            or snap.get('status', {}).get('readyToUse') is not True
            or get('pvc', PVC)['metadata']['uid'] != state['pvc_uid']):
        raise ValueError('immutable capture not established')
    content = get('volumesnapshotcontent', snap['status']['boundVolumeSnapshotContentName'])
    if (content['spec']['deletionPolicy'] != 'Retain'
            or content['spec']['volumeSnapshotRef']['uid'] != snap['metadata']['uid']
            or content.get('status', {}).get('readyToUse') is not True
            or content['metadata'].get('deletionTimestamp')):
        raise ValueError('retained CSI content not established')
    # Persist proof BEFORE releasing native hardlinks from the production PVC.
    state.update(csi_uid=snap['metadata']['uid'],
                 csi_content=content['metadata']['name'], state='captured')
    save(state_path, state)
    api(endpoint, '/snapshot/delete?' + urllib.parse.urlencode({'snapshot': state['native']}))
    remaining = api(endpoint, '/snapshot/list').get('snapshots')
    if not isinstance(remaining, list) or state['native'] in remaining:
        raise RuntimeError('native release not confirmed; reconcile captured transaction')
    state['state'] = 'released'
    save(state_path, state)
    return state


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['prepare', 'release'])
    parser.add_argument('--endpoint', required=True)
    parser.add_argument('--state', required=True)
    parser.add_argument('--snapshot-class')
    args = parser.parse_args()
    if args.action == 'prepare' and not args.snapshot_class:
        parser.error('--snapshot-class required for prepare')
    result = (prepare(args.endpoint, args.state, args.snapshot_class)
              if args.action == 'prepare' else release(args.endpoint, args.state))
    print(json.dumps(result, indent=2))
