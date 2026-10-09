"""In-memory escrow bundles for the existing dedicated synthetic Kopia drill.

Kopia returns restored plaintext and repository receipts, not ciphertext bytes.
This bridge deliberately does not manufacture capture_export encryption receipts.
No original-source capture, production authorization or engine verification here.
"""
import base64
import copy
import json
import os
import sys

from kopiur_elasticsearch_escrow import (
    EscrowError, _binding, _digest, _encoded, _validate_parts, restore_parts,
)
from kopiur_elasticsearch_provider_fixture import restore_generation, validate_identity


SCHEMA = 'k8s92-elasticsearch-escrow-bundle/v1'


def encode_bundle(binding, parts):
    expected = _binding(binding)
    parts = copy.deepcopy(parts)
    entries = _validate_parts(expected, parts)
    manifest = {'schema': 'k8s92-elasticsearch-escrow/v1',
                'binding': expected, 'entries': entries}
    encoded_parts = {name: {**part, 'data': base64.b64encode(part['data']).decode('ascii')}
                     for name, part in parts.items()}
    return _encoded({'schema': SCHEMA, 'manifest': manifest, 'parts': encoded_parts}), manifest


def decode_bundle(data, expected_manifest):
    """Reject all changes before supplying any bytes to a restore adapter."""
    expected_manifest = copy.deepcopy(expected_manifest)
    if not isinstance(data, bytes):
        raise EscrowError('escrow bundle bytes required')

    def unique_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise EscrowError('duplicate escrow bundle key')
            result[key] = value
        return result

    try:
        value = json.loads(data, object_pairs_hook=unique_pairs)
        if (not isinstance(value, dict) or set(value) != {'schema', 'manifest', 'parts'}
                or value['schema'] != SCHEMA or _encoded(value['manifest']) != _encoded(expected_manifest)
                or _encoded(value) != data):
            raise EscrowError('escrow bundle manifest or canonical encoding differs')
        manifest = value['manifest']
        if (set(manifest) != {'schema', 'binding', 'entries'}
                or manifest['schema'] != 'k8s92-elasticsearch-escrow/v1'):
            raise EscrowError('escrow bundle manifest invalid')
        binding = _binding(manifest['binding'])
        if not isinstance(value['parts'], dict):
            raise EscrowError('escrow bundle components invalid')
        parts = value['parts']
        for part in parts.values():
            if not isinstance(part, dict) or not isinstance(part.get('data'), str):
                raise EscrowError('escrow component encoding invalid')
            text = part['data']
            part['data'] = base64.b64decode(text, validate=True)
            if base64.b64encode(part['data']).decode('ascii') != text:
                raise EscrowError('escrow component encoding noncanonical')
        if _validate_parts(binding, parts) != manifest['entries']:
            raise EscrowError('escrow component bytes or metadata differ')
        return parts
    except (ValueError, TypeError, KeyError, UnicodeError) as error:
        # Never include decoder details, component bytes or credentials in errors.
        raise EscrowError('escrow bundle invalid') from None


def roundtrip_synthetic_bundle(drill, binding, parts, *, require_authority, synthetic) -> tuple[dict, dict]:
    """Use the already approved exact-service NAS-to-R2 drill, ARC only.

    require_authority must independently enforce this owned disposable generation.
    Original bytes must not be supplied. The affirmative synthetic flag is scope,
    not a source fence or proof of original-source authorization.
    """
    if (synthetic is not True or sys.platform != 'linux'
            or not os.environ.get('RUNNER_NAME', '').startswith('ghar-set-zoo-')):
        raise EscrowError('owned synthetic ARC drill required')
    if drill.app != 'elasticsearch':
        raise EscrowError('dedicated Elasticsearch drill required')
    validate_identity(drill.fields)
    binding = _binding(binding)

    def guard():
        if require_authority(copy.deepcopy(binding)) is not True:
            raise EscrowError('affirmative synthetic generation authority required')

    guard()
    bundle, manifest = encode_bundle(binding, parts)
    guard()
    restored, receipt = restore_generation(drill, bundle)
    guard()
    digest = _digest(bundle)
    if (not isinstance(restored, bytes) or restored != bundle
            or not isinstance(receipt, dict)
            or receipt.get('same_generation_archive_sha256') != digest
            or receipt.get('r2_input_is_fresh_nas_restore') is not True
            or receipt.get('synthetic_archive_lineage_qualified') is not True
            or receipt.get('production_replication_qualified') is not False):
        raise EscrowError('synthetic escrow provider lineage incomplete')
    for backend in ('nas', 'r2'):
        evidence = receipt.get(backend)
        if (not isinstance(evidence, dict) or evidence.get('backend') != backend
                or evidence.get('archive_sha256') != digest
                or evidence.get('archive_bytes') != len(bundle)
                or type(evidence.get('archive_bytes')) is not int
                or any(evidence.get(key) is not True for key in
                       ('producer_removed_before_direct_restore', 'fresh_restorer', 'wrong_password_denied'))
                or any(not isinstance(evidence.get(key), str) or not evidence[key]
                       for key in ('snapshot_id', 'object_id'))):
            raise EscrowError('synthetic escrow provider evidence incomplete')
    if receipt['r2'].get('source_nas_snapshot_id') != receipt['nas']['snapshot_id']:
        raise EscrowError('synthetic escrow NAS-to-R2 snapshot lineage differs')
    restored_parts = decode_bundle(restored, manifest)
    guard()
    return restored_parts, {'binding': copy.deepcopy(binding), 'manifest': manifest,
                            'bundle_sha256': digest, 'provider': copy.deepcopy(receipt),
                            'synthetic_bundle_transport_verified': True,
                            'engine_restore_verified': False, 'production_acceptance': False}


def restore_synthetic_bundle(drill, binding, parts, *, require_authority, synthetic,
                             target, require_isolated_authority, restore_configuration,
                             verify_configuration, restore_native, verify):
    """Feed only provider-restored components to the isolated restore contract.

    No engine proof is inferred from bundle or provider receipts. Concrete
    adapters must perform and verify the isolated restoration independently.
    """
    restored, transport_receipt = roundtrip_synthetic_bundle(
        drill, binding, parts, require_authority=require_authority, synthetic=synthetic)
    proof = restore_parts(transport_receipt['manifest'], restored, target=target,
                          require_isolated_authority=require_isolated_authority,
                          restore_configuration=restore_configuration,
                          verify_configuration=verify_configuration,
                          restore_native=restore_native, verify=verify)
    return {'transport': transport_receipt, 'restore': proof,
            'production_acceptance': False, 'source_admission_released': False}
