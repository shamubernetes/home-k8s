#!/usr/bin/env python3
"""Render isolated, exact-ID infrastructure Restore CRs. Does not call Kubernetes.

Input is the immutable generation receipt ConfigMap JSON. An R2 drill also needs
an independently verified destination mapping bound to that run and NAS point.
"""
import argparse
import json
import os
from pathlib import Path
import re

from k8s92_generate_infrastructure import APPS


def render(app, receipt, namespace, tier, r2_ids=None):
    if tier not in ('nas-smb', 'r2'):
        raise ValueError('unsupported restore tier')
    if len(namespace) > 63 or not re.fullmatch(r'[a-z0-9][a-z0-9-]*-recovery-[a-z0-9][a-z0-9-]*[a-z0-9]', namespace):
        raise ValueError('an explicit disposable *-recovery-* namespace is required')
    cfg = APPS[app]
    state = receipt['data']
    if not re.fullmatch('[0-9a-f]{32}', state['run']):
        raise ValueError('receipt generation ID is malformed')
    if (receipt.get('immutable') is not True or receipt['metadata'].get('namespace') != cfg['namespace']
            or receipt['metadata']['name'] != app + '-capture-receipt-' + state['run']
            or state['phase'] != 'complete'):
        raise ValueError('receipt must be a completed capture for this application')
    points = json.loads(state['bundle'])
    if sorted(p['source'] for p in points) != sorted(cfg['pvcs']):
        raise ValueError('receipt is incomplete or contains duplicate PVCs')
    if tier == 'r2' and (r2_ids is None or r2_ids.get('run') != state['run']
            or r2_ids.get('repository') != app + '-r2'
            or r2_ids.get('namespace') != cfg['namespace']
            or set(r2_ids.get('points', {})) != set(cfg['pvcs'])):
        raise ValueError('R2 requires verified destination mapping for this generation')
    resources = [{'apiVersion': 'networking.k8s.io/v1', 'kind': 'NetworkPolicy',
        'metadata': {'name': app + '-deny-egress', 'namespace': namespace},
        'spec': {'podSelector': {}, 'policyTypes': ['Ingress', 'Egress'], 'ingress': [], 'egress': []}},
        # Kopiur 0.10.10 stamps these labels on restore mover pods. Only the
        # transport may reach DNS, the API and its repository. Restored apps
        # must never inherit these controller-owned labels.
        {'apiVersion': 'networking.k8s.io/v1', 'kind': 'NetworkPolicy',
         'metadata': {'name': app + '-restore-transport', 'namespace': namespace},
         'spec': {'podSelector': {'matchLabels': {
             'app.kubernetes.io/managed-by': 'kopiur',
             'kopiur.home-operations.com/op': 'restore'}},
             'policyTypes': ['Egress'], 'egress': [{'ports': [
                 {'protocol': 'UDP', 'port': 53}, {'protocol': 'TCP', 'port': 53},
                 {'protocol': 'TCP', 'port': 443}, {'protocol': 'TCP', 'port': 6443},
                 *([{'protocol': 'TCP', 'port': 445}] if tier == 'nas-smb' else [])]}]}}]
    for i, point in enumerate(points):
        backup = point['backup']
        if (not all(point.get('sourcePVC', {}).get(k) for k in ('uid', 'resourceVersion', 'volumeName'))
                or not all(point.get('status', {}).get(k) for k in ('snapshotUID', 'boundVolumeSnapshotContentName', 'restoreSize'))
                or point['snapshot'] != app + '-' + state['run'] + '-' + str(i)
                or point.get('kopiurSnapshot') != point['snapshot']):
            raise ValueError('receipt source/CSI lineage is incomplete')
        snapshot_id = backup['kopiaSnapshotID']
        if tier == 'r2':
            assert r2_ids is not None
            destination = r2_ids['points'][point['source']]
            if destination['sourceNASID'] != snapshot_id:
                raise ValueError('R2 mapping belongs to a different NAS point')
            snapshot_id = destination['snapshotID']
        if not isinstance(snapshot_id, str) or not re.fullmatch('[0-9a-f]{32}', snapshot_id):
            raise ValueError('exact Kopia snapshot ID is malformed')
        if (backup['identity']['username'] != app
                or backup['identity']['hostname'] != cfg['namespace']
                or backup['identity']['sourcePath'] != '/pvc/' + point['source']):
            raise ValueError('receipt identity mismatch')
        name = f"{app}-restore-{state['run']}-{i}"
        resources.append({'apiVersion': 'kopiur.home-operations.com/v1alpha1', 'kind': 'Restore',
            'metadata': {'name': name, 'namespace': namespace,
                         'labels': {'recovery.home.arpa/run': state['run']},
                         'annotations': {'recovery.home.arpa/source-pvc': point['source'],
                             'recovery.home.arpa/source-pvc-uid': point['sourcePVC']['uid'],
                             'recovery.home.arpa/source-pv': point['sourcePVC']['volumeName'],
                             'recovery.home.arpa/csi-snapshot-uid': point['status']['snapshotUID']}},
            'spec': {'repository': {'kind': 'Repository', 'name': app + '-' + tier,
                                    'namespace': cfg['namespace']},
                     'credentialProjection': {'enabled': True},
                     'source': {'identity': {'username': app, 'hostname': cfg['namespace'],
                          'sourcePath': '/pvc/' + point['source'], 'snapshotID': snapshot_id}},
                     'target': {'pvc': {'name': name, 'storageClassName': 'ceph-block',
                          'capacity': point['status']['restoreSize'], 'accessModes': ['ReadWriteOnce']}},
                     'mover': {'privilegedMode': True},
                     'policy': {'onMissingSnapshot': 'Fail', 'waitTimeout': '5m'},
                     'options': {'ignoreErrors': False, 'ignorePermissionErrors': False,
                         'skipOwners': False, 'skipPermissions': False, 'skipExisting': False,
                         'writeFilesAtomically': True, 'enableFileDeletion': False},
                     'failurePolicy': {'activeDeadlineSeconds': 1800, 'backoffLimit': 0,
                                       'podStartupDeadlineSeconds': 300}}})
    return {'apiVersion': 'v1', 'kind': 'List', 'items': resources}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('app', choices=sorted(APPS))
    parser.add_argument('--receipt', required=True, type=Path)
    parser.add_argument('--namespace', required=True)
    parser.add_argument('--tier', choices=['nas-smb', 'r2'], required=True)
    parser.add_argument('--r2-ids', type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    result = render(args.app, json.loads(args.receipt.read_text()), args.namespace, args.tier,
                    json.loads(args.r2_ids.read_text()) if args.r2_ids else None)
    # Never overwrite existing artifacts, including symlinks.
    with os.fdopen(os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'w') as f:
        json.dump(result, f, indent=2)
    print(json.dumps({'restoreCount': sum(o['kind'] == 'Restore' for o in result['items']), 'applied': False}))


if __name__ == '__main__':
    main()
