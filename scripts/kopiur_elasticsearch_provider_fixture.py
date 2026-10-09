"""Synthetic native Elasticsearch recovery from real encrypted backup providers.

Run only in the allocated ARC runner. Credentials arrive on stdin. This does
not qualify production capture, recurring replication, retention or recovery policy.
"""
import configparser
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import secrets
import signal
import sys
import time
import uuid

from kopiur_nonrel_native import fixture


def load_transport():
    spec = importlib.util.spec_from_file_location(
        'identity_transport', Path(__file__).with_name('kopiur-identity-transport.py'))
    if spec is None or spec.loader is None:
        raise RuntimeError('provider transport module is unavailable')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def restore_archive(drill, kind, archive):
    password = drill.fields['NAS_KOPIA_PASSWORD' if kind == 'nas' else 'R2_KOPIA_PASSWORD']
    digest = hashlib.sha256(archive).hexdigest()
    producer = drill.start()
    drill.run(['docker', 'exec', '-i', producer, '/tools/sh', '-c',
               '/tools/mkdir -p /work/source && /tools/cat > /work/source/repository.tar'], stdin=archive)
    snapshot = json.loads(drill.exec(producer,
        'k repository create ' + drill.backend(kind) + ' >/dev/null\n'
        'k snapshot create /work/source --json\n', password=password).stdout)
    object_id = snapshot['rootEntry']['obj']
    if not re.fullmatch(r'[A-Za-z0-9]+', object_id):
        raise RuntimeError('unexpected provider object identifier')
    drill.remove(producer)
    wrong = drill.start()
    denied = drill.exec(wrong, 'k repository connect ' + drill.backend(kind) + ' >/dev/null\n',
                        password=secrets.token_hex(32), check=False)
    if not denied.returncode or not any(code in denied.stderr.lower() for code in
            (b'invalid repository password', b'unable to decrypt')):
        raise RuntimeError('provider encryption denial not proved')
    drill.remove(wrong)
    restorer = drill.start()
    drill.exec(restorer, 'k repository connect ' + drill.backend(kind) + ' >/dev/null\n'
               'k snapshot restore ' + object_id + ' /work/restored >/dev/null\n', password=password)
    restored = drill.run(['docker', 'exec', restorer, '/tools/cat',
                          '/work/restored/repository.tar']).stdout
    if hashlib.sha256(restored).hexdigest() != digest:
        raise RuntimeError('provider native archive bytes differ')
    drill.remove(restorer)
    return restored, {'backend': kind, 'snapshot_id': snapshot['id'], 'object_id': object_id,
                      'archive_sha256': digest, 'archive_bytes': len(restored),
                      'producer_removed_before_direct_restore': True,
                      'fresh_restorer': True, 'wrong_password_denied': True}


def restore_generation(drill, archive):
    """Re-encrypt NAS-restored bytes into R2, without another source capture."""
    generation = uuid.uuid4().hex
    nas_archive, nas = restore_archive(drill, 'nas', archive)
    r2_archive, r2 = restore_archive(drill, 'r2', nas_archive)
    digest = hashlib.sha256(archive).hexdigest()
    if (nas['archive_sha256'] != digest or r2['archive_sha256'] != digest
            or hashlib.sha256(r2_archive).hexdigest() != digest):
        raise RuntimeError('same-generation provider archive lineage differs')
    return r2_archive, {'generation': generation, 'nas': nas,
                        'r2': dict(r2, source_nas_snapshot_id=nas['snapshot_id']),
                        'same_generation_archive_sha256': digest,
                        'r2_input_is_fresh_nas_restore': True,
                        'synthetic_archive_lineage_qualified': True,
                        'production_replication_qualified': False}


def validate_identity(fields):
    if (fields['R2_BUCKET'] != 'kopiur-elasticsearch'
            or fields['NAS_SHARE'] != 'kopiur-elasticsearch'
            or fields['NAS_USERNAME'] != 'kp-elasticsearch'
            or fields['NAS_KOPIA_PASSWORD'] == fields['R2_KOPIA_PASSWORD']):
        raise ValueError('dedicated independent provider identities required')
    config = configparser.ConfigParser(interpolation=None)
    config.read_string(fields['NAS_RCLONE_CONFIG'])
    if config.sections() != ['mnemosyne']:
        raise ValueError('dedicated NAS remote required')
    expected = {'type': 'smb', 'host': '10.100.47.100', 'user': 'kp-elasticsearch',
                'domain': 'WORKGROUP', 'pass': config['mnemosyne'].get('pass', '')}
    if dict(config['mnemosyne']) != expected or not expected['pass']:
        raise ValueError('dedicated NAS identity configuration required')


def interrupted(signum, frame):
    raise RuntimeError('search fixture interrupted by signal ' + str(signum))


def exercise_payload(payload, deadline=None, *, escrow=False):
    if sys.platform != 'linux' or not os.environ.get('RUNNER_NAME', '').startswith('ghar-set-zoo-'):
        raise RuntimeError('requires owned ghar-set-zoo ARC runner')

    if set(payload) != {'app', 'fields'} or payload['app'] != 'elasticsearch':
        raise ValueError('only the dedicated Elasticsearch backup identity is allowed')
    fields = payload['fields']
    validate_identity(fields)
    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    deadline = min(deadline or time.monotonic() + 1800, time.monotonic() + 1800)
    work_deadline = deadline - 420
    def bounded(args, timeout=180):
        remaining = work_deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeError('search provider absolute deadline exceeded')
        return transport.run(args, timeout=min(timeout, remaining))
    transport = load_transport()
    nonce = uuid.uuid4().hex
    transport.TOOLS = 'k8s92-search-tools-' + nonce
    bounded(['docker', 'pull', transport.TOOL_IMAGE], timeout=600)
    bounded(['docker', 'pull', transport.IMAGE], timeout=600)
    bounded(['docker', 'volume', 'create', '--label', 'k8s92.search.tools=' + nonce,
                   transport.TOOLS])
    results = []
    try:
        bounded(['docker', 'run', '--rm', '--read-only', '--network', 'none',
                       '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges',
                       '--mount', 'type=volume,src=' + transport.TOOLS + ',dst=/tools,volume-nocopy',
                       transport.TOOL_IMAGE, 'sh', '-c',
                       'cp /bin/busybox /tools/busybox && /tools/busybox --install -s /tools'])
        for attempt in range(2):
            drill = transport.Drill('elasticsearch', fields)
            drill.deadline = min(time.monotonic() + 1200, work_deadline)
            try:
                proof = (fixture('elasticsearch', escrow_provider=drill, deadline=work_deadline)
                         if escrow else fixture('elasticsearch',
                         transport=lambda data: restore_generation(drill, data), deadline=work_deadline))
            finally:
                drill.cleanup()
            proof['owned_provider_fixture_removed'] = True
            results.append(proof)
    finally:
        owner = transport.run(['docker', 'volume', 'inspect', '--format',
                               '{{ index .Labels "k8s92.search.tools" }}', transport.TOOLS], timeout=30).stdout.decode().strip()
        if owner != nonce:
            raise RuntimeError('search tool volume ownership changed')
        transport.run(['docker', 'volume', 'rm', transport.TOOLS], timeout=30)
    return {'app': 'elasticsearch', 'results': results,
            'operation': 'native-elasticsearch-escrow' if escrow else 'native-elasticsearch-provider',
            'independent_escrow_provider_engine_restores': 4 if escrow else 0,
            'nas_to_r2_native_fixture_restores': 2,
            'synthetic_archive_lineage_qualified': True,
            'production_recovery_accepted': False, 'replication_lineage_qualified': False}


if __name__ == '__main__':
    print(json.dumps(exercise_payload(json.loads(sys.stdin.buffer.read(65537)))))
