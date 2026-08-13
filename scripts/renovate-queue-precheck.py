#!/usr/bin/env python3
"""Return a bounded Renovate queue hint without creating CI work."""

from __future__ import annotations

import json
import subprocess
import sys
import time
from typing import Any

REPOSITORY = "shamubernetes/home-k8s"
MIN_CORE_REMAINING = 1500
MAX_ACTIVE_RUNS = 12


def gh_json(*args: str) -> Any:
    result = subprocess.run(
        ["gh", *args],
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )
    return json.loads(result.stdout)


def checks_green(rollup: list[dict[str, object]]) -> bool:
    if not rollup:
        return False

    for check in rollup:
        kind = check.get("__typename")
        if kind == "CheckRun":
            if check.get("status") != "COMPLETED":
                return False
            if check.get("conclusion") not in {"SUCCESS", "NEUTRAL", "SKIPPED"}:
                return False
        elif kind == "StatusContext":
            if check.get("state") != "SUCCESS":
                return False
        else:
            return False
    return True


def main() -> int:
    rate_limit = gh_json("api", "rate_limit")
    core = rate_limit["resources"]["core"]
    workflow_runs = [
        *gh_json(
            "run",
            "list",
            "--repo",
            REPOSITORY,
            "--status",
            "in_progress",
            "--limit",
            "100",
            "--json",
            "databaseId",
        ),
        *gh_json(
            "run",
            "list",
            "--repo",
            REPOSITORY,
            "--status",
            "queued",
            "--limit",
            "100",
            "--json",
            "databaseId",
        ),
    ]
    pull_requests = gh_json(
        "pr",
        "list",
        "--repo",
        REPOSITORY,
        "--state",
        "open",
        "--base",
        "main",
        "--author",
        "app/shamubot",
        "--limit",
        "100",
        "--json",
        "number,title,headRefOid,mergeable,mergeStateStatus,isDraft,updatedAt,statusCheckRollup",
    )

    candidates = [
        {
            "number": pr["number"],
            "title": pr["title"],
            "head_sha": pr["headRefOid"],
            "mergeable": pr["mergeable"],
            "merge_state": pr["mergeStateStatus"],
            "updated_at": pr["updatedAt"],
        }
        for pr in pull_requests
        if not pr["isDraft"]
        and pr["mergeable"] == "MERGEABLE"
        and pr["mergeStateStatus"] in {"CLEAN", "BEHIND"}
        and checks_green(pr["statusCheckRollup"])
    ]

    quota_ok = core["remaining"] >= MIN_CORE_REMAINING
    run_capacity_ok = len(workflow_runs) < MAX_ACTIVE_RUNS
    mutation_allowed = quota_ok and run_capacity_ok
    context = {
        "repository": REPOSITORY,
        "open_renovate_pr_count": len(pull_requests),
        "green_precheck_candidates": candidates,
        "github_core_limit": core["limit"],
        "github_core_remaining": core["remaining"],
        "github_core_reset_at": time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime(core["reset"])
        ),
        "active_or_queued_workflow_run_count": len(workflow_runs),
        "mutation_allowed": mutation_allowed,
        "mutation_blockers": [
            blocker
            for blocked, blocker in (
                (not quota_ok, "github-core-quota-reserve"),
                (not run_capacity_ok, "workflow-run-capacity"),
            )
            if blocked
        ],
        "operator_contract": (
            "Read-only inventory is always allowed. Before every branch update, rerun, "
            "push, or merge, recheck quota and active runs. Never mutate more than one "
            "Renovate PR at a time. Never create no-op retrigger commits or queue-wide "
            "refresh waves. A mergeable BEHIND PR remains eligible and must not be "
            "updated solely for freshness. After a head mutation, wait for that exact "
            "head's checks to settle before another head mutation."
        ),
    }

    if not pull_requests:
        print(json.dumps({"wakeAgent": False, "context": context}, separators=(",", ":")))
        return 0

    print(json.dumps({"wakeAgent": True, "context": context}, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (json.JSONDecodeError, KeyError, OSError, subprocess.SubprocessError) as exc:
        print(f"Renovate queue precheck failed: {exc}", file=sys.stderr)
        raise SystemExit(1)