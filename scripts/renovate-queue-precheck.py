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
MIN_GRAPHQL_REMAINING = 1500
MAX_ACTIVE_RUNS = 12
WORKFLOW_RUN_LOOKBACK_SECONDS = 24 * 60 * 60
WORKFLOW_RUN_SNAPSHOT_LIMIT = 1000
WORKFLOW_RUN_STATUSES = {
    "completed",
    "in_progress",
    "pending",
    "queued",
    "requested",
    "waiting",
}
ACTIVE_WORKFLOW_RUN_STATUSES = WORKFLOW_RUN_STATUSES - {"completed"}
CHECK_RUN_STATUSES = {"completed", "in_progress", "pending", "queued", "requested", "waiting"}
CHECK_RUN_CONCLUSIONS = {
    "action_required",
    "cancelled",
    "failure",
    "neutral",
    "skipped",
    "stale",
    "startup_failure",
    "success",
    "timed_out",
}


def gh_json(*args: str) -> Any:
    result = subprocess.run(
        ["gh", *args],
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )
    return json.loads(result.stdout)


def checks_green(
    check_runs: dict[str, object],
    required_checks: set[tuple[str, int]],
    expected_head_sha: str,
) -> bool:
    runs = check_runs.get("check_runs")
    if not isinstance(runs, list) or not runs:
        return False

    total_count = check_runs.get("total_count")
    if type(total_count) is not int or total_count != len(runs):
        return False

    successful_checks: set[tuple[str, int]] = set()
    for check in runs:
        if not isinstance(check, dict):
            return False
        name = check.get("name")
        app = check.get("app")
        app_id = app.get("id") if isinstance(app, dict) else None
        status = check.get("status")
        conclusion = check.get("conclusion")
        if (
            not isinstance(name, str)
            or not name
            or type(app_id) is not int
            or status not in CHECK_RUN_STATUSES
            or (
                status == "completed"
                and (
                    not isinstance(conclusion, str)
                    or conclusion not in CHECK_RUN_CONCLUSIONS
                )
            )
            or (status != "completed" and conclusion is not None)
            or check.get("head_sha") != expected_head_sha
        ):
            return False
        if (
            status == "completed"
            and conclusion in {"success", "neutral", "skipped"}
        ):
            successful_checks.add((name, app_id))
    return bool(required_checks) and required_checks <= successful_checks


def workflow_runs_valid(
    runs: object, *, expected_status: str | None = None
) -> bool:
    if not isinstance(runs, list):
        return False
    seen_ids: set[int] = set()
    for run in runs:
        if not isinstance(run, dict):
            return False
        database_id = run.get("databaseId")
        status = run.get("status")
        if (
            type(database_id) is not int
            or database_id in seen_ids
            or status not in WORKFLOW_RUN_STATUSES
            or (expected_status is not None and status != expected_status)
        ):
            return False
        seen_ids.add(database_id)
    return True


def main() -> int:
    protection = gh_json(
        "api",
        f"repos/{REPOSITORY}/branches/main/protection/required_status_checks",
    )
    configured_checks = protection.get("checks") if isinstance(protection, dict) else None
    if not isinstance(configured_checks, list):
        configured_checks = []
    required_checks = {
        (check["context"], check["app_id"])
        for check in configured_checks
        if isinstance(check, dict)
        and isinstance(check.get("context"), str)
        and type(check.get("app_id")) is int
        and check["app_id"] > 0
    }
    required_check_identity_complete = (
        bool(configured_checks) and len(required_checks) == len(configured_checks)
    )
    workflow_run_cutoff = time.strftime(
        "%Y-%m-%dT%H:%M:%SZ",
        time.gmtime(time.time() - WORKFLOW_RUN_LOOKBACK_SECONDS),
    )
    workflow_run_snapshot = gh_json(
        "run",
        "list",
        "--repo",
        REPOSITORY,
        "--created",
        f">={workflow_run_cutoff}",
        "--limit",
        str(WORKFLOW_RUN_SNAPSHOT_LIMIT),
        "--json",
        "databaseId,status",
    )
    workflow_run_snapshot_valid = workflow_runs_valid(workflow_run_snapshot)
    workflow_run_snapshot_complete = workflow_run_snapshot_valid and (
        len(workflow_run_snapshot) < WORKFLOW_RUN_SNAPSHOT_LIMIT
    )
    recent_workflow_runs = [
        run
        for run in (
            workflow_run_snapshot if isinstance(workflow_run_snapshot, list) else []
        )
        if isinstance(run, dict)
        and run.get("status") in ACTIVE_WORKFLOW_RUN_STATUSES
    ]
    # Self-hosted jobs can execute for up to five days. Cover older running
    # work separately; the fixed creation-time ranges do not overlap, so a
    # queued-to-running transition cannot disappear between the queries.
    older_in_progress_runs = gh_json(
        "run",
        "list",
        "--repo",
        REPOSITORY,
        "--created",
        f"<{workflow_run_cutoff}",
        "--status",
        "in_progress",
        "--limit",
        str(MAX_ACTIVE_RUNS),
        "--json",
        "databaseId,status",
    )
    older_in_progress_runs_valid = workflow_runs_valid(
        older_in_progress_runs, expected_status="in_progress"
    )
    if not isinstance(older_in_progress_runs, list):
        older_in_progress_runs = []
    workflow_runs = [*recent_workflow_runs, *older_in_progress_runs]
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
        "number,title,headRefOid,mergeable,mergeStateStatus,isDraft,updatedAt",
    )
    candidates = []
    if required_check_identity_complete:
        for pr in pull_requests:
            if (
                pr["isDraft"]
                or pr["mergeable"] != "MERGEABLE"
                or pr["mergeStateStatus"] not in {"CLEAN", "BEHIND"}
            ):
                continue
            check_runs = gh_json(
                "api",
                f"repos/{REPOSITORY}/commits/{pr['headRefOid']}/check-runs?per_page=100",
            )
            if checks_green(check_runs, required_checks, pr["headRefOid"]):
                candidates.append(
                    {
                        "number": pr["number"],
                        "title": pr["title"],
                        "head_sha": pr["headRefOid"],
                        "mergeable": pr["mergeable"],
                        "merge_state": pr["mergeStateStatus"],
                        "updated_at": pr["updatedAt"],
                    }
                )
    # Sample quota after discovery so the mutation decision reflects every API
    # request the precheck itself consumed, including GraphQL PR inventory.
    rate_limit = gh_json("api", "rate_limit")
    core = rate_limit["resources"]["core"]
    graphql = rate_limit["resources"]["graphql"]

    quota_ok = core["remaining"] >= MIN_CORE_REMAINING
    graphql_quota_ok = graphql["remaining"] >= MIN_GRAPHQL_REMAINING
    run_capacity_ok = len(workflow_runs) < MAX_ACTIVE_RUNS
    mutation_allowed = (
        quota_ok
        and graphql_quota_ok
        and run_capacity_ok
        and workflow_run_snapshot_valid
        and workflow_run_snapshot_complete
        and older_in_progress_runs_valid
        and required_check_identity_complete
    )
    context = {
        "repository": REPOSITORY,
        "open_renovate_pr_count": len(pull_requests),
        "green_precheck_candidates": candidates,
        "github_core_limit": core["limit"],
        "github_core_remaining": core["remaining"],
        "github_core_reset_at": time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime(core["reset"])
        ),
        "github_graphql_limit": graphql["limit"],
        "github_graphql_remaining": graphql["remaining"],
        "github_graphql_reset_at": time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime(graphql["reset"])
        ),
        "active_or_queued_workflow_run_count": len(workflow_runs),
        "mutation_allowed": mutation_allowed,
        "mutation_blockers": [
            blocker
            for blocked, blocker in (
                (not quota_ok, "github-core-quota-reserve"),
                (not graphql_quota_ok, "github-graphql-quota-reserve"),
                (not run_capacity_ok, "workflow-run-capacity"),
                (
                    not workflow_run_snapshot_valid
                    or not older_in_progress_runs_valid,
                    "workflow-run-snapshot-malformed",
                ),
                (
                    not workflow_run_snapshot_complete,
                    "workflow-run-snapshot-truncated",
                ),
                (
                    not required_check_identity_complete,
                    "required-check-identity-incomplete",
                ),
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
    except (
        json.JSONDecodeError,
        KeyError,
        OSError,
        subprocess.SubprocessError,
        TypeError,
        ValueError,
    ) as exc:
        print(f"Renovate queue precheck failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
