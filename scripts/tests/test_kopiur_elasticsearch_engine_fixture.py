"""Adapter ordering regressions. Real engine acceptance runs separately on ARC."""
import copy
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from kopiur_elasticsearch_engine_fixture import EngineRestore
from kopiur_elasticsearch_escrow import EscrowError


class EngineAdapterTests(unittest.TestCase):
    def setUp(self):
        self.engine = EngineRestore.__new__(EngineRestore)
        self.engine.drill = Mock()
        self.engine.binding = {'generation': 'synthetic'}
        self.engine.parts = {'native': {'data': b'native'}, 'runtime': {'data': b'runtime'}}
        self.engine.check_config = Mock()
        self.target = {'uid': 'a' * 64}
        self.config = {'runtime': copy.deepcopy(self.engine.parts['runtime'])}

    def restore(self):
        with patch('kopiur_elasticsearch_engine_fixture.configuration_archive', return_value=b'archive'):
            return self.engine.configuration(self.target, self.engine.binding, self.config)

    def test_tar_member_owners_not_rewritten_to_container_user(self):
        self.assertIs(self.restore(), True)
        self.engine.drill.run.assert_called_once_with(
            'cp', '-', self.target['uid'] + ':/usr/share/elasticsearch/config', data=b'archive')
        self.engine.check_config.assert_called_once_with(self.target)
        self.engine.drill.start_registered.assert_not_called()

    def test_metadata_check_failure_does_not_start_engine(self):
        self.engine.check_config.side_effect = EscrowError('metadata differs')
        with self.assertRaises(EscrowError):
            self.restore()
        self.engine.drill.start_registered.assert_not_called()

    def test_changed_configuration_denied_before_archive_copy(self):
        self.config['runtime']['data'] = b'changed'
        with self.assertRaises(EscrowError):
            self.restore()
        self.engine.drill.run.assert_not_called()


if __name__ == '__main__':
    unittest.main()
