#!/usr/bin/env python3
from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

COMMIT_MESSAGES_PRESET = (
    "github>shamubernetes/home-k8s//.github/renovate/commitmessages.json5"
)
PACKAGE_RULES_PRESET = (
    "github>shamubernetes/home-k8s//.github/renovate/packageRules.json5"
)


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


class RenovateGroupPrecedenceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.repo_root = Path(__file__).resolve().parents[2]
        cls.config = load_json5(cls.repo_root / ".github" / "renovate.json5")
        cls.rules = load_json5(
            cls.repo_root / ".github" / "renovate" / "packageRules.json5"
        )["packageRules"]

    def test_groups_load_after_generic_commit_message_rules(self) -> None:
        extends = self.config["extends"]
        self.assertLess(
            extends.index(COMMIT_MESSAGES_PRESET),
            extends.index(PACKAGE_RULES_PRESET),
            "generic manager scopes must not override grouped PR scopes",
        )

    def test_every_explicit_group_uses_group_scope(self) -> None:
        grouped_rules = [rule for rule in self.rules if "groupName" in rule]
        self.assertTrue(grouped_rules, "expected explicit Renovate groups")
        for rule in grouped_rules:
            with self.subTest(group=rule["groupName"]):
                self.assertEqual(rule.get("semanticCommitScope"), "group")


if __name__ == "__main__":
    unittest.main()
