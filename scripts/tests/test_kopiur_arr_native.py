"""Credential-free extended ARR regressions, executed on authorized ARC only."""
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import kopiur_arr_native as arr


class ExtendedArrTests(unittest.TestCase):
    def setUp(self):
        self.native = arr.contract()
        self.scope = self.native['fixture'].__globals__

    def test_chaptarr_maps_all_three_native_databases(self):
        drill = self.native['DockerDrill']('chaptarr')
        self.assertEqual(drill.databases, ['chaptarr_main', 'chaptarr_log', 'chaptarr_cache'])
        with patch.object(drill, 'start', return_value='fixture') as start:
            drill.app('app', 'database', 'config')
        env = start.call_args.kwargs['env']
        self.assertEqual(env['CHAPTARR__POSTGRES__CACHEDB'], 'chaptarr_cache')
        self.assertEqual(env['CHAPTARR__POSTGRES__LOGDB'], 'chaptarr_log')
        self.assertEqual(start.call_args.kwargs['network'], 'container:database')

    def test_reconciliation_rewrites_cache_without_changing_source(self):
        drill = self.native['DockerDrill']('chaptarr')
        source = b'<Config><PostgresHost>old</PostgresHost><PostgresCacheDb>old</PostgresCacheDb><PostgresCacheDb>duplicate</PostgresCacheDb></Config>'
        # Only the bounded output of isolated_config, never untrusted XML input.
        tree = ET.fromstring(drill.isolated_config(source))
        self.assertEqual(tree.findtext('PostgresHost'), '127.0.0.1')
        self.assertEqual([x.text for x in tree.findall('PostgresCacheDb')], ['chaptarr_cache'])
        self.assertIn(b'duplicate', source)
        for unsafe in (b'<!DOCTYPE Config><Config/>', b'<!ENTITY x "y"><Config/>'):
            with self.assertRaises(ValueError):
                drill.isolated_config(unsafe)

    def test_application_api_and_original_identity_are_used(self):
        drill = self.native['DockerDrill']('prowlarr')
        response = SimpleNamespace(stdout=json.dumps([{'id': 1, 'name': 'Fixture'}]).encode())
        with patch.dict(self.scope, run=lambda *a, **kw: response):
            before = drill.application_state('fixture')
            response.stdout = json.dumps([{'id': 1, 'name': 'Changed'}]).encode()
            self.assertNotEqual(before, drill.application_state('fixture'))
            response.stdout = b'[]'
            with self.assertRaises(ValueError):
                drill.application_state('fixture')

    def test_provider_identity_allowlist_is_not_expanded(self):
        import runpy
        channel = runpy.run_path(str(arr.ROOT / 'scripts/kopiur-identity-transport.py'))
        self.assertNotIn('prowlarr', channel['APPS'])
        self.assertNotIn('chaptarr', channel['APPS'])
        for invalid in ('other', '../prowlarr'):
            with self.assertRaises(ValueError):
                arr.fixture(invalid)


if __name__ == '__main__':
    unittest.main()
