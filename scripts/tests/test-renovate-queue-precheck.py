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
    APP_ID = 15368
    REQUIRED_CHECK_NAMES = {
        "Flate - Success",
        "Static Analysis - Success",
        "Talos - Validate",
        "Talos Image Availability",
    }
    REQUIRED_CHECKS = {(name, 15368) for name in REQUIRED_CHECK_NAMES}

    @classmethod
    def successful_check_runs(
        cls, *, app_id: int | None = None, head_sha: str = "a" * 40
    ) -> dict[str, Any]:
        runs = [
            {
                "name": name,
                "status": "completed",
                "conclusion": "success",
                "app": {"id": cls.APP_ID if app_id is None else app_id},
                "head_sha": head_sha,
            }
            for name in cls.REQUIRED_CHECK_NAMES
        ]
        return {"total_count": len(runs), "check_runs": runs}

    def run_main(
        self,
        *,
        core_remaining: int,
        graphql_remaining: int = 5000,
        active_runs: int,
        active_status: str = "in_progress",
        older_active_runs: int = 0,
        configured_app_id: int | None = 15368,
        check_runs: dict[str, Any] | None = None,
        recent_snapshot: object | None = None,
        older_snapshot: object | None = None,
    ) -> dict[str, Any]:
        protection_checks = [
            {"context": name, "app_id": configured_app_id}
            for name in self.REQUIRED_CHECK_NAMES
        ]
        responses: list[Any] = [
            {"checks": protection_checks},
            recent_snapshot
            if recent_snapshot is not None
            else [
                {"databaseId": number, "status": active_status}
                for number in range(active_runs)
            ],
            older_snapshot
            if older_snapshot is not None
            else [
                {"databaseId": 10_000 + number, "status": "in_progress"}
                for number in range(older_active_runs)
            ],
            [
                {
                    "number": 42,
                    "title": "Update dependency",
                    "headRefOid": "a" * 40,
                    "mergeable": "MERGEABLE",
                    "mergeStateStatus": "BEHIND",
                    "isDraft": False,
                    "updatedAt": "2026-08-13T00:00:00Z",
                }
            ],
        ]
        if configured_app_id is not None and configured_app_id > 0:
            responses.append(check_runs or self.successful_check_runs())
        responses.append(
            {
                "resources": {
                    "core": {
                        "limit": 5000,
                        "remaining": core_remaining,
                        "reset": 1786632277,
                    },
                    "graphql": {
                        "limit": 5000,
                        "remaining": graphql_remaining,
                        "reset": 1786632277,
                    },
                }
            }
        )
        with mock.patch.object(
            MODULE, "gh_json", side_effect=responses
        ) as gh_json, mock.patch("builtins.print") as output:
            self.assertEqual(MODULE.main(), 0)
        calls = gh_json.call_args_list
        self.assertEqual(
            calls[0],
            mock.call(
                "api",
                f"repos/{MODULE.REPOSITORY}/branches/main/protection/required_status_checks",
            ),
        )
        recent_args = calls[1].args
        self.assertEqual(recent_args[:4], ("run", "list", "--repo", MODULE.REPOSITORY))
        self.assertEqual(recent_args[4], "--created")
        self.assertRegex(recent_args[5], r"^>=\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
        self.assertEqual(
            recent_args[6:],
            (
                "--limit",
                str(MODULE.WORKFLOW_RUN_SNAPSHOT_LIMIT),
                "--json",
                "databaseId,status",
            ),
        )
        older_args = calls[2].args
        self.assertEqual(older_args[:4], ("run", "list", "--repo", MODULE.REPOSITORY))
        self.assertEqual(older_args[4], "--created")
        self.assertRegex(older_args[5], r"^<\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
        self.assertEqual(older_args[5][1:], recent_args[5][2:])
        self.assertEqual(
            older_args[6:],
            (
                "--status",
                "in_progress",
                "--limit",
                str(MODULE.MAX_ACTIVE_RUNS),
                "--json",
                "databaseId,status",
            ),
        )
        self.assertEqual(
            calls[3],
            mock.call(
                "pr",
                "list",
                "--repo",
                MODULE.REPOSITORY,
                "--state",
                "open",
                "--base",
                "main",
                "--author",
                "app/shamubot",
                "--limit",
                "100",
                "--json",
                "number,title,headRefOid,mergeable,mergeStateStatus,isDraft,updatedAt",
            ),
        )
        expected_call_count = 5
        if configured_app_id is not None and configured_app_id > 0:
            expected_call_count = 6
            self.assertEqual(
                calls[4],
                mock.call(
                    "api",
                    f"repos/{MODULE.REPOSITORY}/commits/{'a' * 40}/check-runs?per_page=100",
                ),
            )
        self.assertEqual(len(calls), expected_call_count)
        self.assertEqual(calls[-1], mock.call("api", "rate_limit"))
        return json.loads(output.call_args.args[0])

    def test_low_quota_blocks_mutation_but_keeps_read_only_wakeup(self) -> None:
        result = self.run_main(core_remaining=1499, active_runs=0)
        self.assertTrue(result["wakeAgent"])
        self.assertFalse(result["context"]["mutation_allowed"])
        self.assertEqual(
            result["context"]["mutation_blockers"], ["github-core-quota-reserve"]
        )

    def test_active_run_pressure_blocks_mutation(self) -> None:
        result = self.run_main(core_remaining=5000, active_runs=12)
        self.assertFalse(result["context"]["mutation_allowed"])
        self.assertEqual(result["context"]["mutation_blockers"], ["workflow-run-capacity"])

    def test_queued_run_pressure_blocks_mutation_from_same_snapshot(self) -> None:
        result = self.run_main(
            core_remaining=5000, active_runs=12, active_status="queued"
        )
        self.assertFalse(result["context"]["mutation_allowed"])
        self.assertEqual(result["context"]["mutation_blockers"], ["workflow-run-capacity"])

    def test_every_nonterminal_status_counts_toward_capacity(self) -> None:
        for status in ("in_progress", "pending", "queued", "requested", "waiting"):
            with self.subTest(status=status):
                result = self.run_main(
                    core_remaining=5000,
                    active_runs=MODULE.MAX_ACTIVE_RUNS,
                    active_status=status,
                )
                self.assertFalse(result["context"]["mutation_allowed"])
                self.assertEqual(
                    result["context"]["active_or_queued_workflow_run_count"],
                    MODULE.MAX_ACTIVE_RUNS,
                )
                self.assertEqual(
                    result["context"]["mutation_blockers"],
                    ["workflow-run-capacity"],
                )

    def test_completed_runs_do_not_count_toward_capacity(self) -> None:
        result = self.run_main(
            core_remaining=5000,
            active_runs=MODULE.MAX_ACTIVE_RUNS,
            active_status="completed",
        )
        self.assertTrue(result["context"]["mutation_allowed"])
        self.assertEqual(result["context"]["active_or_queued_workflow_run_count"], 0)

    def test_older_in_progress_run_pressure_blocks_mutation(self) -> None:
        result = self.run_main(
            core_remaining=5000,
            active_runs=0,
            older_active_runs=MODULE.MAX_ACTIVE_RUNS,
        )
        self.assertFalse(result["context"]["mutation_allowed"])
        self.assertEqual(result["context"]["mutation_blockers"], ["workflow-run-capacity"])

    def test_truncated_workflow_snapshot_blocks_mutation(self) -> None:
        result = self.run_main(
            core_remaining=5000,
            active_runs=MODULE.WORKFLOW_RUN_SNAPSHOT_LIMIT,
            active_status="completed",
        )
        self.assertFalse(result["context"]["mutation_allowed"])
        self.assertEqual(
            result["context"]["mutation_blockers"],
            ["workflow-run-snapshot-truncated"],
        )

    def test_behind_green_candidate_does_not_require_refresh(self) -> None:
        result = self.run_main(core_remaining=5000, active_runs=0)
        self.assertTrue(result["context"]["mutation_allowed"])
        self.assertEqual(result["context"]["green_precheck_candidates"][0]["number"], 42)
        self.assertIn("Never mutate more than one", result["context"]["operator_contract"])

    def test_graphql_quota_reserve_blocks_mutation(self) -> None:
        result = self.run_main(
            core_remaining=5000, graphql_remaining=1499, active_runs=0
        )
        self.assertFalse(result["context"]["mutation_allowed"])
        self.assertEqual(
            result["context"]["mutation_blockers"],
            ["github-graphql-quota-reserve"],
        )

    def test_quota_threshold_is_inclusive(self) -> None:
        result = self.run_main(
            core_remaining=1500, graphql_remaining=1500, active_runs=0
        )
        self.assertTrue(result["context"]["mutation_allowed"])

    def test_missing_required_check_is_not_green(self) -> None:
        payload = self.successful_check_runs()
        payload["check_runs"] = payload["check_runs"][:-1]
        payload["total_count"] -= 1
        self.assertFalse(MODULE.checks_green(payload, self.REQUIRED_CHECKS, "a" * 40))

    def test_same_named_checks_from_wrong_app_are_not_green(self) -> None:
        self.assertFalse(
            MODULE.checks_green(
                self.successful_check_runs(app_id=99999),
                self.REQUIRED_CHECKS,
                "a" * 40,
            )
        )

    def test_incomplete_check_run_page_is_not_green(self) -> None:
        payload = self.successful_check_runs()
        payload["total_count"] += 1
        self.assertFalse(MODULE.checks_green(payload, self.REQUIRED_CHECKS, "a" * 40))

    def test_check_run_count_smaller_than_page_is_not_green(self) -> None:
        payload = self.successful_check_runs()
        payload["total_count"] -= 1
        self.assertFalse(MODULE.checks_green(payload, self.REQUIRED_CHECKS, "a" * 40))

    def test_malformed_check_run_is_not_green(self) -> None:
        payload = self.successful_check_runs()
        payload["check_runs"].append({"name": "optional"})
        payload["total_count"] += 1
        self.assertFalse(MODULE.checks_green(payload, self.REQUIRED_CHECKS, "a" * 40))

    def test_malformed_check_run_status_is_not_green(self) -> None:
        payload = self.successful_check_runs()
        payload["check_runs"].append(
            {
                "name": "optional",
                "status": 123,
                "conclusion": None,
                "app": {"id": self.APP_ID},
                "head_sha": "a" * 40,
            }
        )
        payload["total_count"] += 1
        self.assertFalse(MODULE.checks_green(payload, self.REQUIRED_CHECKS, "a" * 40))

    def test_malformed_check_run_conclusion_is_not_green(self) -> None:
        payload = self.successful_check_runs()
        payload["check_runs"].append(
            {
                "name": "optional",
                "status": "completed",
                "conclusion": {},
                "app": {"id": self.APP_ID},
                "head_sha": "a" * 40,
            }
        )
        payload["total_count"] += 1
        self.assertFalse(MODULE.checks_green(payload, self.REQUIRED_CHECKS, "a" * 40))

    def test_noncompleted_check_run_requires_null_conclusion(self) -> None:
        payload = self.successful_check_runs()
        payload["check_runs"].append(
            {
                "name": "optional",
                "status": "in_progress",
                "conclusion": "success",
                "app": {"id": self.APP_ID},
                "head_sha": "a" * 40,
            }
        )
        payload["total_count"] += 1
        self.assertFalse(MODULE.checks_green(payload, self.REQUIRED_CHECKS, "a" * 40))

    def test_check_runs_from_other_head_are_not_green(self) -> None:
        self.assertFalse(
            MODULE.checks_green(
                self.successful_check_runs(head_sha="b" * 40),
                self.REQUIRED_CHECKS,
                "a" * 40,
            )
        )

    def test_malformed_recent_workflow_snapshot_blocks_mutation(self) -> None:
        result = self.run_main(
            core_remaining=5000,
            active_runs=0,
            recent_snapshot=[{"databaseId": 1, "status": "unexpected"}],
        )
        self.assertFalse(result["context"]["mutation_allowed"])
        self.assertIn(
            "workflow-run-snapshot-malformed", result["context"]["mutation_blockers"]
        )

    def test_malformed_older_workflow_snapshot_blocks_mutation(self) -> None:
        result = self.run_main(
            core_remaining=5000,
            active_runs=0,
            older_snapshot=[{"databaseId": 1, "status": "queued"}],
        )
        self.assertFalse(result["context"]["mutation_allowed"])
        self.assertIn(
            "workflow-run-snapshot-malformed", result["context"]["mutation_blockers"]
        )

    def test_unpinned_required_check_identity_blocks_mutation(self) -> None:
        result = self.run_main(
            core_remaining=5000,
            active_runs=0,
            configured_app_id=None,
        )
        self.assertFalse(result["context"]["mutation_allowed"])
        self.assertEqual(
            result["context"]["mutation_blockers"],
            ["required-check-identity-incomplete"],
        )
        self.assertEqual(result["context"]["green_precheck_candidates"], [])

    def test_wildcard_required_check_identity_blocks_mutation(self) -> None:
        result = self.run_main(
            core_remaining=5000,
            active_runs=0,
            configured_app_id=-1,
        )
        self.assertFalse(result["context"]["mutation_allowed"])
        self.assertEqual(
            result["context"]["mutation_blockers"],
            ["required-check-identity-incomplete"],
        )
        self.assertEqual(result["context"]["green_precheck_candidates"], [])

    def test_conclusion_value_is_not_a_valid_workflow_status(self) -> None:
        result = self.run_main(
            core_remaining=5000,
            active_runs=0,
            recent_snapshot=[{"databaseId": 1, "status": "success"}],
        )
        self.assertFalse(result["context"]["mutation_allowed"])
        self.assertIn(
            "workflow-run-snapshot-malformed", result["context"]["mutation_blockers"]
        )


if __name__ == "__main__":
    unittest.main()
