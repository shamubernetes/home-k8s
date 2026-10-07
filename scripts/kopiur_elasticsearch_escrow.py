"""Coherent Elasticsearch escrow integration, with no production I/O defaults.

Adapters own authorized capture, encryption and isolated engine operations. This
contract does not authorize source export, prove cessation or approve retention.
Plaintext is passed only to the caller's approved in-boundary encryption adapter.
"""
import copy
import hashlib
import json
import re


CONFIG_FILES = frozenset({
    'elasticsearch-plugins.example.yml', 'elasticsearch.keystore',
    'elasticsearch.yml', 'jvm.options', 'log4j2.file.properties',
    'log4j2.properties', 'role_mapping.yml', 'roles.yml', 'users', 'users_roles',
})
CHECKS = frozenset({
    'config_metadata', 'keystore_load', 'credential_authentication',
    'runtime_identity', 'native_security', 'documents', 'mappings', 'aliases',
    'shards', 'timestamps', 'consumer_queries',
})


class EscrowError(ValueError):
    pass


def _digest(data):
    return hashlib.sha256(data).hexdigest()


def _encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def _binding(value):
    required = {'generation', 'source_uid', 'source_pod_uid', 'engine_image',
                'runtime_version', 'credential_versions', 'config_paths'}
    if not isinstance(value, dict) or set(value) != required:
        raise EscrowError('complete source binding required')
    if not isinstance(value['generation'], str) or not re.fullmatch('[0-9a-f]{32}', value['generation']):
        raise EscrowError('generation identity invalid')
    for key in required - {'generation', 'credential_versions', 'config_paths'}:
        if not isinstance(value[key], str) or not value[key]:
            raise EscrowError('source runtime identity incomplete')
    if not re.search('@sha256:[0-9a-f]{64}$', value['engine_image']):
        raise EscrowError('immutable engine image required')
    versions = value['credential_versions']
    if (not isinstance(versions, dict) or not versions
            or any(not isinstance(k, str) or not k or not isinstance(v, str) or not v
                   for k, v in versions.items())):
        raise EscrowError('exact credential versions required')
    paths = value['config_paths']
    if (not isinstance(paths, list) or not paths
            or any(not isinstance(p, str) or not p or p.startswith('/')
                   or any(x in ('', '.', '..') for x in p.split('/')) for p in paths)
            or len(set(paths)) != len(paths) or not CONFIG_FILES <= set(paths)):
        raise EscrowError('complete source-specific configuration dependency inventory required')
    return copy.deepcopy(value)


def _validate_parts(binding, parts):
    names = {'native', 'runtime', 'credentials'} | {'config/' + name for name in binding['config_paths']}
    if not isinstance(parts, dict) or set(parts) != names:
        raise EscrowError('native/config/keystore/runtime/credential coverage incomplete')
    entries = {}
    for name, part in parts.items():
        entries[name] = _validate_component(binding, name, part)
    return entries


def _validate_component(binding, name, part):
    if (not isinstance(part, dict) or set(part) != {'binding', 'data', 'mode', 'uid', 'gid'}
            or _binding(part['binding']) != binding):
        raise EscrowError('mixed or stale component binding')
    data = part['data']
    if not isinstance(data, bytes) or (not data and name not in {'config/users', 'config/users_roles'}):
        raise EscrowError('component bytes missing')
    if (type(part['mode']) is not int or not 0 <= part['mode'] <= 0o7777
            or any(type(part[k]) is not int or part[k] < 0 for k in ('uid', 'gid'))):
        raise EscrowError('original file metadata incomplete')
    return {'sha256': _digest(data), 'bytes': len(data),
            **{key: part[key] for key in ('mode', 'uid', 'gid')}}


def _current(expected, observe):
    if _binding(observe()) != expected:
        raise EscrowError('source identity or runtime/credential version changed')


def capture_export(binding, *, observe, require_capture_authority, capture, encrypt_export):
    """Capture complete state and encrypt inside the authorized boundary.

    require_capture_authority must enforce the independent live generation fence,
    not infer it from metadata. It is checked before and after each adapter call.
    The export adapter receives a complete immutable-by-copy component set and
    manifest, and returns ciphertext bytes plus an exact protected object ID.
    No plaintext file, network transport or production adapter is provided here.
    """
    expected = _binding(binding)

    def guard():
        if require_capture_authority(copy.deepcopy(expected)) is not True:
            raise EscrowError('affirmative capture authority required')
        _current(expected, observe)

    guard()
    parts = copy.deepcopy(capture(copy.deepcopy(expected)))
    guard()
    entries = _validate_parts(expected, parts)
    manifest = {'schema': 'k8s92-elasticsearch-escrow/v1',
                'binding': expected, 'entries': entries}
    manifest_hash = _digest(_encoded(manifest))
    exported = encrypt_export(copy.deepcopy(manifest), copy.deepcopy(parts))
    guard()
    if (not isinstance(exported, dict)
            or set(exported) != {'ciphertext', 'object_id', 'manifest_sha256'}
            or not isinstance(exported['ciphertext'], bytes) or not exported['ciphertext']
            or not isinstance(exported['object_id'], str) or not exported['object_id']
            or exported['manifest_sha256'] != manifest_hash
            or any(part['data'] and exported['ciphertext'] == part['data'] for part in parts.values())):
        raise EscrowError('encrypted export adapter receipt incomplete')
    # Ciphertext format/authentication must be qualified by the concrete adapter.
    # A callback receipt alone is not cryptographic or production acceptance.
    return {'manifest': manifest, 'manifest_sha256': manifest_hash,
            'object_id': exported['object_id'], 'ciphertext': exported['ciphertext'],
            'ciphertext_sha256': _digest(exported['ciphertext']),
            'production_acceptance': False}


def restore_export(exported, *, binding, target, require_isolated_authority,
                   decrypt, restore_configuration, verify_configuration, restore_native, verify):
    """Restore config/key/runtime first, then native security/index state.

    Concrete callbacks must enforce immutable target identity and isolated network
    admission throughout execution. This function never releases source admission.
    """
    expected = _binding(binding)
    if (not isinstance(target, dict) or set(target) != {'uid', 'isolated'}
            or target['isolated'] is not True or not isinstance(target['uid'], str)
            or not target['uid'] or target['uid'] in {expected['source_uid'], expected['source_pod_uid']}):
        raise EscrowError('independent isolated restore target required')
    target = copy.deepcopy(target)
    if not isinstance(exported, dict):
        raise EscrowError('export receipt absent')
    exported = copy.deepcopy(exported)
    manifest = exported.get('manifest')
    if (not isinstance(manifest, dict) or set(manifest) != {'schema', 'binding', 'entries'}
            or manifest['schema'] != 'k8s92-elasticsearch-escrow/v1'
            or _binding(manifest['binding']) != expected
            or exported.get('manifest_sha256') != _digest(_encoded(manifest))
            or not isinstance(exported.get('ciphertext'), bytes)
            or not exported['ciphertext']
            or exported.get('ciphertext_sha256') != _digest(exported['ciphertext'])
            or not isinstance(exported.get('object_id'), str) or not exported['object_id']):
        raise EscrowError('export generation or ciphertext integrity mismatch')

    def guard():
        if require_isolated_authority(copy.deepcopy(target), copy.deepcopy(expected)) is not True:
            raise EscrowError('affirmative isolated restore authority required')

    guard()
    parts = copy.deepcopy(decrypt(exported['ciphertext'], copy.deepcopy(manifest)))
    guard()
    if _validate_parts(expected, parts) != manifest['entries']:
        raise EscrowError('restored component bytes or metadata differ')
    return restore_parts(manifest, parts, target=target,
                         require_isolated_authority=require_isolated_authority,
                         restore_configuration=restore_configuration,
                         verify_configuration=verify_configuration,
                         restore_native=restore_native, verify=verify)


def restore_parts(manifest, parts, *, target, require_isolated_authority,
                  restore_configuration, verify_configuration, restore_native, verify):
    """Restore verified plaintext without inventing an encryption receipt.

    Used after independently qualified transport. This contract validates the
    component set again; caller adapters still own isolation and engine I/O.
    """
    manifest = copy.deepcopy(manifest)
    if (not isinstance(manifest, dict) or set(manifest) != {'schema', 'binding', 'entries'}
            or manifest['schema'] != 'k8s92-elasticsearch-escrow/v1'):
        raise EscrowError('restore manifest invalid')
    expected = _binding(manifest['binding'])
    if (not isinstance(target, dict) or set(target) != {'uid', 'isolated'}
            or target['isolated'] is not True or not isinstance(target['uid'], str)
            or not target['uid'] or target['uid'] in {expected['source_uid'], expected['source_pod_uid']}):
        raise EscrowError('independent isolated restore target required')
    target = copy.deepcopy(target)
    parts = copy.deepcopy(parts)

    def guard():
        if require_isolated_authority(copy.deepcopy(target), copy.deepcopy(expected)) is not True:
            raise EscrowError('affirmative isolated restore authority required')

    guard()
    if _validate_parts(expected, parts) != manifest['entries']:
        raise EscrowError('restored component bytes or metadata differ')
    config = {name: part for name, part in parts.items() if name != 'native'}
    if restore_configuration(copy.deepcopy(target), copy.deepcopy(expected), copy.deepcopy(config)) is not True:
        raise EscrowError('configuration/key/runtime restore incomplete')
    guard()
    prerequisites = verify_configuration(copy.deepcopy(target), copy.deepcopy(manifest))
    guard()
    required_checks = {'config_metadata', 'keystore_load', 'credential_authentication', 'runtime_identity'}
    if (not isinstance(prerequisites, dict) or set(prerequisites) != required_checks
            or any(v is not True for v in prerequisites.values())):
        raise EscrowError('configuration/key/runtime prerequisites unverified')
    if restore_native(copy.deepcopy(target), copy.deepcopy(expected), copy.deepcopy(parts['native'])) is not True:
        raise EscrowError('native restore incomplete')
    guard()
    checks = verify(copy.deepcopy(target), copy.deepcopy(manifest))
    guard()
    if not isinstance(checks, dict) or set(checks) != CHECKS or any(v is not True for v in checks.values()):
        raise EscrowError('isolated restore verification incomplete')
    return {'generation': expected['generation'], 'target_uid': target['uid'],
            'manifest_sha256': _digest(_encoded(manifest)), 'checks': copy.deepcopy(checks),
            'contract_verified': True, 'production_acceptance': False,
            'source_admission_released': False}
