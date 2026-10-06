"""Documenso native fixture regressions for authorized ARC."""
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import kopiur_documenso_native as document


class DocumensoTests(unittest.TestCase):
    def setUp(self):
        self.scope = document.contract()
        self.drill = self.scope['DockerDrill']('documenso')

    def test_deployed_image_and_database(self):
        self.assertEqual(self.drill.image, document.IMAGE)
        self.assertEqual(self.drill.databases, ['documenso'])
        self.assertEqual(self.drill.port, 3000)

    def test_pdf_xref_offsets_match_real_objects(self):
        pdf = document.fixture_pdf()
        self.assertTrue(pdf.startswith(b'%PDF-1.4\n'))
        start = int(pdf.rsplit(b'startxref\n', 1)[1].splitlines()[0])
        self.assertEqual(pdf[start:start+4], b'xref')
        entries = pdf[start:].splitlines()[3:8]
        self.assertEqual(len(entries), 5)
        for index, entry in enumerate(entries, 1):
            offset = int(entry.split()[0])
            self.assertTrue(pdf[offset:].startswith(str(index).encode() + b' 0 obj'))

    def test_original_api_and_encryption_identity_preserved(self):
        value = {'original_fixture_key': 'api_original_fixture_key', 'original_encryption_key': 'a' * 64}
        raw = json.dumps(value).encode()
        self.assertEqual(self.drill.isolated_config(raw), raw)
        value['original_encryption_key'] = 'not-a-key'
        with self.assertRaises(ValueError):
            self.drill.isolated_config(json.dumps(value).encode())

    def test_multipart_identity_and_pdf_stay_in_stdin(self):
        invoke = Mock(return_value=SimpleNamespace(returncode=0, stdout=b'{}', stderr=b''))
        with patch.dict(self.scope, run=invoke):
            self.drill.create_pdf('fixture')
        args = invoke.call_args.args
        payload = invoke.call_args.kwargs['stdin']
        self.assertNotIn(self.drill.api_key, args)
        self.assertIn(self.drill.api_key.encode(), payload)
        self.assertIn(b'%PDF-1.4', payload)
        self.assertIn(b'multipart/form-data', payload)
        self.assertIn('container:fixture', args)
        self.assertIn('--read-only', args)


if __name__ == '__main__':
    unittest.main()
