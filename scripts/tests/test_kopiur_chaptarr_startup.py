#!/usr/bin/env python3
"""Host regressions, not native-image or production capture acceptance."""
import ast
import importlib.util
from pathlib import Path
import subprocess
import unittest
ROOT = Path(__file__).resolve().parents[2]
HOLD = ROOT / 'scripts/kopiur-chaptarr-startup-hold.sh'
NATIVE = ROOT / 'scripts/kopiur-chaptarr-startup-native.py'

class ChaptarrStartup(unittest.TestCase):
    def test_bash_syntax(self):
        subprocess.run(['bash', '-n', str(HOLD)], check=True)

    def test_start_rejects_non_pid1(self):
        r = subprocess.run(['bash', str(HOLD), 'start', '0'], capture_output=True)
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(r.stderr.strip(), b'Chaptarr startup hold rejected')

    def test_invalid_modes_and_arity(self):
        for args in [[], ['other'], ['start'], ['status', 'extra'], ['check', 'evidence']]:
            r = subprocess.run(['bash', str(HOLD), *args], capture_output=True)
            self.assertNotEqual(r.returncode, 0, args)

    def test_fixture_safety_survives_optimization(self):
        tree = ast.parse(NATIVE.read_text())
        self.assertFalse(any(isinstance(n, ast.Assert) for n in ast.walk(tree)))
        for optimize in [0, 1, 2]:
            code = compile(tree, str(NATIVE), 'exec', optimize=optimize)
            def constants(obj):
                import types
                for item in obj.co_consts:
                    if isinstance(item, types.CodeType):
                        yield from constants(item)
                    else:
                        yield item
            self.assertIn('native qualification check failed', list(constants(code)))

    def test_native_identity_is_exact(self):
        spec = importlib.util.spec_from_file_location('native', NATIVE)
        if spec is None or spec.loader is None:
            self.fail('native module unavailable')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.assertEqual(module.IMAGE, 'docker.io/chaptarr/chaptarr:0.9.965@sha256:bb00d67cbe1485d1cd61ef178362a29b42aa08f86b290841aab4b115ad2647a6')
        self.assertIn('exec /entrypoint.sh dotnet /app/Chaptarr.dll -nobrowser -data=/config', HOLD.read_text())

    def test_fixture_errors_are_not_empty_tree_or_cleanup_success(self):
        text = NATIVE.read_text()
        self.assertIn('found=$(find /config /media /ebooks -mindepth 1 -print -quit) || exit 1', text)
        self.assertIn('owned cleanup inspection failed', text)
        self.assertIn("docker('container', 'ls'", text)

if __name__ == '__main__':
    unittest.main()
