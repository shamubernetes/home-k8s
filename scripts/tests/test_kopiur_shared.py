#!/usr/bin/env python3
"""Shared helper regressions. Executed by the trusted-main ARC identities suite."""
import copy
import hashlib
import os
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import kopiur_shared as shared


def ledger():
    rows = []
    for app, owner, category in [('arrs/bazarr', 'database-lane', 'database-stateful'),
                                  ('media/plex', 'filesystem-lane', 'filesystem-volume-object')]:
        rows.append({'id': app, 'execution_owner': owner, 'category': category,
                     'state_dependencies': ['nfs:shared'], 'retention_requirements': {
                         'numeric_recovery_policy': None, 'required': 'preserve incumbents'},
                     'credentials_environment_dependencies': {'references': []},
                     'required_next_gates': ['native isolated boot']})
    return {'applications': rows, 'physical_stores': [{
        'id': 'nfs:shared', 'physical_capture_owner': 'NAS owner',
        'capture_execution_owner': 'filesystem-lane',
        'consumer_contracts': [row['id'] for row in rows]}],
        'shared_prerequisites': [{'name': 'shared NAS capture',
                                 'consumers': [row['id'] for row in rows]}]}


def receipt():
    return {'app': 'bazarr', 'owned_fixture_repositories_removed': True,
            'results': [{'backend': tier, 'snapshot_id': char * 32, 'bytes_equal': 4096,
                         'producer_removed_before_restore': True,
                         'wrong_encryption_password_denied': True,
                         'fresh_container_direct_restore': True,
                         'guest_access_denied': True if tier == 'nas' else None}
                        for tier, char in [('nas', 'a'), ('r2', 'b')]]}


class SelectionTests(unittest.TestCase):
    def test_selected_subset_does_not_require_unrelated_credentials(self):
        self.assertEqual(shared.selected_apps({'operation': 'select', 'apps': ['bazarr']},
                                             {'bazarr', 'plex'}), {'bazarr'})

    def test_empty_duplicate_unknown_or_malformed_selection_denied(self):
        for apps in ([], ['bazarr', 'bazarr'], ['unknown'], 'bazarr', [None]):
            with self.subTest(apps=apps), self.assertRaises(shared.InvalidEvidence):
                shared.selected_apps({'operation': 'select', 'apps': apps}, {'bazarr'})

    def test_explicit_selection_operation_required(self):
        with self.assertRaises(shared.InvalidEvidence):
            shared.selected_apps({'apps': ['bazarr']}, {'bazarr'})


class ReceiptTests(unittest.TestCase):
    def test_real_transport_receipt_contract(self):
        self.assertEqual(shared.transport_receipt(receipt(), 'bazarr'), receipt())

    def test_wrong_service_or_failed_cleanup_denied(self):
        with self.assertRaises(shared.InvalidEvidence):
            shared.transport_receipt(receipt(), 'plex')
        value = receipt()
        value['owned_fixture_repositories_removed'] = False
        with self.assertRaises(shared.InvalidEvidence):
            shared.transport_receipt(value, 'bazarr')

    def test_duplicate_or_missing_backend_denied(self):
        for results in ([receipt()['results'][0]], [receipt()['results'][0]] * 2):
            value = receipt()
            value['results'] = results
            with self.assertRaises(shared.InvalidEvidence):
                shared.transport_receipt(value, 'bazarr')

    def test_no_fixture_claim_can_replace_fresh_restore_and_key_denial(self):
        for key, value in [('snapshot_id', 'latest'), ('bytes_equal', 0),
                           ('producer_removed_before_restore', False),
                           ('fresh_container_direct_restore', False),
                           ('wrong_encryption_password_denied', False),
                           ('guest_access_denied', False)]:
            candidate = receipt()
            candidate['results'][0][key] = value
            with self.subTest(key=key), self.assertRaises(shared.InvalidEvidence):
                shared.transport_receipt(candidate, 'bazarr')


class InventoryTests(unittest.TestCase):
    def test_shared_physical_store_captured_once(self):
        result = shared.inventory_report(ledger())
        self.assertEqual(result['application_count'], 2)
        self.assertEqual(result['physical_capture_count'], 1)
        self.assertFalse(result['retirement_authorized'])
        self.assertTrue(all(not row['application_recovery_accepted'] for row in result['applications']))

    def test_lane_report_keeps_consumer_and_unknown_policy(self):
        result = shared.inventory_report(ledger(), ['arrs/bazarr'])
        self.assertEqual(result['applications'][0]['retention']['numeric_recovery_policy'], None)
        self.assertEqual(result['physical_capture_plan'][0]['consumer_contracts'],
                         ['arrs/bazarr', 'media/plex'])

    def test_duplicate_rows_stores_or_unowned_store_denied(self):
        for kind in ('application', 'store', 'owner'):
            candidate = ledger()
            if kind == 'application':
                candidate['applications'].append(copy.deepcopy(candidate['applications'][0]))
            elif kind == 'store':
                candidate['physical_stores'].append(copy.deepcopy(candidate['physical_stores'][0]))
            else:
                candidate['physical_stores'][0]['capture_execution_owner'] = None
            with self.subTest(kind=kind), self.assertRaises(shared.InvalidEvidence):
                shared.inventory_report(candidate)

    def test_unknown_consumer_or_duplicate_dependency_denied(self):
        for kind in ('consumer', 'duplicate'):
            candidate = ledger()
            if kind == 'consumer':
                candidate['physical_stores'][0]['consumer_contracts'] = ['unknown']
            else:
                candidate['applications'][0]['state_dependencies'] *= 2
            with self.subTest(kind=kind), self.assertRaises(shared.InvalidEvidence):
                shared.inventory_report(candidate)

    def test_missing_dependency_lineage_blocks_only_affected_application(self):
        candidate = ledger()
        candidate['applications'][0]['state_dependencies'].append('unknown')
        result = shared.inventory_report(candidate)
        self.assertEqual(result['applications'][0]['unresolved_dependencies'], ['unknown'])
        self.assertEqual(result['applications'][1]['unresolved_dependencies'], [])
        self.assertEqual(result['physical_capture_count'], 1)
        self.assertEqual(result['physical_store_inventory_count'], 1)

    def test_unknown_or_duplicate_requested_apps_denied(self):
        for apps in (['unknown'], ['arrs/bazarr', 'arrs/bazarr']):
            with self.assertRaises(shared.InvalidEvidence):
                shared.inventory_report(ledger(), apps)


class ArtifactTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='k8s92-shared-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.file = self.root / 'native-export.bin'
        self.file.write_bytes(b'original-generation')
        info = self.file.stat()
        self.manifest = {'schema': 'k8s92-artifact/v1', 'entries': [{
            'path': self.file.name, 'kind': 'file', 'uid': info.st_uid,
            'gid': info.st_gid, 'mode': info.st_mode & 0o7777,
            'size': info.st_size, 'sha256': hashlib.sha256(self.file.read_bytes()).hexdigest()}]}

    def test_content_owner_mode_equal_without_native_acceptance(self):
        result = shared.validate_artifact(self.root, self.manifest)
        self.assertEqual(result['entries_equal'], 1)
        self.assertFalse(result['native_recovery_accepted'])

    def test_changed_bytes_missing_or_extra_entry_denied(self):
        self.file.write_bytes(b'other-generation')
        with self.assertRaises(shared.InvalidEvidence):
            shared.validate_artifact(self.root, self.manifest)
        self.file.unlink()
        with self.assertRaises(shared.InvalidEvidence):
            shared.validate_artifact(self.root, self.manifest)
        self.file.write_bytes(b'original-generation')
        (self.root / 'undeclared').write_bytes(b'extra')
        with self.assertRaises(shared.InvalidEvidence):
            shared.validate_artifact(self.root, self.manifest)

    def test_symlink_fifo_and_path_escape_denied(self):
        self.file.unlink()
        self.file.symlink_to('/dev/null')
        with self.assertRaises(shared.InvalidEvidence):
            shared.validate_artifact(self.root, self.manifest)
        self.file.unlink()
        os.mkfifo(self.file)
        with self.assertRaises(shared.InvalidEvidence):
            shared.validate_artifact(self.root, self.manifest)
        for path in ('../export', '/absolute', './export', 'a//b'):
            candidate = copy.deepcopy(self.manifest)
            candidate['entries'][0]['path'] = path
            with self.subTest(path=path), self.assertRaises(shared.InvalidEvidence):
                shared.validate_artifact(self.root, candidate)

    def test_owner_mode_size_and_hash_mismatch_denied(self):
        for key, value in [('uid', os.getuid() + 1), ('mode', 0), ('size', 0),
                           ('sha256', '0' * 64)]:
            candidate = copy.deepcopy(self.manifest)
            candidate['entries'][0][key] = value
            with self.subTest(key=key), self.assertRaises(shared.InvalidEvidence):
                shared.validate_artifact(self.root, candidate)

    def test_empty_or_duplicate_manifest_denied(self):
        for entries in ([], self.manifest['entries'] * 2):
            candidate = copy.deepcopy(self.manifest)
            candidate['entries'] = entries
            with self.assertRaises(shared.InvalidEvidence):
                shared.validate_artifact(self.root, candidate)


if __name__ == '__main__':
    unittest.main()
