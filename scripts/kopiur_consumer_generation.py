#!/usr/bin/env python3
"""Reconcile held consumer receipts with one closed physical-store generation.

Read-only admission, never starts applications or releases writer fences.
References and hashes must come from original captures and isolated restores.
A validated receipt is not production recovery acceptance.
"""
import argparse
import json
from pathlib import Path
import re

from kopiur_shared import InvalidEvidence, generation_lineage, inventory_report


def reconcile(ledger, receipt):
    if receipt.get('schema') != 'k8s92-consumer-generation/v1':
        raise InvalidEvidence('versioned consumer generation receipt required')
    generation = receipt.get('generation')
    if not isinstance(generation, str) or not re.fullmatch(r'[0-9a-f]{32}', generation):
        raise InvalidEvidence('exact original generation required')
    if receipt.get('capture_state') != 'closed' or receipt.get('revoked') is not False:
        raise InvalidEvidence('incomplete or revoked generation cannot be reconciled')
    consumers = receipt.get('consumers')
    stores = receipt.get('stores')
    if not isinstance(consumers, list) or not consumers or not isinstance(stores, list):
        raise InvalidEvidence('explicit consumer and physical store receipts required')
    if any(not isinstance(item, dict) for item in consumers + stores):
        raise InvalidEvidence('receipts must be objects')
    apps = [item.get('application') for item in consumers]
    if any(not isinstance(app, str) for app in apps):
        raise InvalidEvidence('consumer application identity required')
    plan = inventory_report(ledger, apps)
    by_store = {}
    for store in stores:
        identity = store.get('source')
        if not isinstance(identity, str) or identity in by_store:
            raise InvalidEvidence('physical store must appear exactly once')
        by_store[identity] = store
    required = {store['id'] for store in plan['physical_capture_plan']}
    if set(by_store) != required:
        raise InvalidEvidence('physical receipts differ from declared consumer dependencies')
    cohort = {app for store in plan['physical_capture_plan']
              for app in store['consumer_contracts']}
    if cohort != set(apps):
        raise InvalidEvidence('every declared shared-store consumer must remain in the cohort')
    results = []
    for contract, consumer in zip(plan['applications'], consumers):
        app = contract['application']
        if contract['unresolved_dependencies'] or not contract['stores']:
            raise InvalidEvidence('consumer has unresolved or empty state dependencies')
        if (consumer.get('execution') != 'HELD' or consumer.get('automatic_replay') is not False
                or consumer.get('generation') != generation):
            raise InvalidEvidence('consumer must remain held on the original generation')
        points = consumer.get('points')
        if (not isinstance(points, list) or any(not isinstance(point, dict)
                or not isinstance(point.get('nas'), dict)
                or not isinstance(point.get('r2'), dict) for point in points)):
            raise InvalidEvidence('complete consumer source point objects required')
        sources = [store['id'] for store in contract['stores']]
        lineage = generation_lineage(app, points, sources)
        if lineage['generation'] != generation:
            raise InvalidEvidence('consumer generation differs from closed physical generation')
        for point in points:
            physical = by_store[point['source']]
            if any(point.get(key) != physical.get(key) for key in ('generation', 'nas', 'r2')):
                raise InvalidEvidence('consumer receipt differs from original physical capture')
        results.append({'application': app, 'generation': generation,
                        'source_count': len(sources), 'execution': 'HELD',
                        'automatic_replay': False, 'lineage_validated': True,
                        'application_recovery_accepted': False})
    return {'schema': 'k8s92-consumer-reconciliation/v1', 'generation': generation,
            'consumers': results, 'physical_store_count': len(required),
            'production_recovery_accepted': False, 'release_authorized': False,
            'production_mutation_performed': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ledger', type=Path, required=True)
    parser.add_argument('--receipt', type=Path, required=True)
    args = parser.parse_args()
    try:
        result = reconcile(json.loads(args.ledger.read_text()), json.loads(args.receipt.read_text()))
    except (InvalidEvidence, KeyError, TypeError, ValueError) as exc:
        parser.exit(1, 'consumer reconciliation refused: ' + str(exc) + '\n')
    print(json.dumps(result, sort_keys=True))


if __name__ == '__main__':
    main()
