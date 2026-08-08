#!/usr/bin/env python3
from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

CEPH_PACKAGE = "quay.io/ceph/ceph"
CEPH_ALLOWED_VERSIONS = "<20.2.3 || >=20.2.4 <21"


def load_json5(path: Path) -> dict:
    """Parse the limited JSON5 syntax used by Renovate with stdlib only."""
    text = path.read_text()
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    text = re.sub(r"(^|\s)//.*$", r"\1", text, flags=re.MULTILINE)
    text = re.sub(
        r'(?m)([,{]\s*)([A-Za-z_$][A-Za-z0-9_$]*)(\s*:)',
        r'\1"\2"\3',
        text,
    )
    text = re.sub(r",(\s*[}\]])", r"\1", text)
    return json.loads(text)


class RenovateCephGuardTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        repo_root = Path(__file__).resolve().parents[2]
        cls.rules = load_json5(
            repo_root / ".github" / "renovate" / "packageRules.json5"
        )["packageRules"]

    def test_ceph_guard_skips_only_the_broken_patch_within_major_20(self) -> None:
        matching = [
            rule
            for rule in self.rules
            if CEPH_PACKAGE in rule.get("matchPackageNames", [])
            and "allowedVersions" in rule
        ]
        self.assertEqual(len(matching), 1, "expected exactly one Ceph version guard")
        rule = matching[0]
        self.assertEqual(rule.get("matchDatasources"), ["docker"])
        self.assertEqual(rule.get("allowedVersions"), CEPH_ALLOWED_VERSIONS)

    def test_ceph_guard_is_not_a_permanent_pre_20_2_3_ceiling(self) -> None:
        matching = [
            rule
            for rule in self.rules
            if CEPH_PACKAGE in rule.get("matchPackageNames", [])
            and "allowedVersions" in rule
        ]
        self.assertNotEqual(matching[0].get("allowedVersions"), "<20.2.3")
        self.assertIn(">=20.2.4", matching[0]["allowedVersions"])
        self.assertIn("<21", matching[0]["allowedVersions"])


if __name__ == "__main__":
    unittest.main()
