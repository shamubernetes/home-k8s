"""Exercise real original-point selection and the dispatch input guard."""
import ast
import os
from pathlib import Path
import re
import subprocess
from typing import Any
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "scripts/kopiur-bazarr-original.py"
WORKFLOW = ROOT / ".github/workflows/recovery-verify.yaml"
NAS = "a" * 32
R2 = "b" * 32
APPS = ("bazarr", "radarr", "radarr-3d", "whisparr")


def selector():
    node = next(n for n in ast.parse(SOURCE.read_text()).body
                if isinstance(n, ast.FunctionDef) and n.name == "originals_for")
    namespace: dict[str, Any] = {"os": os, "re": re}
    for name in ("ORIGINALS", "RADARR_ORIGINALS", "RADARR_3D_ORIGINALS", "SONARR_ORIGINALS"):
        namespace[name] = {"nas": "c" * 32, "r2": "d" * 32}
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(SOURCE), "exec"), namespace)
    return namespace["originals_for"]


class OriginalInputs(unittest.TestCase):
    def test_explicit_supported_points(self):
        with patch.dict(os.environ, {"ORIGINAL_NAS_SNAPSHOT": NAS, "ORIGINAL_R2_SNAPSHOT": R2}, clear=True):
            for app in APPS:
                with self.subTest(app=app):
                    self.assertEqual(selector()(app), {"nas": NAS, "r2": R2})
            with self.assertRaises(ValueError):
                selector()("sonarr")

    def test_missing_invalid_or_identical_points_rejected(self):
        for nas, offsite in ((NAS, ""), ("", R2), (NAS.upper(), R2), (NAS[:-1], R2), (NAS, NAS)):
            with patch.dict(os.environ, {"ORIGINAL_NAS_SNAPSHOT": nas, "ORIGINAL_R2_SNAPSHOT": offsite}, clear=True):
                for app in APPS:
                    with self.subTest(app=app, nas=nas, offsite=offsite):
                        with self.assertRaises(ValueError):
                            selector()(app)

    def test_legacy_defaults_preserved_and_whisparr_requires_points(self):
        with patch.dict(os.environ, {}, clear=True):
            for app in ("bazarr", "radarr", "radarr-3d", "sonarr"):
                self.assertEqual(selector()(app), {"nas": "c" * 32, "r2": "d" * 32})
            with self.assertRaises(ValueError):
                selector()("whisparr")

    def test_actual_bash_dispatch_guard(self):
        text = WORKFLOW.read_text()
        start = text.index("        if [[ $SUITE == postgres-whisparr-original")
        end = text.index("        [[ $COMMIT =~", start)
        guard = "set -euo pipefail\n" + text[start:end]
        suites = tuple("postgres-" + app + "-original" for app in APPS)
        cases = ((NAS, R2, True), (NAS, "", False), ("", R2, False),
                 (NAS.upper(), R2, False), (NAS, NAS, False))
        for suite in suites + ("postgres-sonarr-original", "media-native"):
            for nas, offsite, valid in cases + (("", "", suite != "postgres-whisparr-original"),):
                expected = valid and (suite in suites or not nas and not offsite)
                env = dict(os.environ, SUITE=suite, NAS_SNAPSHOT=nas, R2_SNAPSHOT=offsite)
                result = subprocess.run(["bash", "-c", guard], env=env, capture_output=True)
                with self.subTest(suite=suite, nas=nas, offsite=offsite):
                    self.assertEqual(result.returncode == 0, expected)


if __name__ == "__main__":
    unittest.main()
