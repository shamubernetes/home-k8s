"""The retained native db.db, not older catalogs, defines Tunarr channels."""
import ast
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / 'kubernetes/apps/media/tunarr/backups/original-native.py'


class OriginalCatalog(unittest.TestCase):
    def select(self, databases):
        module = ast.parse(SOURCE.read_text())
        prepare = next(n for n in module.body if isinstance(n, ast.FunctionDef) and n.name == 'prepare')
        first = next(i for i, n in enumerate(prepare.body)
                     if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'active_database'
                                                          for t in n.targets))
        last = next(i for i in range(first, len(prepare.body))
                    if isinstance(prepare.body[i], ast.Expr)
                    and ast.unparse(prepare.body[i]).startswith('require(databases'))
        require = next(n for n in module.body if isinstance(n, ast.FunctionDef) and n.name == 'require')
        namespace = {'databases': databases, 'native_root': Path('/work/data/stable-1.3'),
                     'target': Path('/work/data')}
        exec(compile(ast.Module(body=[require, *prepare.body[first:last + 1]], type_ignores=[]),
                     str(SOURCE), 'exec'), namespace)
        return namespace['channel_ids']

    def test_only_native_database_defines_catalog(self):
        databases = {'stable-1.3/db.db': {'channel_ids': ['current']},
                     'stable-1.3/old/db.db': {'channel_ids': ['historical']}}
        self.assertEqual(self.select(databases), ['current'])

    def test_empty_native_catalog_is_valid(self):
        self.assertEqual(self.select({'stable-1.3/db.db': {'channel_ids': []}}), [])

    def test_missing_native_database_fails(self):
        with self.assertRaises(ValueError):
            self.select({'stable-1.3/old/db.db': {'channel_ids': ['old']}})

    def test_missing_native_channel_table_fails(self):
        with self.assertRaises(ValueError):
            self.select({'stable-1.3/db.db': {'tables': 1}})


if __name__ == '__main__':
    unittest.main()
