#!/usr/bin/env python3

from __future__ import annotations

import importlib.util
import json
import pathlib
import unittest
from typing import Any
from unittest import mock

SCRIPT = pathlib.Path(__file__).parents[1] / "renovate-queue-precheck.py"
SPEC = importlib.util.spec_from_file_location("renovate_queue_precheck", SCRIPT)
assert SPEC is not None
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


class RenovateQueuePrecheckTests(unittest.TestCase):
    REQUIRED_CHECKS = {
        "Flate - Success",
        "Static Analysis - Success",
        "Talos - Validate",
        "Talos Image Availability",
    }

    def run_main(self, *, remaining: int, active_runs: int) -> dict[str, Any]:
        responses = [
            {
                "resources": {
                    "core": {"limit": 5000, "remaining": remaining, "reset": 1786632277}
                }
            },
            {"checks": [{"context": name} for name in self.REQUIRED_CHECKS]},
            [{"databaseId": number} for number in range(active_runs)],
            [],
            [
                {
                    "number": 42,
                    "title": "Update dependency",
                    "headRefOid": "a" * 40,
                    "mergeable": "MERGEABLE",
                    "mergeStateStatus": "BEHIND",
                    "isDraft": False,
                    "updatedAt": "2026-08-13T00:00:00Z",
                    "statusCheckRollup": [
                        *[
                            {
                                "__typename": "CheckRun",
                                "name": name,
                                "status": "COMPLETED",
                                "conclusion": "SUCCESS",
                            }
                            for name in self.REQUIRED_CHECKS
                        ]
                    ],
                }
            ],
        ]
        with mock.patch.object(MODULE, "gh_json", side_effect=responses), mock.patch(
            "builtins.print"
        ) as output:
            self.assertEqual(MODULE.main(), 0)
        return json.loads(output.call_args.args[0])

    def test_low_quota_blocks_mutation_but_keeps_read_only_wakeup(self) -> None:
        result = self.run_main(remaining=1499, active_runs=0)
        self.assertTrue(result["wakeAgent"])
        self.assertFalse(result["context"]["mutation_allowed"])
        self.assertEqual(
            result["context"]["mutation_blockers"], ["github-core-quota-reserve"]
        )

    def test_active_run_pressure_blocks_mutation(self) -> None:
        result = self.run_main(remaining=5000, active_runs=12)
        self.assertFalse(result["context"]["mutation_allowed"])
        self.assertEqual(result["context"]["mutation_blockers"], ["workflow-run-capacity"])

    def test_queued_run_pressure_blocks_mutation(self) -> None:
        responses = [
            {
                "resources": {
                    "core": {"limit": 5000, "remaining": 5000, "reset": 1786632277}
                }
            },
            {"checks": [{"context": name} for name in self.REQUIRED_CHECKS]},
            [],
            [{"databaseId": number} for number in range(12)],
            [],
        ]
        with mock.patch.object(MODULE, "gh_json", side_effect=responses), mock.patch(
            "builtins.print"
        ) as output:
            self.assertEqual(MODULE.main(), 0)
        result = json.loads(output.call_args.args[0])
        self.assertFalse(result["context"]["mutation_allowed"])
        self.assertEqual(result["context"]["mutation_blockers"], ["workflow-run-capacity"])

    def test_behind_green_candidate_does_not_require_refresh(self) -> None:
        result = self.run_main(remaining=5000, active_runs=0)
        self.assertTrue(result["context"]["mutation_allowed"])
        self.assertEqual(result["context"]["green_precheck_candidates"][0]["number"], 42)
        self.assertIn("Never mutate more than one", result["context"]["operator_contract"])

    def test_missing_required_check_is_not_green(self) -> None:
        rollup = [
            {
                "__typename": "CheckRun",
                "name": name,
                "status": "COMPLETED",
                "conclusion": "SUCCESS",
            }
            for name in self.REQUIRED_CHECKS - {"Static Analysis - Success"}
        ]
        self.assertFalse(MODULE.checks_green(rollup, self.REQUIRED_CHECKS))


if __name__ == "__main__":
    unittest.main()