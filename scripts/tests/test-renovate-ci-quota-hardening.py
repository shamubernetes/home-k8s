#!/usr/bin/env python3

from __future__ import annotations

import pathlib
import re
import tomllib
import unittest

ROOT = pathlib.Path(__file__).parents[2]
WORKFLOWS = ROOT / ".github" / "workflows"


class RenovateCIQuotaHardeningTests(unittest.TestCase):
    def test_pr_workflows_cancel_superseded_runs(self) -> None:
        for workflow in sorted(WORKFLOWS.glob("*.yaml")):
            text = workflow.read_text()
            if not any(
                trigger in text
                for trigger in ("pull_request:", "pull_request_target:", "workflow_run:")
            ):
                continue
            with self.subTest(workflow=workflow.name):
                self.assertIn("concurrency:", text)
                self.assertIn("cancel-in-progress: true", text)

    def test_workflow_run_uses_stable_source_branch_concurrency(self) -> None:
        text = (WORKFLOWS / "image-pull.yaml").read_text()
        concurrency = re.search(r"concurrency:\n(?P<block>(?:  .*\n)+)", text)
        self.assertIsNotNone(concurrency)
        assert concurrency is not None
        block = concurrency.group("block")
        self.assertIn("github.event.workflow_run.head_repository.full_name", block)
        self.assertIn("github.event.workflow_run.head_branch", block)
        self.assertNotIn("github.event.workflow_run.id", block)

    def test_mise_installs_use_cache_and_avoid_github_bootstrap(self) -> None:
        blocks = []
        for workflow in sorted(WORKFLOWS.glob("*.yaml")):
            text = workflow.read_text()
            blocks.extend(
                re.findall(
                    r"uses: jdx/mise-action@[^\n]+\n(?P<block>(?:\s{6,}.*\n)*)",
                    text,
                )
            )
        self.assertTrue(blocks)
        for block in blocks:
            with self.subTest(block=block):
                self.assertIn("cache: true", block)
                self.assertIn("fetch_from_github: false", block)

    def test_mise_lock_is_enforced(self) -> None:
        config = (ROOT / ".mise.toml").read_text()
        lockfile = (ROOT / "mise.lock").read_text()
        self.assertIn('MISE_LOCKED = "1"', config)
        self.assertIn('"platforms.linux-x64"', lockfile)
        self.assertIn('"platforms.macos-arm64"', lockfile)

    def test_yq_has_locked_platform_artifacts(self) -> None:
        config = (ROOT / ".mise.toml").read_text()
        lockfile = (ROOT / "mise.lock").read_text()
        version = tomllib.loads(config)["tools"]["yq"]
        self.assertRegex(version, r"^\d+\.\d+\.\d+$")
        self.assertNotIn("asdf-yq", config)
        entries = tomllib.loads(lockfile)["tools"]["yq"]
        self.assertEqual(len(entries), 1)
        entry = entries[0]
        self.assertEqual(entry["version"], version)
        self.assertEqual(entry["backend"], "aqua:mikefarah/yq")
        for platform, artifact in (
            ("linux-x64", "yq_linux_amd64"),
            ("macos-arm64", "yq_darwin_arm64"),
        ):
            with self.subTest(platform=platform):
                locked = entry[f"platforms.{platform}"]
                self.assertRegex(locked["checksum"], r"^sha256:[0-9a-f]{64}$")
                self.assertEqual(
                    locked["url"],
                    f"https://github.com/mikefarah/yq/releases/download/v{version}/{artifact}",
                )
                self.assertEqual(locked["provenance"], "cosign")

if __name__ == "__main__":
    unittest.main()