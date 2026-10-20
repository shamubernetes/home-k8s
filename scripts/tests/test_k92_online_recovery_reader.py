#!/usr/bin/env python3
"""Online Whisparr reader compatibility, never cross-database atomicity proof."""
import hashlib
import io
from pathlib import Path
import runpy
import tarfile
import tempfile
import unittest

MODULE = runpy.run_path(str(Path(__file__).resolve().parents[1] / 'kopiur-postgres-drill'))


def bundle(root, databases, consistency):
    current = root / 'current'
    current.mkdir()
    artifacts = {'application-config': b'<Config/>', 'filetree.sha256': b'a' * 64 + b'\n',
                 'metadata': ('format=2\n' + consistency + '\n' + ''.join(
                     'database=' + db + ' server_version=170000 tables=1\n' for db in databases)).encode()}
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode='w') as handle:
        member = tarfile.TarInfo('config.xml')
        member.size = 1
        handle.addfile(member, io.BytesIO(b'x'))
    artifacts['application-state.tar'] = archive.getvalue()
    for db in databases:
        artifacts[db + '.dump'] = b'parser fixture only'
        artifacts[db + '.toc'] = b'parser fixture only'
    for name, data in artifacts.items():
        (current / name).write_bytes(data)
    (current / 'SHA256SUMS').write_text(''.join(
        hashlib.sha256(data).hexdigest() + '  ' + name + '\n' for name, data in artifacts.items()))
    (root / 'COMPLETE').write_text('complete\n')


class OnlineReaderTests(unittest.TestCase):
    def test_both_contracts_accept_only_exact_ordered_databases(self):
        correct = ['whisparrv3_main', 'whisparrv3_logs']
        for contract in MODULE['WHISPARR_CONSISTENCY']:
            for databases in (correct, correct[::-1], correct[:1], ['radarr_main', correct[1]]):
                with self.subTest(contract=contract, databases=databases), tempfile.TemporaryDirectory() as temp:
                    root = Path(temp)
                    bundle(root, databases, contract)
                    if databases == correct:
                        self.assertEqual(MODULE['verify_bundle'](root), correct)
                        self.assertIn(contract, (root / 'current/metadata').read_text())
                    else:
                        with self.assertRaises(ValueError):
                            MODULE['verify_bundle'](root)

    def test_unknown_or_ambiguous_contract_rejected(self):
        for contract in ('consistency=unknown', '\n'.join(MODULE['WHISPARR_CONSISTENCY'])):
            with self.subTest(contract=contract), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                bundle(root, ['whisparrv3_main', 'whisparrv3_logs'], contract)
                with self.assertRaises(ValueError):
                    MODULE['verify_bundle'](root)

    def test_payload_integrity_still_required(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            bundle(root, ['whisparrv3_main', 'whisparrv3_logs'], MODULE['WHISPARR_CONSISTENCY'][1])
            (root / 'current/whisparrv3_main.dump').write_bytes(b'changed')
            with self.assertRaisesRegex(ValueError, 'checksum'):
                MODULE['verify_bundle'](root)


if __name__ == '__main__':
    unittest.main()
