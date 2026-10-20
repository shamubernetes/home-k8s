#!/usr/bin/env python3
"""Database-backed Grimmory native API contract, without production access."""
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import kopiur_grimmory_original as original


class DatabaseApiTests(unittest.TestCase):
    def test_exact_http_200_and_literal_false_required(self):
        self.assertTrue(original.validate_setup_response(200, {'status': 200, 'data': False}))
        for status, body in [(201, {'status': 200, 'data': False}),
                             (503, {'status': 200, 'data': False}),
                             (200, {'status': 200, 'data': 0}),
                             (200, {'status': 200, 'data': None}),
                             (200, {'status': 200, 'data': True}),
                             (200, {'status': 500, 'data': False}),
                             (200, {'status': True, 'data': False}),
                             (200, {}), (200, [])]:
            with self.subTest(status=status, body=body):
                self.assertFalse(original.validate_setup_response(status, body))

    def test_same_validation_program_runs_in_native_probe(self):
        for data, expected in [(False, True), (0, False)]:
            body = json.dumps({'status': 200, 'data': data}).encode()
            class Drill:
                def python(self, app, program):
                    setup = ('import io,urllib.request\n'
                             'class Reply(io.BytesIO):\n    status=200\n'
                             f'payload={body!r}\n'
                             'urllib.request.urlopen=lambda *args,**kwargs:Reply(payload)\n')
                    result = subprocess.run([sys.executable, '-c', setup + program],
                                            capture_output=True, text=True, timeout=10, check=True)
                    return result.stdout
            with mock.patch.object(original.native, 'docker', return_value='0\n'):
                self.assertIs(original.database_backed_setup_status(Drill(), 'app', 'database'), expected)

    def test_nonempty_original_requires_its_own_api_contract(self):
        drill = mock.Mock()
        with mock.patch.object(original.native, 'docker', return_value='1\n'):
            with self.assertRaises(ValueError):
                original.database_backed_setup_status(drill, 'app', 'database')
        drill.python.assert_not_called()


if __name__ == '__main__':
    unittest.main()
