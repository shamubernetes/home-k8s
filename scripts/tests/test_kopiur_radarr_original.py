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

    def test_radarr_3d_rejects_main_radarr_transport(self):
        fields = self.fields()
        fields['R2_BUCKET'] = 'kopiur-radarr-3d'
        fields['NAS_RCLONE_CONFIG'] = fields['NAS_RCLONE_CONFIG'].replace('kp-radarr', 'kp-radarr-3d')
        ORIGINAL['validate_fields'](fields, 'radarr-3d')
        with self.assertRaises(ValueError):
            ORIGINAL['validate_fields'](self.fields(), 'radarr-3d')
        with self.assertRaises(ValueError):
            ORIGINAL['validate_fields'](fields, 'radarr')
        self.assertEqual(NATIVE['CONTRACTS']['radarr-3d'][1], ['radarr_3d_main'])
        self.assertEqual(ORIGINAL['RADARR_3D_ORIGINALS'], {
            'nas': '4b974fb1e16ad236f02a98a32e7df660',
            'r2': '5ce5f1e5dfc49c707e15efd19f94ad6f'})
        self.assertTrue(set(ORIGINAL['RADARR_3D_ORIGINALS'].values()).isdisjoint(
            ORIGINAL['RADARR_ORIGINALS'].values()))

    def test_sonarr_native_api_compares_every_series(self):
        records = [{'id': 1, 'tvdbId': 101, 'title': 'Fixture', 'path': '/media/fixture'},
                   {'id': 2, 'tvdbId': 102, 'title': 'Other fixture', 'path': '/media/other'}]
        def sql(query):
            self.assertIn('FROM "Series"', query)
            self.assertIn('"TvdbId"', query)
            self.assertNotIn('"Movies"', query)
            return json.dumps(records)
        proof = NATIVE['validate_radarr_api'](list(reversed(records)), 2, sql, app='sonarr')
        self.assertEqual(proof['series'], 2)
        self.assertEqual(proof['native_records_equal'], 2)
        self.assertNotIn('/media', json.dumps(proof))
        for field, value in [('id', True), ('id', 99), ('tvdbId', 999),
                             ('title', 'Wrong title'), ('path', '/wrong')]:
            changed = copy.deepcopy(records)
            changed[0][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                NATIVE['validate_radarr_api'](changed, 2, sql, app='sonarr')
        with self.assertRaises(ValueError):
            NATIVE['validate_radarr_api'](records, 3, sql, app='sonarr')
        with self.assertRaises(ValueError):
            NATIVE['validate_radarr_api']([records[0], records[0]], 2, sql, app='sonarr')
        self.assertEqual(NATIVE['validate_radarr_api']([], 0, lambda query: '[]',
                                                    app='sonarr')['series'], 0)

    def test_empty_native_movie_catalog(self):
        proof = NATIVE['validate_radarr_api']([], 0, lambda query: '[]')
        self.assertEqual(proof['movies'], 0)
        self.assertEqual(proof['native_records_equal'], 0)
        with self.assertRaises(ValueError):
            NATIVE['validate_radarr_api']([], 1, lambda query: '[]')

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

    def test_radarr_reader_bounds_local_cache_without_writes(self):
        source = (ROOT / 'scripts/kopiur-bazarr-original.py').read_text()
        self.assertIn('cache = "" if app == "bazarr" else', source)
        for option in ('content-cache-size-mb=64', 'content-cache-size-limit-mb=128',
                       'metadata-cache-size-mb=64', 'metadata-cache-size-limit-mb=128',
                       'content-min-sweep-age=0s', 'metadata-min-sweep-age=0s'):
            self.assertIn('--' + option, source)
        self.assertIn('backend + cache + " --readonly >/dev/null', source)
        self.assertNotIn('kopia cache set', source)

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
