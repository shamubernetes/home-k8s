"""Radarr original recovery must use its own identity and native movie records."""
import copy
import io
import json
from pathlib import Path
import runpy
import tarfile
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
ORIGINAL = runpy.run_path(str(ROOT / 'scripts/kopiur-bazarr-original.py'))
NATIVE = ORIGINAL['NATIVE']


class RadarrOriginalTests(unittest.TestCase):
    def fields(self):
        return {'NAS_RCLONE_CONFIG': '[mnemosyne]\ntype=smb\nhost=10.100.47.100\nuser=kp-radarr\npass=fixture\ndomain=WORKGROUP\n',
                'NAS_KOPIA_PASSWORD': 'fixture', 'R2_KOPIA_PASSWORD': 'fixture',
                'R2_ACCESS_KEY_ID': 'fixture', 'R2_SECRET_ACCESS_KEY': 'fixture',
                'R2_BUCKET': 'kopiur-radarr'}

    def test_dedicated_radarr_identity(self):
        ORIGINAL['validate_fields'](self.fields(), 'radarr')
        with self.assertRaises(ValueError):
            ORIGINAL['validate_fields'](self.fields())
        for field, value in [('R2_BUCKET', 'kopiur-bazarr'),
                             ('NAS_RCLONE_CONFIG', self.fields()['NAS_RCLONE_CONFIG'].replace('kp-radarr', 'kp-bazarr'))]:
            fields = self.fields()
            fields[field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                ORIGINAL['validate_fields'](fields, 'radarr')
        with self.assertRaises(ValueError):
            ORIGINAL['validate_fields'](self.fields(), 'unrelated')

    def test_native_api_compares_every_movie(self):
        records = [{'id': 1, 'tmdbId': 101, 'title': 'Fixture', 'path': '/media/fixture'},
                   {'id': 2, 'tmdbId': 102, 'title': 'Other fixture', 'path': '/media/other'}]
        def sql(query):
            self.assertIn('JOIN "MovieMetadata" mm', query)
            self.assertIn('mm."Id"=m."MovieMetadataId"', query)
            self.assertIn('mm."TmdbId"', query)
            self.assertIn('mm."Title"', query)
            self.assertIn('m."Path"', query)
            return json.dumps(records)
        proof = NATIVE['validate_radarr_api'](list(reversed(records)), 2, sql)
        self.assertEqual(proof['movies'], 2)
        self.assertEqual(proof['native_records_equal'], 2)
        self.assertNotIn('/media', json.dumps(proof))
        for field, value in [('id', True), ('id', 99), ('tmdbId', 999),
                             ('title', 'Wrong title'), ('path', '/wrong')]:
            changed = copy.deepcopy(records)
            changed[0][field] = value
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                NATIVE['validate_radarr_api'](changed, 2, sql)
        with self.assertRaises(ValueError):
            NATIVE['validate_radarr_api'](records, 3, sql)
        with self.assertRaises(ValueError):
            NATIVE['validate_radarr_api']([records[0], records[0]], 2, sql)

    def test_transfer_allowlist_is_radarr_specific_and_capacity_bound(self):
        names = ['.kopiur-postgres/COMPLETE', '.kopiur-postgres/current',
                 '.kopiur-postgres/current/SHA256SUMS']
        names += ['.kopiur-postgres/current/' + name for name in
                  ('radarr_main.dump', 'radarr_main.toc', 'application-config',
                   'application-state.tar', 'filetree.sha256', 'metadata')]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive = root / 'transfer.tar'
            with tarfile.open(archive, 'w') as out:
                for name in names:
                    member = tarfile.TarInfo(name)
                    if name == '.kopiur-postgres/current':
                        member.type = tarfile.DIRTYPE
                        out.addfile(member)
                    else:
                        member.size = 1
                        out.addfile(member, io.BytesIO(b'x'))
            ORIGINAL['extract_original'](archive, root / 'good', database='radarr_main', capacity=16)
            with self.assertRaises(ValueError):
                ORIGINAL['extract_original'](archive, root / 'wrong-database')
            with self.assertRaises(ValueError):
                ORIGINAL['extract_original'](archive, root / 'too-small', database='radarr_main', capacity=1)


if __name__ == '__main__':
    unittest.main()
