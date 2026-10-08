"""Authenticated capture adapters for an explicitly prepared, fenced generation.

SnapshotCapture reads a completed snapshot through an authenticated request and
a caller-owned archive read. It never creates a repository, changes writer
admission or grants permission to export original data. All I/O remains inside
the caller approved boundary and requires its live independent guard.
"""
import copy
import json
import re

from kopiur_elasticsearch_escrow import EscrowError, _binding, _encoded
from kopiur_nonrel_native import validate_archive


def snapshot_metadata(binding):
    return {key: binding[key] for key in ('generation', 'source_uid', 'source_pod_uid')}


def synthetic_snapshot_archive():
    """Build a minimal structurally valid native snapshot archive.

    Real engine acceptance runs separately on ARC against a captured archive.
    """
    import io
    import tarfile
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode='w') as target:
        for name, data in (('index-N', b'0' * 4), ('snap-1.dat', b'snapshot-data-here')):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mtime = 0
            target.addfile(info, io.BytesIO(data))
    payload = archive.getvalue()
    validate_archive(payload)
    return payload


def credentials_from_bytes(data):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('duplicate credential key')
            result[key] = value
        return result
    try:
        if not isinstance(data, bytes) or not data:
            raise ValueError('credential bytes required')
        text = data.decode()
        parsed = json.JSONDecoder(object_pairs_hook=unique).raw_decode(text)
        if not isinstance(parsed, tuple):
            raise ValueError('single credential document required')
        value, offset = parsed
        if text[offset:]:  # Exact bytes: no whitespace, suffix or second document.
            raise ValueError('trailing credential bytes')
        if (not isinstance(value, dict) or set(value) != {'elastic_username', 'elastic_password'}
                or value['elastic_username'] != 'elastic'
                or not isinstance(value['elastic_password'], str) or not value['elastic_password']
                or any(ord(c) < 32 or ord(c) == 127 for c in value['elastic_password'])):
            raise ValueError('invalid credentials')
        return value
    except (ValueError, TypeError, UnicodeError):
        raise EscrowError('exact-service credentials invalid') from None


class SnapshotCapture:
    """Authenticate and bind snapshot metadata around the native archive read.

    guard must check export authority, all-writer fencing and source lifetime.
    request is an authenticated GET inside that same boundary. Credentials never
    become command arguments, files, logs or a receipt. The caller explicitly
    selects repository, snapshot, filesystem location and full application index
    inventory. Security feature indices must be present in the same snapshot.
    """
    def __init__(self, binding, *, guard, read_credentials, request, read_archive,
                 repository, snapshot, location, indices, expected_uuid=None):
        self.binding = _binding(binding)
        if (any(not isinstance(v, str) or not re.fullmatch(r'[a-z0-9][a-z0-9_-]*', v)
                for v in (repository, snapshot))
                or not isinstance(location, str)
                or not location.startswith('/usr/share/elasticsearch/data/snapshot')
                or location != '/usr/share/elasticsearch/data/snapshot'
                and not location.startswith('/usr/share/elasticsearch/data/snapshot/')
                or any(x in ('', '.', '..') for x in location.split('/')[1:])
                or not isinstance(indices, list) or not indices
                or any(not isinstance(i, str) or not re.fullmatch(r'[a-z0-9][a-z0-9_.-]*', i) for i in indices)
                or len(indices) != len(set(indices))):
            raise EscrowError('explicit native snapshot inventory and location required')
        if expected_uuid is not None and (not isinstance(expected_uuid, str)
                or not re.fullmatch(r'[A-Za-z0-9_-]+', expected_uuid)):
            raise EscrowError('explicit snapshot UUID required')
        self.expected_uuid = expected_uuid
        self.guard, self.read_credentials = guard, read_credentials
        self.request, self.read_archive = request, read_archive
        self.repository, self.snapshot, self.location = repository, snapshot, location
        self.indices = set(indices)

    def check(self, binding):
        if _binding(binding) != self.binding:
            raise EscrowError('authenticated capture binding differs')
        # A guard either raises or returns exactly True, never a truthy receipt.
        if self.guard() is not True:
            raise EscrowError('affirmative live capture fence required')

    def call(self, binding, fn, *args):
        self.check(binding)
        try:
            result = fn(*args)
        except EscrowError:
            raise
        except Exception:
            # Suppress adapter exception details and their secret-bearing chain.
            raise EscrowError('authenticated source operation failed') from None
        self.check(binding)
        return result

    def validate_archive_bytes(self, data):
        try:
            validate_archive(data)
            return True
        except Exception:
            raise EscrowError('native repository archive invalid') from None

    def authenticated_credentials(self, binding):
        value = credentials_from_bytes(self.call(binding, self.read_credentials))
        identity = self.call(binding, self.request, '/_security/_authenticate', value)
        if (not isinstance(identity, dict) or identity.get('username') != 'elastic'
                or identity.get('roles') != ['superuser']):
            raise EscrowError('source credential authentication not proved')
        return value

    def credentials(self, binding):
        value = self.authenticated_credentials(binding)
        return self.part(binding, _encoded(value))

    @staticmethod
    def part(binding, data):
        return {'binding': copy.deepcopy(binding), 'data': data,
                'mode': 0o600, 'uid': 1000, 'gid': 1000}

    def native(self, binding):
        value = self.authenticated_credentials(binding)
        root = self.call(binding, self.request, '/', value)
        if not isinstance(root, dict) or root.get('version', {}).get('number') != binding['runtime_version']:
            raise EscrowError('authenticated native runtime version differs')
        path = '/_snapshot/' + self.repository
        repository = self.call(binding, self.request, path, value)
        if (not isinstance(repository, dict) or set(repository) != {self.repository}
                or repository[self.repository].get('type') != 'fs'
                or repository[self.repository].get('settings', {}).get('location') != self.location):
            raise EscrowError('native repository location differs')
        snapshot_path = path + '/' + self.snapshot
        before = self.call(binding, self.request, snapshot_path, value)
        self.validate_snapshot(before)
        archive = self.call(binding, self.read_archive, self.location)
        self.validate_archive_bytes(archive)
        after = self.call(binding, self.request, snapshot_path, value)
        if after != before:
            raise EscrowError('native snapshot changed during capture')
        repository_after = self.call(binding, self.request, path, value)
        if (not isinstance(repository_after, dict) or set(repository_after) != {self.repository}
                or repository_after[self.repository].get('type') != 'fs'
                or repository_after[self.repository].get('settings', {}).get('location') != self.location):
            raise EscrowError('native repository location changed during capture')
        return self.part(binding, archive)

    def validate_snapshot(self, result):
        try:
            snapshots = result['snapshots']
            if not isinstance(snapshots, list) or len(snapshots) != 1:
                raise ValueError('ambiguous snapshot')
            state = snapshots[0]
            shards = state['shards']
            features = state['feature_states']
            if (not isinstance(features, list) or len(features) != 1
                    or features[0]['feature_name'] != 'security'
                    or not isinstance(features[0]['indices'], list) or not features[0]['indices']
                    or any(not isinstance(i, str) or not i.startswith('.security-')
                           for i in features[0]['indices'])):
                raise ValueError('security feature state absent')
            security = set(features[0]['indices'])
            checks = {
                'snapshot_name': state['snapshot'] == self.snapshot,
                'snapshot_uuid': isinstance(state['uuid'], str) and bool(state['uuid'])
                    and (self.expected_uuid is None or state['uuid'] == self.expected_uuid),
                'successful_snapshot': state['state'] == 'SUCCESS' and not state.get('failures'),
                'global_state': state['include_global_state'] is True,
                'runtime_version': state['version'] == self.binding['runtime_version'],
                'generation_metadata': state['metadata'] == snapshot_metadata(self.binding),
                'index_inventory': isinstance(state['indices'], list)
                    and len(state['indices']) == len(set(state['indices']))
                    and set(state['indices']) == self.indices | security,
                'shard_inventory': type(shards['total']) is int and shards['total'] > 0
                    and type(shards['successful']) is int and shards['successful'] == shards['total']
                    and type(shards['failed']) is int and shards['failed'] == 0,
            }
            failed = sorted(key for key, passed in checks.items() if not passed)
            if failed:
                # Only fixed validation labels may escape, never API values.
                raise EscrowError('coherent complete native/security snapshot required: ' + ','.join(failed))
        except (ValueError, KeyError, TypeError):
            raise EscrowError('coherent complete native/security snapshot required') from None
