#!/usr/bin/env python3
"""Render rollback-only backup resources from an already qualified Repository.

Input is the nonsecret JSON returned by kubectl get repository NAME -n media.
This does not apply anything and cannot resume the retired Tunarr application.
"""
import json
import sys


def render(repository):
    metadata = repository['metadata']
    if metadata.get('namespace') != 'media' or repository.get('kind') != 'Repository':
        raise ValueError('requires a media Repository')
    conditions = repository.get('status', {}).get('conditions', [])
    if not any(c.get('type') == 'Ready' and c.get('status') == 'True'
               and c.get('observedGeneration') == metadata.get('generation')
               for c in conditions):
        raise ValueError('Repository must be Ready at its current generation')
    backend = repository['spec']['backend']
    if not any(k in backend for k in ('rclone', 's3')):
        raise ValueError('requires a durable NAS or offsite backend')
    policy = {
        'apiVersion': 'kopiur.home-operations.com/v1alpha1',
        'kind': 'SnapshotPolicy',
        'metadata': {'name': 'tunarr-rollback', 'namespace': 'media',
                     'annotations': {'kustomize.toolkit.fluxcd.io/prune': 'disabled'}},
        'spec': {'repository': {'kind': 'Repository', 'name': metadata['name']},
                 'identity': {'username': 'tunarr-rollback', 'hostname': 'media'},
                 'sources': [{'pvc': {'name': 'tunarr'}, 'readOnly': True,
                              'sourcePathOverride': '/pvc/tunarr-rollback'}],
                 'copyMethod': 'Snapshot',
                 'volumeSnapshotClassName': 'csi-ceph-blockpool',
                 'staging': {'timeout': '10m', 'storageClassName': 'ceph-block',
                             'accessModes': ['ReadWriteOnce']},
                 'defaultDeletionPolicy': 'Retain',
                 'suspend': True,
                 'mover': {'podSecurityContext': {'runAsNonRoot': True,
                            'runAsUser': 568, 'runAsGroup': 568, 'fsGroup': 568,
                            'fsGroupChangePolicy': 'OnRootMismatch'}}}}
    schedule = {
        'apiVersion': policy['apiVersion'], 'kind': 'SnapshotSchedule',
        'metadata': {'name': 'tunarr-rollback-daily', 'namespace': 'media'},
        'spec': {'policyRef': {'name': 'tunarr-rollback'},
                 'schedule': {'suspend': True, 'cron': '43 2 * * *', 'timezone': 'America/New_York',
                              'jitter': '0s', 'runOnCreate': False,
                              'concurrencyPolicy': 'Forbid',
                              'startingDeadlineSeconds': 3600}}}
    return {'apiVersion': 'v1', 'kind': 'List', 'items': [policy, schedule]}


if __name__ == '__main__':
    print(json.dumps(render(json.load(sys.stdin)), indent=2))
