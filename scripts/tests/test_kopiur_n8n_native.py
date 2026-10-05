"""n8n regressions, executed on authorized Linux ARC."""
import copy
from pathlib import Path
import sys
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import kopiur_n8n_native as n8n


class N8nTests(unittest.TestCase):
    def setUp(self):
        self.workflow = {'id': n8n.WORKFLOW_ID, 'name': 'fixture', 'active': False,
                         'nodes': [], 'connections': {}, 'settings': {}, 'updatedAt': 'runtime'}
        self.credential = {'id': n8n.CREDENTIAL_ID, 'name': 'fixture', 'type': 'httpBasicAuth',
                           'data': {'user': 'fixture', 'password': 'original-key'}}

    def test_deployment_pin_and_database(self):
        drill = n8n.contract()['DockerDrill']('n8n')
        self.assertEqual(drill.image, n8n.IMAGE)
        self.assertEqual(drill.databases, ['n8n'])
        self.assertEqual(drill.port, 5678)

    def test_native_projection_is_strict_except_runtime_timestamps(self):
        first = n8n.visible_state([self.workflow], [self.credential], 'original-key')
        self.workflow['updatedAt'] = 'later'
        self.assertEqual(first, n8n.visible_state([self.workflow], [self.credential], 'original-key'))
        self.workflow['active'] = True
        self.assertNotEqual(first, n8n.visible_state([self.workflow], [self.credential], 'original-key'))

    def test_changed_catalog_or_key_is_rejected(self):
        for workflows, credentials, key in (([], [self.credential], 'original-key'),
                ([self.workflow], [], 'original-key'), ([self.workflow], [self.credential], 'replacement-key')):
            with self.assertRaises(ValueError):
                n8n.visible_state(workflows, credentials, key)
        changed = copy.deepcopy(self.credential)
        changed['data']['password'] = 'changed'
        with self.assertRaises(ValueError):
            n8n.visible_state([self.workflow], [changed], 'original-key')


if __name__ == '__main__':
    unittest.main()
