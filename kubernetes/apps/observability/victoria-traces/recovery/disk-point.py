#!/usr/bin/env python3
"""Render a whole-volume VT CSI capture, never detach live partitions.

Requires the API objects for the PVC and an existing Retain snapshot class.
This is a crash-consistent disk point, NOT proven native trace recovery.
No /snapshot/create API exists in the deployed VictoriaTraces 0.12 contract.
"""
import argparse
import json
from pathlib import Path
import uuid

PVC = 'server-volume-victoria-traces-0'


def render(pvc, snapshot_class):
    if (pvc.get('kind') != 'PersistentVolumeClaim'
            or pvc['metadata'].get('namespace') != 'observability'
            or pvc['metadata']['name'] != PVC
            or pvc.get('status', {}).get('phase') != 'Bound'):
        raise ValueError('requires the bound VictoriaTraces PVC')
    if (snapshot_class.get('kind') != 'VolumeSnapshotClass'
            or snapshot_class.get('deletionPolicy') != 'Retain'
            or snapshot_class.get('driver') != 'rook-ceph.rbd.csi.ceph.com'):
        raise ValueError('requires an existing retained RBD snapshot class')
    return {'apiVersion': 'snapshot.storage.k8s.io/v1', 'kind': 'VolumeSnapshot',
            'metadata': {'name': 'vt-archive-' + uuid.uuid4().hex,
                         'namespace': 'observability',
                         'annotations': {'kustomize.toolkit.fluxcd.io/prune': 'disabled',
                                         'kopiur.home.arpa/source-pvc-uid': pvc['metadata']['uid'],
                                         'kopiur.home.arpa/recovery-state': 'unqualified'}},
            'spec': {'volumeSnapshotClassName': snapshot_class['metadata']['name'],
                     'source': {'persistentVolumeClaimName': PVC}}}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pvc-json', required=True)
    parser.add_argument('--class-json', required=True)
    args = parser.parse_args()
    print(json.dumps(render(json.loads(Path(args.pvc_json).read_text()),
                            json.loads(Path(args.class_json).read_text())), indent=2))
