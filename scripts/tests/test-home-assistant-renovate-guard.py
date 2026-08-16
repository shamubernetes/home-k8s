#!/usr/bin/env python3
from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

RULE_DESCRIPTION = "Home Assistant core uses the rehearsed upgrade workflow"
TARGET_FILE = "kubernetes/apps/home-assistant/home-assistant/app/helmrelease.yaml"
BLOCKED_PACKAGES = {"ghcr.io/home-operations/home-assistant"}


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


class HomeAssistantRenovateGuardTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.repo_root = Path(__file__).resolve().parents[2]
        cls.rules = load_json5(
            cls.repo_root / ".github" / "renovate" / "packageRules.json5"
        )["packageRules"]
        cls.manifest = (cls.repo_root / TARGET_FILE).read_text()

    def test_guard_is_exact_and_disabled(self) -> None:
        matching = [
            rule for rule in self.rules if rule.get("description") == RULE_DESCRIPTION
        ]
        self.assertEqual(len(matching), 1, "expected exactly one HA core guard")
        rule = matching[0]
        self.assertIs(rule.get("enabled"), False)
        self.assertEqual(rule.get("matchFileNames"), [TARGET_FILE])
        self.assertEqual(set(rule.get("matchPackageNames", [])), BLOCKED_PACKAGES)

    def test_guarded_dependencies_exist_in_target_manifest(self) -> None:
        for package in BLOCKED_PACKAGES:
            self.assertIn(package, self.manifest)
        self.assertIn(
            "# renovate: datasource=github-releases depName=hacs/integration",
            self.manifest,
        )
        self.assertRegex(
            self.manifest,
            r"(?m)^\s*HACS_VERSION:\s*\d+\.\d+\.\d+$",
        )

    def test_hacs_is_an_install_if_missing_bootstrap(self) -> None:
        manifest_guard = 'if (hacs_dir / "manifest.json").is_file():'
        cleanup = "shutil.rmtree(hacs_dir)"
        self.assertIn("bootstrap-hacs:", self.manifest)
        self.assertIn("enabled: true", self.manifest)
        self.assertIn(manifest_guard, self.manifest)
        self.assertIn("preserving the Home Assistant-managed version", self.manifest)
        self.assertIn("Downloaded HACS archive has no manifest.json", self.manifest)
        self.assertIn("urllib.request.urlopen(url, timeout=60)", self.manifest)
        self.assertLess(self.manifest.index(manifest_guard), self.manifest.index(cleanup))

    def test_hacs_bootstrap_baseline_is_renovated_normally(self) -> None:
        matching = [
            rule for rule in self.rules if rule.get("description") == RULE_DESCRIPTION
        ]
        self.assertNotIn("hacs/integration", matching[0]["matchPackageNames"])

    def test_vscode_identity_is_generated_without_an_editor_sidecar(self) -> None:
        self.assertIn("init-vscode-identity:", self.manifest)
        self.assertIn('uid = "568"', self.manifest)
        self.assertIn('gid = "568"', self.manifest)
        self.assertIn(
            'homeassistant:x:568:568:Home Assistant:/config:/bin/bash',
            self.manifest,
        )
        self.assertIn('homeassistant:x:568:', self.manifest)
        self.assertIn('Path("/etc/passwd").read_text()', self.manifest)
        self.assertIn('Path("/etc/group").read_text()', self.manifest)
        self.assertIn("vscode-identity:", self.manifest)
        self.assertIn("path: /etc/passwd", self.manifest)
        self.assertIn("path: /etc/group", self.manifest)
        self.assertRegex(
            self.manifest,
            r"(?m)^\s*code-server:\n\s*enabled: false$",
        )

    def test_unrelated_dependencies_are_not_disabled(self) -> None:
        matching = [
            rule for rule in self.rules if rule.get("description") == RULE_DESCRIPTION
        ]
        self.assertEqual(len(matching), 1, "expected exactly one HA core guard")
        blocked = set(matching[0].get("matchPackageNames", []))
        for package in {
            "ghcr.io/home-operations/postgres-init",
            "ghcr.io/coder/code-server",
        }:
            self.assertNotIn(package, blocked)
            self.assertIn(package, self.manifest)


if __name__ == "__main__":
    unittest.main()
