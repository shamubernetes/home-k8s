"""Pelican native regressions, executed only on assigned Linux ARC."""
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import kopiur_pelican_native as pelican

class PelicanTests(unittest.TestCase):
    def setUp(self):
        self.scope=pelican.contract()
        self.drill=self.scope['DockerDrill']('pelican')
    def test_pinned_native_contract(self):
        self.assertEqual(self.drill.image,pelican.IMAGE)
        self.assertEqual(self.drill.databases,['pelican'])
    def test_preserves_original_application_key_and_token(self):
        raw=json.dumps({'appKey':'original','originalToken':'original','password':'original'}).encode()
        self.assertEqual(self.drill.isolated_config(raw),raw)
        with self.assertRaises(ValueError):
            self.drill.isolated_config(b'{}')
    def test_php_code_is_stdin_only_and_fixture_local(self):
        invoke=Mock(return_value=SimpleNamespace(stdout=b'{}',stderr=b'',returncode=0))
        with patch.dict(self.scope,run=invoke):
            self.drill.php('owned-fixture',pelican.VISIBLE)
        self.assertEqual(invoke.call_args.args,('docker','exec','-i','owned-fixture','php'))
        self.assertNotIn(self.drill.api_key,invoke.call_args.args)
        self.assertTrue(invoke.call_args.kwargs['stdin'].startswith(b'<?php\n'))
    def test_native_model_state_and_key_decryption_are_required(self):
        value={'uuid':'fixture','email':'recovery@fixture.invalid','originalTokenDecrypted':True,
               'originalMfaSecretDecrypted':True}
        self.drill.php=lambda *args:SimpleNamespace(stdout=json.dumps(value).encode())
        before=self.drill.application_state('owned-fixture')
        value['uuid']='changed'
        self.assertNotEqual(before,self.drill.application_state('owned-fixture'))
        value['originalMfaSecretDecrypted']=False
        with self.assertRaises(ValueError):
            self.drill.application_state('owned-fixture')
    def test_restore_cannot_seed_or_replace_original_identity(self):
        value={'appKey':'original','originalToken':'original','password':'original'}
        invoke=Mock(return_value=SimpleNamespace(stdout=json.dumps(value).encode(),returncode=0))
        self.drill.start=Mock(return_value='owned-fixture')
        with patch.dict(self.scope,run=invoke):
            self.drill.app('restored-app','owned-db','owned-config')
        command=self.drill.start.call_args.kwargs['command'][-1]
        self.assertNotIn('migrate',command)
        self.assertNotIn('pelican-seed.php',command)
        self.assertEqual(self.drill.api_key,'original')
        self.drill.api_key='different-expected'
        with patch.dict(self.scope,run=invoke),self.assertRaises(ValueError):
            self.drill.app('restored-app','owned-db','owned-config')

if __name__=='__main__':
    unittest.main()
