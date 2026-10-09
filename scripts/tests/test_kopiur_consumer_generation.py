"""Held lineage admission, synthetic receipts are not recovery evidence."""
import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from kopiur_consumer_generation import reconcile
from kopiur_shared import InvalidEvidence


def fixture():
    apps = [('media/tubearchivist', ['database/elasticsearch', 'redis:ta', 'cache:ta']),
            ('services/zoo-cowbell', ['database/elasticsearch', 'postgres:cowbell'])]
    ledger = {'applications': [], 'physical_stores': [], 'shared_prerequisites': []}
    for app, dependencies in apps:
        ledger['applications'].append({'id': app, 'state_dependencies': dependencies,
            'execution_owner': 'original-lane', 'category': 'database-stateful',
            'retention_requirements': {}, 'credentials_environment_dependencies': {'references': []},
            'required_next_gates': ['original application-visible recovery']})
    for source in sorted({source for _, sources in apps for source in sources}):
        ledger['physical_stores'].append({'id': source, 'capture_execution_owner': 'original-lane',
            'physical_capture_owner': 'fixture',
            'consumer_contracts': [app for app, sources in apps if source in sources]})
    generation = 'a' * 32
    stores = [{'source': store['id'], 'generation': generation,
               'nas': {'snapshot_id': 'b' * 32, 'manifest_sha256': 'c' * 64},
               'r2': {'snapshot_id': 'd' * 32, 'manifest_sha256': 'c' * 64,
                      'source_nas_id': 'b' * 32}} for store in ledger['physical_stores']]
    receipt = {'schema': 'k8s92-consumer-generation/v1', 'generation': generation,
               'capture_state': 'closed', 'revoked': False, 'stores': stores,
               'consumers': [{'application': app, 'generation': generation,
                   'execution': 'HELD', 'automatic_replay': False,
                   'points': [dict(copy.deepcopy(store), application=app)
                              for store in stores if store['source'] in dependencies]}
                   for app, dependencies in apps]}
    return ledger, receipt


class ConsumerTests(unittest.TestCase):
    def test_shared_store_captured_once_and_all_consumers_held(self):
        ledger, receipt = fixture()
        original = copy.deepcopy((ledger, receipt))
        result = reconcile(ledger, receipt)
        self.assertEqual(result['physical_store_count'], 4)
        self.assertEqual([item['source_count'] for item in result['consumers']], [3, 2])
        self.assertFalse(result['release_authorized'])
        self.assertFalse(result['production_recovery_accepted'])
        self.assertEqual((ledger, receipt), original)

    def test_incomplete_revoked_or_released_generation_denied(self):
        for field, value in [('capture_state', 'intent'), ('capture_state', 'released'),
                             ('revoked', True), ('revoked', None), ('generation', 'latest')]:
            ledger, receipt = fixture()
            receipt[field] = value
            with self.subTest(field=field, value=value), self.assertRaises(InvalidEvidence):
                reconcile(ledger, receipt)

    def test_consumer_mixed_generation_or_replay_denied(self):
        for field, value in [('generation', 'e' * 32), ('execution', 'OPEN'),
                             ('automatic_replay', True), ('automatic_replay', None)]:
            ledger, receipt = fixture()
            receipt['consumers'][0][field] = value
            with self.subTest(field=field), self.assertRaises(InvalidEvidence):
                reconcile(ledger, receipt)

    def test_missing_duplicate_or_extra_physical_store_denied(self):
        for kind in ('missing', 'duplicate', 'extra'):
            ledger, receipt = fixture()
            if kind == 'missing':
                receipt['stores'].pop()
            else:
                extra = copy.deepcopy(receipt['stores'][0])
                if kind == 'extra':
                    extra['source'] = 'unknown'
                receipt['stores'].append(extra)
            with self.subTest(kind=kind), self.assertRaises(InvalidEvidence):
                reconcile(ledger, receipt)

    def test_consumer_physical_point_identity_or_hash_mismatch_denied(self):
        for tier, field, value in [('nas', 'snapshot_id', 'e' * 32),
                                    ('nas', 'manifest_sha256', 'f' * 64),
                                    ('r2', 'source_nas_id', 'e' * 32),
                                    ('r2', 'snapshot_id', 'e' * 32)]:
            ledger, receipt = fixture()
            receipt['consumers'][0]['points'][0][tier][field] = value
            with self.subTest(tier=tier, field=field), self.assertRaises(InvalidEvidence):
                reconcile(ledger, receipt)

    def test_consumer_missing_duplicate_unknown_or_cross_app_source_denied(self):
        for kind in ('missing', 'duplicate', 'unknown', 'cross-app', 'mixed'):
            ledger, receipt = fixture()
            points = receipt['consumers'][0]['points']
            if kind == 'missing':
                points.pop()
            elif kind == 'duplicate':
                points.append(copy.deepcopy(points[0]))
            elif kind == 'unknown':
                points[0]['source'] = 'unknown'
            elif kind == 'cross-app':
                points[0]['application'] = 'services/zoo-cowbell'
            else:
                points[0]['generation'] = 'e' * 32
            with self.subTest(kind=kind), self.assertRaises(InvalidEvidence):
                reconcile(ledger, receipt)

    def test_unresolved_consumer_dependency_denied(self):
        ledger, receipt = fixture()
        ledger['applications'][0]['state_dependencies'].append('unresolved')
        with self.assertRaises(InvalidEvidence):
            reconcile(ledger, receipt)

    def test_omitted_shared_consumer_denied(self):
        ledger, receipt = fixture()
        receipt['consumers'].pop()
        required = set(ledger['applications'][0]['state_dependencies'])
        receipt['stores'] = [store for store in receipt['stores'] if store['source'] in required]
        with self.assertRaises(InvalidEvidence):
            reconcile(ledger, receipt)

    def test_malformed_consumer_points_denied(self):
        for value in (None, [None], [{'source': 'database/elasticsearch', 'nas': None, 'r2': {}}]):
            ledger, receipt = fixture()
            receipt['consumers'][0]['points'] = value
            with self.subTest(value=value), self.assertRaises(InvalidEvidence):
                reconcile(ledger, receipt)

    def test_cli_real_process_readonly_and_failure(self):
        ledger, receipt = fixture()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            inventory, points = root / 'ledger.json', root / 'receipt.json'
            inventory.write_text(json.dumps(ledger))
            points.write_text(json.dumps(receipt))
            before = (inventory.read_bytes(), points.read_bytes())
            command = [sys.executable, str(Path(__file__).resolve().parents[1] /
                       'kopiur_consumer_generation.py'), '--ledger', str(inventory),
                       '--receipt', str(points)]
            result = subprocess.run(command, capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)['physical_store_count'], 4)
            self.assertEqual((inventory.read_bytes(), points.read_bytes()), before)
            receipt['revoked'] = True
            points.write_text(json.dumps(receipt))
            result = subprocess.run(command, capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 1)
            self.assertEqual(result.stdout, b'')


if __name__ == '__main__':
    unittest.main()
