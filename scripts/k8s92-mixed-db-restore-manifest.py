#!/usr/bin/env python3
"""Emit, never apply, an exact-snapshot NAS or R2 Restore to a new PVC."""
import argparse
import json
import uuid

p = argparse.ArgumentParser(description=__doc__)
p.add_argument('app', choices=('grimmory', 'tubearchivist'))
p.add_argument('backend', choices=('nas-smb', 'r2'))
p.add_argument('snapshot_id')
a = p.parse_args()
if not a.snapshot_id.isalnum():
    p.error('snapshot_id must be the exact alphanumeric Kopia snapshot ID')
name = f'{a.app}-k8s92-{a.backend}-{uuid.uuid4().hex[:8]}'
print(json.dumps({
    'apiVersion': 'kopiur.home-operations.com/v1alpha1', 'kind': 'Restore',
    'metadata': {'name': name, 'namespace': 'media'},
    'spec': {
        'repository': {'kind': 'Repository', 'name': f'{a.app}-{a.backend}'},
        'source': {'identity': {'username': a.app, 'hostname': 'media',
                                'sourcePath': f'/pvc/{a.app}', 'snapshotID': a.snapshot_id}},
        'target': {'pvc': {'name': name, 'capacity': '10Gi' if a.app == 'grimmory' else '20Gi',
                           'storageClassName': 'ceph-block', 'accessModes': ['ReadWriteOnce']}},
        'policy': {'onMissingSnapshot': 'Fail', 'waitTimeout': '5m'},
        'options': {'ignoreErrors': False, 'ignorePermissionErrors': False,
                    'skipOwners': True, 'writeFilesAtomically': True, 'enableFileDeletion': False},
        'mover': {'securityContext': {'runAsNonRoot': False}, 'podSecurityContext': {'runAsUser': 0, 'runAsGroup': 0, 'fsGroup': 568}},
        'failurePolicy': {'backoffLimit': 0, 'activeDeadlineSeconds': 1800},
    }}, indent=2))
