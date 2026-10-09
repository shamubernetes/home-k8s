#!/usr/bin/env python3
"""Shared recovery admission helpers. Reports evidence, never mutates production.

The inventory remains the authority for physical store ownership. A transport
receipt is only transport evidence, not a native application recovery acceptance.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat


class InvalidEvidence(ValueError):
    pass


def selected_apps(payload, allowed):
    apps = payload.get('apps')
    if (payload.get('operation') != 'select' or not isinstance(apps, list)
            or not apps or any(not isinstance(app, str) for app in apps)
            or len(set(apps)) != len(apps) or not set(apps) <= set(allowed)):
        raise InvalidEvidence('select an explicit nonempty unique approved service subset')
    return set(apps)


def transport_receipt(receipt, app):
    results = receipt.get('results')
    if (receipt.get('app') != app or receipt.get('owned_fixture_repositories_removed') is not True
            or not isinstance(results, list) or len(results) != 2
            or {point.get('backend') for point in results} != {'nas', 'r2'}):
        raise InvalidEvidence('both backend receipts and owned cleanup are required')
    for point in results:
        if (not isinstance(point.get('snapshot_id'), str)
                or not re.fullmatch(r'[0-9a-f]{32}', point['snapshot_id'])
                or point.get('bytes_equal') != 4096
                or any(point.get(key) is not True for key in (
                    'producer_removed_before_restore', 'wrong_encryption_password_denied',
                    'fresh_container_direct_restore'))
                or (point['backend'] == 'nas' and point.get('guest_access_denied') is not True)):
            raise InvalidEvidence('transport restore proof is incomplete')
    return receipt


def generation_lineage(app, points, required_sources):
    """Bind complete source generations to independent NAS/R2 point identities.

    Native writers must supply the original generation fingerprint after their
    own bounded capture fence. This helper does not stop production writers.
    """
    if (not isinstance(points, list) or not required_sources
            or len(points) != len(required_sources)
            or len(set(required_sources)) != len(required_sources)
            or {point.get('source') for point in points} != set(required_sources)):
        raise InvalidEvidence('all source generations must appear exactly once')
    generations = set()
    for point in points:
        generation = point.get('generation')
        if (point.get('application') != app or not isinstance(generation, str)
                or not re.fullmatch('[0-9a-f]{32}', generation)):
            raise InvalidEvidence('source generation belongs to another application')
        generations.add(generation)
        nas, r2 = point.get('nas', {}), point.get('r2', {})
        for tier in (nas, r2):
            if (not isinstance(tier.get('snapshot_id'), str)
                    or not re.fullmatch('[0-9a-f]{32}', tier['snapshot_id'])
                    or not isinstance(tier.get('manifest_sha256'), str)
                    or not re.fullmatch('[0-9a-f]{64}', tier['manifest_sha256'])):
                raise InvalidEvidence('exact point ID and original generation hash are required')
        if (r2.get('source_nas_id') != nas['snapshot_id']
                or nas['manifest_sha256'] != r2['manifest_sha256']):
            raise InvalidEvidence('R2 destination is not bound to the original NAS generation')
    if len(generations) != 1:
        raise InvalidEvidence('mixed capture generations are not a whole-state recovery point')
    return {'generation': generations.pop(), 'source_count': len(points),
            'lineage_validated': True, 'native_recovery_accepted': False}


def retention_contract(metadata):
    """Normalize unresolved policy without fabricating approval or pruning copies."""
    numeric = ('rpo_seconds', 'rto_seconds', 'history_seconds', 'minimum_copies',
               'rollback_seconds')
    unresolved = []
    for key in numeric:
        value = metadata.get(key)
        if value is None:
            unresolved.append(key)
        elif type(value) is not int or value <= 0:
            raise InvalidEvidence('retention policy must use positive integer units')
    # Neither a supplied number nor a passing synthetic restore authorizes
    # production deletion. Approval provenance must be checked in Kaneo by the
    # integration owner, and original keys/native independent drills retained.
    return {'numeric_policy': {key: metadata.get(key) for key in numeric},
            'unresolved_approval_fields': unresolved,
            'approval_reference': metadata.get('approval_reference'),
            'preserve_incumbent_points_sources_and_original_keys': True,
            'retirement_authorized': False}


def inventory_report(ledger, apps=None):
    rows = ledger['applications']
    stores = ledger['physical_stores']
    by_app = {row['id']: row for row in rows}
    by_store = {store['id']: store for store in stores}
    if len(by_app) != len(rows) or len(by_store) != len(stores):
        raise InvalidEvidence('duplicate application or physical store')
    for store in stores:
        consumers = store['consumer_contracts']
        if (not store.get('capture_execution_owner') or not store.get('physical_capture_owner')
                or len(consumers) != len(set(consumers)) or not set(consumers) <= set(by_app)):
            raise InvalidEvidence('physical store ownership or consumer references are incomplete')
    for row in rows:
        dependencies = row['state_dependencies']
        if len(dependencies) != len(set(dependencies)) or not row.get('execution_owner'):
            raise InvalidEvidence('application dependencies or owner are incomplete')

    if apps is None:
        apps = sorted(by_app)
    if len(apps) != len(set(apps)) or not set(apps) <= set(by_app):
        raise InvalidEvidence('unknown or duplicate requested application')
    selected = []
    for app in apps:
        row = by_app[app]
        selected.append({
            'application': app,
            'execution_owner': row['execution_owner'],
            'category': row['category'],
            'stores': [{
                'id': dependency,
                'capture_owner': by_store[dependency]['capture_execution_owner'],
                'physical_owner': by_store[dependency]['physical_capture_owner'],
                'backend': by_store[dependency].get('backend_contract'),
                'selection': by_store[dependency].get('selection'),
                'consumers': by_store[dependency]['consumer_contracts'],
                'evidence': by_store[dependency].get('evidence'),
            } for dependency in row['state_dependencies'] if dependency in by_store
                       and app in by_store[dependency]['consumer_contracts']],
            # Undeclared hostPaths and missing consumer lineage remain explicit
            # unresolved gates, not invented captures or global lane blockers.
            'unresolved_dependencies': [dependency for dependency in row['state_dependencies']
                       if dependency not in by_store
                       or app not in by_store[dependency]['consumer_contracts']],
            'native_backends_before_application_boot': sorted({
                by_store[dependency]['backend_contract']
                for dependency in row['state_dependencies'] if dependency in by_store
                and app in by_store[dependency]['consumer_contracts']
                and by_store[dependency].get('backend_contract')}),
            'shared_prerequisites': [p['name'] for p in ledger['shared_prerequisites']
                                     if app in p['consumers']],
            'retention': row['retention_requirements'],
            'retention_convention': retention_contract(
                row['retention_requirements'].get('approved_policy', {})),
            'escrow_references': row['credentials_environment_dependencies']['references'],
            'remaining_gates': row['required_next_gates'],
            'application_recovery_accepted': False,
        })
    # Each physical store appears once, even when several selected apps consume it.
    chosen = {dependency for app in apps for dependency in by_app[app]['state_dependencies']
              if dependency in by_store and app in by_store[dependency]['consumer_contracts']}
    return {'schema': 'k8s92-shared-report/v1', 'applications': selected,
            'physical_capture_plan': [by_store[store] for store in sorted(chosen)],
            'application_count': len(selected), 'physical_capture_count': len(chosen),
            'physical_store_inventory_count': len(by_store),
            'retirement_authorized': False, 'production_mutation_performed': False}


def validate_artifact(root, manifest):
    """Check a complete plaintext export tree before encryption or after restore.

    Invoke only on a quiescent isolated export, never a live writer tree.
    This is byte/owner/mode coverage only. Native engine and original key tests
    remain the application owner's responsibility. No symlinks are allowed.
    """
    root = Path(root)
    if root.is_symlink() or not root.is_dir():
        raise InvalidEvidence('artifact root must be a real directory')
    entries = manifest.get('entries')
    if manifest.get('schema') != 'k8s92-artifact/v1' or not isinstance(entries, list) or not entries:
        raise InvalidEvidence('a versioned nonempty complete manifest is required')
    expected = {}
    for entry in entries:
        path = entry.get('path')
        if (not isinstance(path, str) or not path or path.startswith('/')
                or any(part in ('', '.', '..') for part in path.split('/')) or path in expected):
            raise InvalidEvidence('artifact paths must be unique relative canonical paths')
        if entry.get('kind') not in ('file', 'directory'):
            raise InvalidEvidence('only regular files and directories are supported')
        for key in ('uid', 'gid', 'mode'):
            if type(entry.get(key)) is not int or entry[key] < 0:
                raise InvalidEvidence('numeric original ownership/mode are required')
        if entry['mode'] > 0o7777:
            raise InvalidEvidence('mode is outside permission bits')
        if entry['kind'] == 'file' and (
                not isinstance(entry.get('sha256'), str)
                or not re.fullmatch('[0-9a-f]{64}', entry['sha256'])
                or type(entry.get('size')) is not int or entry['size'] < 0):
            raise InvalidEvidence('file hash and size are required')
        expected[path] = entry
    actual = {}
    for directory, dirs, files in os.walk(root, followlinks=False):
        for name in dirs + files:
            target = Path(directory) / name
            info = target.lstat()
            relative = target.relative_to(root).as_posix()
            if stat.S_ISLNK(info.st_mode) or not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
                raise InvalidEvidence('symlink or special file in artifact')
            kind = 'directory' if stat.S_ISDIR(info.st_mode) else 'file'
            data = {'path': relative, 'kind': kind, 'uid': info.st_uid, 'gid': info.st_gid,
                    'mode': stat.S_IMODE(info.st_mode)}
            if kind == 'file':
                digest = hashlib.sha256()
                with os.fdopen(os.open(target, os.O_RDONLY | os.O_NOFOLLOW), 'rb') as source:
                    opened = os.fstat(source.fileno())
                    if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                        raise InvalidEvidence('artifact changed during validation')
                    for chunk in iter(lambda: source.read(1024 * 1024), b''):
                        digest.update(chunk)
                    after = os.fstat(source.fileno())
                    if (opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns) != (
                            after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                        raise InvalidEvidence('artifact changed during validation')
                data.update(size=info.st_size, sha256=digest.hexdigest())
            actual[relative] = data
    if set(actual) != set(expected):
        raise InvalidEvidence('artifact tree coverage differs from manifest')
    if any(actual[path] != expected[path] for path in actual):
        raise InvalidEvidence('artifact bytes, ownership or permissions differ')
    return {'entries_equal': len(actual), 'native_recovery_accepted': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    report = commands.add_parser('report')
    report.add_argument('--ledger', required=True, type=Path)
    report.add_argument('--app', action='append')
    artifact = commands.add_parser('artifact')
    artifact.add_argument('--root', required=True, type=Path)
    artifact.add_argument('--manifest', required=True, type=Path)
    lineage = commands.add_parser('lineage')
    lineage.add_argument('--receipt', required=True, type=Path)
    retention = commands.add_parser('retention')
    retention.add_argument('--metadata', required=True, type=Path)
    args = parser.parse_args()
    if args.command == 'report':
        result = inventory_report(json.loads(args.ledger.read_text()), args.app)
    elif args.command == 'artifact':
        result = validate_artifact(args.root, json.loads(args.manifest.read_text()))
    elif args.command == 'lineage':
        receipt = json.loads(args.receipt.read_text())
        result = generation_lineage(receipt['application'], receipt['points'], receipt['required_sources'])
    else:
        result = retention_contract(json.loads(args.metadata.read_text()))
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
