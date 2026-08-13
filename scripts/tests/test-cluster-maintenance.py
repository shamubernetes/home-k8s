#!/usr/bin/env python3
from __future__ import annotations

import datetime as dt
import importlib.machinery
import io
import json
import os
import re
import subprocess
import tempfile
import threading
import types
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

UTC = dt.timezone.utc
MANAGED_BY = "home-k8s cluster-maintenance"
COMMENT_PREFIX = "cluster-maintenance: "


def timestamp(value):
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


class FakeAlertmanager:
    def __init__(self):
        self.cluster_status = "ready"
        self.peers = [{"name": "am-0"}, {"name": "am-1"}]
        self.alerts = []
        self.silences = {}
        self.posts = []
        self.deletes = []
        self.ignore_silence_application = False
        self.fail_alert_reads_after_post = False
        self.proxy_style_not_found: str | None = None
        self.mirror_source: FakeAlertmanager | None = None
        self.mirror_matchers: list[dict[str, object]] | None = None
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self.handler())
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self):
        host = self.server.server_address[0]
        port = self.server.server_address[1]
        return f"http://{host}:{port}"

    def close(self):
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.server_close()

    def add_alert(self, alertname, severity="warning", silenced_by=None, **labels):
        self.alerts.append(
            {
                "labels": {"alertname": alertname, "severity": severity, **labels},
                "status": {
                    "state": "suppressed" if silenced_by else "active",
                    "silencedBy": list(silenced_by or []),
                    "inhibitedBy": [],
                },
            }
        )

    def add_managed_silence(self, silence_id="maintenance-1", reason="test"):
        now = dt.datetime.now(UTC)
        self.silences[silence_id] = {
            "id": silence_id,
            "matchers": [
                {"name": "alertname", "value": ".+", "isRegex": True, "isEqual": True},
                {
                    "name": "alertname",
                    "value": "Watchdog|InfoInhibitor",
                    "isRegex": True,
                    "isEqual": False,
                },
            ],
            "startsAt": timestamp(now - dt.timedelta(minutes=1)),
            "endsAt": timestamp(now + dt.timedelta(hours=4)),
            "createdBy": MANAGED_BY,
            "comment": COMMENT_PREFIX + reason,
            "status": {"state": "active"},
        }
        return silence_id

    def apply_silence(self, silence):
        silence_id = silence["id"]
        now = dt.datetime.now(UTC)
        ends_at = dt.datetime.fromisoformat(silence["endsAt"].replace("Z", "+00:00"))
        active = ends_at > now
        silence["status"] = {"state": "active" if active else "expired"}
        for alert in self.alerts:
            covered = True
            for matcher in silence["matchers"]:
                candidate = alert["labels"].get(matcher["name"], "")
                if matcher["isRegex"]:
                    matched = re.fullmatch(matcher["value"], candidate) is not None
                else:
                    matched = candidate == matcher["value"]
                if not matcher.get("isEqual", True):
                    matched = not matched
                covered = covered and matched
            ids = set(alert["status"].get("silencedBy", []))
            if active and covered:
                ids.add(silence_id)
            else:
                ids.discard(silence_id)
            alert["status"]["silencedBy"] = sorted(ids)
            alert["status"]["state"] = "suppressed" if ids else "active"

    def handler(self):
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                del format, args
                return

            def send_json(self, status, payload):
                body = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if self.path == "/api/v2/status":
                    self.send_json(
                        200,
                        {
                            "cluster": {"status": owner.cluster_status, "peers": owner.peers},
                            "versionInfo": {"version": "test"},
                        },
                    )
                    return
                if self.path == "/api/v2/alerts":
                    if owner.fail_alert_reads_after_post and owner.posts:
                        self.send_json(500, {"message": "injected alert read failure"})
                        return
                    self.send_json(200, owner.alerts)
                    return
                if self.path == "/api/v2/silences":
                    self.send_json(200, list(owner.silences.values()))
                    return
                prefix = "/api/v2/silence/"
                if self.path.startswith(prefix):
                    silence = owner.silences.get(self.path[len(prefix) :])
                    if silence is None and owner.mirror_source is not None:
                        source = owner.mirror_source.silences.get(self.path[len(prefix) :])
                        if source is not None:
                            silence = json.loads(json.dumps(source))
                            if owner.mirror_matchers is not None:
                                silence["matchers"] = owner.mirror_matchers
                            owner.silences[silence["id"]] = silence
                    if silence is None:
                        if owner.proxy_style_not_found == "html":
                            body = b"<html>proxy not found</html>"
                            self.send_response(404)
                            self.send_header("Content-Type", "text/html")
                            self.send_header("Content-Length", str(len(body)))
                            self.end_headers()
                            self.wfile.write(body)
                        elif owner.proxy_style_not_found == "misleading-empty":
                            self.send_response(404)
                            self.send_header("Cache-Control", "x-no-store")
                            self.send_header("Content-Length", "0")
                            self.end_headers()
                        else:
                            self.send_response(404)
                            self.send_header("Cache-Control", "no-store")
                            self.send_header("Content-Length", "0")
                            self.end_headers()
                        return
                    self.send_json(200, silence)
                    return
                self.send_json(404, {"message": "not found"})

            def do_POST(self):
                if self.path != "/api/v2/silences":
                    self.send_json(404, {"message": "not found"})
                    return
                length = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(length))
                owner.posts.append(payload)
                silence_id = payload.get("id", "maintenance-1")
                payload["id"] = silence_id
                owner.silences[silence_id] = payload
                if not owner.ignore_silence_application:
                    owner.apply_silence(payload)
                else:
                    payload["status"] = {"state": "active"}
                self.send_json(200, {"silenceID": silence_id})

            def do_DELETE(self):
                prefix = "/api/v2/silence/"
                if not self.path.startswith(prefix):
                    self.send_json(404, {"message": "not found"})
                    return
                silence_id = self.path[len(prefix) :]
                silence = owner.silences.get(silence_id)
                if not silence:
                    self.send_json(404, {"message": "not found"})
                    return
                owner.deletes.append(silence_id)
                silence["endsAt"] = timestamp(dt.datetime.now(UTC))
                owner.apply_silence(silence)
                self.send_response(200)
                self.send_header("Content-Length", "0")
                self.end_headers()

        return Handler


class FakeStatusApi:
    def __init__(self):
        self.page_id = "page-1"
        self.notices = {}
        self.created = []
        self.resolved = []
        self.fail_creates = False
        self.fail_resolves = False
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self.handler())
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self):
        host = self.server.server_address[0]
        port = self.server.server_address[1]
        return f"http://{host}:{port}"

    def close(self):
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.server_close()

    def add_notice(self, notice_id="notice-1", message="Scheduled maintenance: test"):
        self.notices[notice_id] = {"id": notice_id, "message": message, "resolved": False}
        return notice_id

    def handler(self):
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                del format, args

            def send_json(self, status, payload):
                body = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if self.path == "/api/pages":
                    self.send_json(200, [{"id": owner.page_id, "slug": "thezoo"}])
                    return
                self.send_json(404, {"message": "not found"})

            def do_POST(self):
                if self.path != f"/api/pages/{owner.page_id}/notices":
                    self.send_json(404, {"message": "not found"})
                    return
                if owner.fail_creates:
                    self.send_json(500, {"message": "injected create failure"})
                    return
                length = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(length))
                notice_id = f"notice-{len(owner.notices) + 1}"
                notice = {"id": notice_id, **payload, "resolved": False}
                owner.notices[notice_id] = notice
                owner.created.append(notice_id)
                self.send_json(200, notice)

            def do_PUT(self):
                prefix = f"/api/pages/{owner.page_id}/notices/"
                suffix = "/resolve"
                if not self.path.startswith(prefix) or not self.path.endswith(suffix):
                    self.send_json(404, {"message": "not found"})
                    return
                if owner.fail_resolves:
                    self.send_json(500, {"message": "injected resolve failure"})
                    return
                notice_id = self.path[len(prefix) : -len(suffix)]
                notice = owner.notices.get(notice_id)
                if not notice:
                    self.send_json(404, {"message": "not found"})
                    return
                notice["resolved"] = True
                owner.resolved.append(notice_id)
                self.send_json(200, notice)

        return Handler


class ClusterMaintenanceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.repo_root = Path(__file__).resolve().parents[2]
        cls.command = cls.repo_root / "scripts" / "cluster-maintenance"
        loader = importlib.machinery.SourceFileLoader(
            "cluster_maintenance_under_test", str(cls.command)
        )
        cls.module = types.ModuleType(loader.name)
        loader.exec_module(cls.module)

    def setUp(self):
        self.alertmanager = FakeAlertmanager()
        self.status_api = FakeStatusApi()
        self.tmpdir = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.alertmanager.close()
        self.status_api.close()
        self.tmpdir.cleanup()

    def command_env(self, **overrides):
        env = os.environ.copy()
        env["ALERTMANAGER_URLS"] = f"{self.alertmanager.url},{self.alertmanager.url}"
        env.pop("ALERTMANAGER_URL", None)
        env["MAINTENANCE_LOCK_FILE"] = str(Path(self.tmpdir.name) / "begin.lock")
        env["MAINTENANCE_VERIFY_TIMEOUT_SECONDS"] = "1"
        env["MAINTENANCE_ALLOW_DUPLICATE_MEMBERS"] = "1"
        env["MAINTENANCE_STATE_FILE"] = str(Path(self.tmpdir.name) / "maintenance.json")
        env["STATUS_API_URL"] = self.status_api.url
        env["STATUS_API_KEY"] = "test-key"
        env.update(overrides)
        return env

    def seed_state(self, silence_id, notice_id="notice-1", allowed_alerts=None):
        self.status_api.add_notice(notice_id)
        state = {
            "silenceId": silence_id,
            "noticeId": notice_id,
            "reason": "test",
            "startedAt": timestamp(dt.datetime.now(UTC) - dt.timedelta(minutes=1)),
            "endsAt": timestamp(dt.datetime.now(UTC) + dt.timedelta(hours=4)),
            "allowedActiveAlerts": allowed_alerts or [],
        }
        Path(self.tmpdir.name, "maintenance.json").write_text(json.dumps(state))

    def raw_command(self, *args, env=None):
        return subprocess.run(
            [str(self.command), *args],
            cwd=self.repo_root,
            env=env or self.command_env(),
            text=True,
            capture_output=True,
            timeout=15,
        )

    def run_command(self, *args, expected=0, env=None):
        result = self.raw_command(*args, env=env)
        self.assertEqual(
            result.returncode,
            expected,
            msg=f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}",
        )
        return result

    def test_routine_begin_creates_only_a_verified_bounded_silence(self):
        self.alertmanager.add_alert("RoutineInfo", severity="info")
        self.alertmanager.add_alert("Watchdog", severity="none")
        result = self.run_command("begin", "--reason", "Renovate PR #123", "--duration", "4h")
        output = json.loads(result.stdout)

        self.assertEqual(output["state"], "active")
        self.assertEqual(output["silenceId"], "maintenance-1")
        self.assertIsNone(output["noticeId"])
        self.assertEqual(output["publicStatus"], "not-posted")
        self.assertEqual(output["coveredAlerts"], 1)
        self.assertEqual(output["excludedAlerts"], ["Watchdog"])
        self.assertEqual(len(self.alertmanager.posts), 1)
        state = json.loads(Path(self.tmpdir.name, "maintenance.json").read_text())
        self.assertEqual(state["silenceId"], "maintenance-1")
        self.assertIsNone(state["noticeId"])
        self.assertEqual(self.status_api.notices, {})

        posted = self.alertmanager.posts[0]
        self.assertEqual(posted["createdBy"], MANAGED_BY)
        self.assertEqual(posted["comment"], COMMENT_PREFIX + "Renovate PR #123")
        self.assertEqual(
            posted["matchers"],
            [
                {"name": "alertname", "value": ".+", "isRegex": True, "isEqual": True},
                {
                    "name": "alertname",
                    "value": "Watchdog|InfoInhibitor",
                    "isRegex": True,
                    "isEqual": False,
                },
            ],
        )
        routine, watchdog = self.alertmanager.alerts
        self.assertEqual(routine["status"]["silencedBy"], ["maintenance-1"])
        self.assertEqual(watchdog["status"]["silencedBy"], [])

    def test_begin_fails_closed_when_unrelated_critical_alert_is_active(self):
        self.alertmanager.add_alert("CephHealthError", severity="critical")
        result = self.run_command(
            "begin", "--reason", "routine maintenance", expected=1
        )
        self.assertIn("CephHealthError", result.stderr)
        self.assertEqual(self.alertmanager.posts, [])
        self.assertEqual(self.status_api.resolved, [])

    def test_public_begin_posts_an_identified_maintenance_incident(self):
        result = self.run_command(
            "begin", "--reason", "public service migration", "--public-status"
        )
        output = json.loads(result.stdout)
        self.assertEqual(output["noticeId"], "notice-1")
        self.assertEqual(output["publicStatus"], "posted")
        notice = self.status_api.notices["notice-1"]
        self.assertEqual(notice["status"], "identified")
        self.assertEqual(notice["severity"], "warning")

    def test_public_begin_cleans_up_notice_when_admission_fails(self):
        self.alertmanager.add_alert("CephHealthError", severity="critical")
        result = self.run_command(
            "begin",
            "--reason",
            "public service migration",
            "--public-status",
            expected=1,
        )
        self.assertIn("CephHealthError", result.stderr)
        self.assertEqual(self.alertmanager.posts, [])
        self.assertEqual(self.status_api.resolved, ["notice-1"])

    def test_begin_does_not_create_a_silence_when_public_notice_creation_fails(self):
        self.status_api.fail_creates = True
        result = self.run_command(
            "begin", "--reason", "public maintenance", "--public-status", expected=1
        )
        self.assertIn("status API", result.stderr)
        self.assertEqual(self.alertmanager.posts, [])
        self.assertFalse(Path(self.tmpdir.name, "maintenance.json").exists())

    def test_begin_allows_one_exact_known_baseline_alert(self):
        self.alertmanager.add_alert(
            "SmartDeviceInterfaceSlow",
            severity="critical",
            kubernetes_node="k8s-rhea",
            device="sda",
        )
        result = self.run_command(
            "begin",
            "--reason",
            "routine maintenance",
            "--allow-active-alert",
            "alertname=SmartDeviceInterfaceSlow,kubernetes_node=k8s-rhea,device=sda",
        )
        output = json.loads(result.stdout)
        self.assertEqual(output["state"], "active")
        self.assertEqual(
            output["allowedBaselineAlerts"],
            [
                {
                    "alertname": "SmartDeviceInterfaceSlow",
                    "severity": "critical",
                    "namespace": None,
                    "node": "k8s-rhea",
                    "device": "sda",
                }
            ],
        )

    def test_begin_rejects_alert_outside_exact_baseline_selector(self):
        self.alertmanager.add_alert(
            "SmartDeviceInterfaceSlow",
            severity="critical",
            kubernetes_node="k8s-coeus",
            device="sda",
        )
        result = self.run_command(
            "begin",
            "--reason",
            "routine maintenance",
            "--allow-active-alert",
            "alertname=SmartDeviceInterfaceSlow,kubernetes_node=k8s-rhea,device=sda",
            expected=1,
        )
        self.assertIn("SmartDeviceInterfaceSlow", result.stderr)
        self.assertEqual(self.alertmanager.posts, [])

    def test_begin_rejects_unscoped_baseline_selector(self):
        result = self.run_command(
            "begin",
            "--reason",
            "routine maintenance",
            "--allow-active-alert",
            "alertname=SmartDeviceInterfaceSlow",
            expected=2,
        )
        self.assertIn("at least one identifying", result.stderr)

    def test_begin_requires_healthy_two_peer_alertmanager_cluster(self):
        self.alertmanager.peers = [{"name": "am-0"}]
        result = self.run_command(
            "begin", "--reason", "routine maintenance", expected=1
        )
        self.assertIn("two ready peers", result.stderr)
        self.assertEqual(self.alertmanager.posts, [])

    def test_begin_rejects_an_overlapping_owned_window(self):
        self.alertmanager.add_managed_silence("existing-maintenance")
        result = self.run_command(
            "begin", "--reason", "routine maintenance", expected=1
        )
        self.assertIn("already active", result.stderr)
        self.assertEqual(self.alertmanager.posts, [])

    def test_concurrent_begin_calls_leave_exactly_one_active_window(self):
        args = ("begin", "--reason", "concurrent maintenance", "--duration", "4h")
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _: self.raw_command(*args), range(2)))
        self.assertEqual(sorted(result.returncode for result in results), [0, 1])
        active = [
            silence
            for silence in self.alertmanager.silences.values()
            if silence["status"]["state"] == "active"
        ]
        self.assertEqual(len(active), 1)

    def test_begin_expires_a_silence_when_coverage_verification_fails(self):
        self.alertmanager.add_alert("RoutineInfo", severity="info")
        self.alertmanager.ignore_silence_application = True
        result = self.run_command(
            "begin", "--reason", "routine maintenance", expected=1
        )
        self.assertIn("coverage verification failed", result.stderr)
        self.assertEqual(self.alertmanager.deletes, ["maintenance-1"])
        self.assertEqual(
            self.alertmanager.silences["maintenance-1"]["status"]["state"],
            "expired",
        )

    def test_renew_extends_only_a_managed_active_silence(self):
        silence_id = self.alertmanager.add_managed_silence()
        self.seed_state(silence_id)
        old_end = self.alertmanager.silences[silence_id]["endsAt"]
        result = self.run_command("renew", "--id", silence_id, "--duration", "6h")
        output = json.loads(result.stdout)
        self.assertEqual(output["state"], "active")
        self.assertGreater(self.alertmanager.silences[silence_id]["endsAt"], old_end)
        self.assertEqual(self.alertmanager.posts[-1]["id"], silence_id)

    def test_failed_renewal_expires_the_unverified_silence(self):
        silence_id = self.alertmanager.add_managed_silence()
        self.seed_state(silence_id)
        self.alertmanager.add_alert("RoutineInfo", severity="info")
        self.alertmanager.ignore_silence_application = True
        result = self.run_command(
            "renew", "--id", silence_id, "--duration", "12h", expected=1
        )
        self.assertIn("coverage verification failed", result.stderr)
        self.assertEqual(self.alertmanager.deletes, [silence_id])
        self.assertEqual(
            self.alertmanager.silences[silence_id]["status"]["state"], "expired"
        )

    def test_renew_rejects_drifted_matchers_before_writing(self):
        silence_id = self.alertmanager.add_managed_silence()
        self.seed_state(silence_id)
        self.alertmanager.silences[silence_id]["matchers"] = [
            {
                "name": "alertname",
                "value": "OnlyThis",
                "isRegex": False,
                "isEqual": True,
            }
        ]
        result = self.run_command("renew", "--id", silence_id, expected=1)
        self.assertIn("canonical maintenance matchers", result.stderr)
        self.assertEqual(self.alertmanager.posts, [])

    def test_end_expires_only_a_managed_silence(self):
        silence_id = self.alertmanager.add_managed_silence()
        self.seed_state(silence_id)
        result = self.run_command("end", "--id", silence_id)
        output = json.loads(result.stdout)
        self.assertEqual(output["state"], "expired")
        self.assertEqual(self.alertmanager.silences[silence_id]["status"]["state"], "expired")
        self.assertEqual(self.alertmanager.deletes, [silence_id])
        self.assertEqual(self.status_api.resolved, ["notice-1"])
        self.assertFalse(Path(self.tmpdir.name, "maintenance.json").exists())

    def test_routine_begin_and_end_need_no_status_api_key(self):
        env = self.command_env(STATUS_API_KEY="")
        started = self.run_command(
            "begin", "--reason", "deploy Kaneo", env=env
        )
        start_output = json.loads(started.stdout)
        self.assertIsNone(start_output["noticeId"])
        self.assertEqual(start_output["publicStatus"], "not-posted")
        ended = self.run_command(
            "end", "--id", start_output["silenceId"], env=env
        )
        end_output = json.loads(ended.stdout)
        self.assertEqual(end_output["noticeState"], "not-posted")
        self.assertEqual(self.status_api.notices, {})
        self.assertFalse(Path(self.tmpdir.name, "maintenance.json").exists())

    def test_end_preserves_silence_and_notice_while_cluster_alerts_are_unhealthy(self):
        silence_id = self.alertmanager.add_managed_silence()
        self.alertmanager.add_alert(
            "CephHealthError", severity="critical", silenced_by=[silence_id]
        )
        self.seed_state(silence_id)
        result = self.run_command("end", "--id", silence_id, expected=1)
        self.assertIn("CephHealthError", result.stderr)
        self.assertEqual(self.alertmanager.deletes, [])
        self.assertEqual(self.status_api.resolved, [])
        self.assertTrue(Path(self.tmpdir.name, "maintenance.json").exists())

    def test_end_persists_observed_expiry_before_notice_resolution(self):
        silence_id = self.alertmanager.add_managed_silence()
        observed_end = timestamp(dt.datetime.now(UTC) - dt.timedelta(minutes=1))
        self.alertmanager.silences[silence_id]["endsAt"] = observed_end
        self.alertmanager.silences[silence_id]["status"] = {"state": "expired"}
        self.seed_state(silence_id)
        state_path = Path(self.tmpdir.name, "maintenance.json")
        self.status_api.fail_resolves = True

        result = self.run_command("end", "--id", silence_id, expected=1)

        self.assertIn("injected resolve failure", result.stderr)
        state = json.loads(state_path.read_text())
        self.assertEqual(state["alertmanagerEndedAt"], observed_end)
        self.assertTrue(state_path.exists())

    def test_repair_requires_explicit_approval_for_a_separately_silenced_alert(self):
        silence_id = self.alertmanager.add_managed_silence()
        self.alertmanager.silences[silence_id]["status"] = {"state": "expired"}
        baseline_id = "known-baseline"
        self.alertmanager.add_alert(
            "SmartDeviceInterfaceSlow",
            severity="critical",
            silenced_by=[baseline_id],
            kubernetes_node="k8s-rhea",
            device="sda",
        )
        self.seed_state(silence_id)

        rejected = self.run_command("repair", "--id", silence_id, expected=1)
        self.assertIn("SmartDeviceInterfaceSlow", rejected.stderr)
        self.assertEqual(self.alertmanager.deletes, [])
        self.assertTrue(Path(self.tmpdir.name, "maintenance.json").exists())

        result = self.run_command(
            "repair",
            "--id",
            silence_id,
            "--allow-active-alert",
            "alertname=SmartDeviceInterfaceSlow,kubernetes_node=k8s-rhea,device=sda",
        )

        output = json.loads(result.stdout)
        self.assertEqual(output["state"], "expired")
        self.assertEqual(output["repairState"], "cleared")
        self.assertEqual(self.alertmanager.deletes, [])
        self.assertEqual(self.alertmanager.alerts[0]["status"]["silencedBy"], [baseline_id])
        self.assertEqual(self.alertmanager.alerts[0]["status"]["state"], "suppressed")
        self.assertFalse(Path(self.tmpdir.name, "maintenance.json").exists())

    def test_repair_rejects_unscoped_baseline_selector(self):
        silence_id = self.alertmanager.add_managed_silence()
        self.alertmanager.silences[silence_id]["status"] = {"state": "expired"}
        self.seed_state(silence_id)
        result = self.run_command(
            "repair",
            "--id",
            silence_id,
            "--allow-active-alert",
            "alertname=SmartDeviceInterfaceSlow",
            expected=2,
        )
        self.assertIn("at least one identifying", result.stderr)
        self.assertEqual(self.alertmanager.deletes, [])

    def test_repair_rejects_severity_as_the_only_scope_label(self):
        silence_id = self.alertmanager.add_managed_silence()
        self.alertmanager.silences[silence_id]["status"] = {"state": "expired"}
        self.seed_state(silence_id)
        result = self.run_command(
            "repair",
            "--id",
            silence_id,
            "--allow-active-alert",
            "alertname=SmartDeviceInterfaceSlow,severity=critical",
            expected=2,
        )
        self.assertIn("other than severity", result.stderr)
        self.assertEqual(self.alertmanager.deletes, [])

    def test_repair_persists_allowance_for_an_interrupted_retry(self):
        silence_id = self.alertmanager.add_managed_silence()
        observed_end = timestamp(dt.datetime.now(UTC) - dt.timedelta(minutes=1))
        self.alertmanager.silences[silence_id]["endsAt"] = observed_end
        self.alertmanager.silences[silence_id]["status"] = {"state": "expired"}
        self.alertmanager.add_alert(
            "SmartDeviceInterfaceSlow",
            severity="critical",
            silenced_by=["known-baseline"],
            kubernetes_node="k8s-rhea",
            device="sda",
        )
        self.seed_state(silence_id)
        self.status_api.fail_resolves = True

        failed = self.run_command(
            "repair",
            "--id",
            silence_id,
            "--allow-active-alert",
            "alertname=SmartDeviceInterfaceSlow,kubernetes_node=k8s-rhea,device=sda",
            expected=1,
        )
        self.assertIn("injected resolve failure", failed.stderr)
        state = json.loads(Path(self.tmpdir.name, "maintenance.json").read_text())
        self.assertEqual(
            state["allowedRepairAlerts"],
            [
                {
                    "alertname": "SmartDeviceInterfaceSlow",
                    "kubernetes_node": "k8s-rhea",
                    "device": "sda",
                }
            ],
        )
        self.assertEqual(state["alertmanagerEndedAt"], observed_end)
        del self.alertmanager.silences[silence_id]

        self.status_api.fail_resolves = False
        retried = self.run_command("repair", "--id", silence_id)
        self.assertEqual(json.loads(retried.stdout)["noticeState"], "resolved")
        self.assertFalse(Path(self.tmpdir.name, "maintenance.json").exists())

    def test_repair_rejects_an_active_owned_silence(self):
        silence_id = self.alertmanager.add_managed_silence()
        self.seed_state(silence_id)
        result = self.run_command("repair", "--id", silence_id, expected=1)
        self.assertIn("requires an expired owned silence", result.stderr)
        self.assertEqual(self.alertmanager.deletes, [])
        self.assertTrue(Path(self.tmpdir.name, "maintenance.json").exists())

    def test_repair_rejects_invalid_persisted_alert_selectors(self):
        silence_id = self.alertmanager.add_managed_silence()
        self.alertmanager.silences[silence_id]["status"] = {"state": "expired"}
        self.seed_state(
            silence_id,
            allowed_alerts=[
                {"alertname": "SmartDeviceInterfaceSlow", "severity": "critical"}
            ],
        )

        result = self.run_command("repair", "--id", silence_id, expected=1)

        self.assertIn("invalid alert selector", result.stderr)
        self.assertEqual(self.status_api.resolved, [])
        self.assertTrue(Path(self.tmpdir.name, "maintenance.json").exists())

    def test_end_rejects_invalid_persisted_alert_selectors(self):
        silence_id = self.alertmanager.add_managed_silence()
        self.seed_state(
            silence_id,
            allowed_alerts=[
                {"alertname": "SmartDeviceInterfaceSlow", "severity": "critical"}
            ],
        )

        result = self.run_command("end", "--id", silence_id, expected=1)

        self.assertIn("invalid alert selector", result.stderr)
        self.assertEqual(self.alertmanager.deletes, [])
        self.assertEqual(self.status_api.resolved, [])
        self.assertTrue(Path(self.tmpdir.name, "maintenance.json").exists())

    def test_end_rejects_falsy_malformed_persisted_alert_selectors(self):
        silence_id = self.alertmanager.add_managed_silence()
        self.seed_state(silence_id)
        state_path = Path(self.tmpdir.name, "maintenance.json")
        state = json.loads(state_path.read_text())
        state["allowedActiveAlerts"] = {}
        state_path.write_text(json.dumps(state))

        result = self.run_command("end", "--id", silence_id, expected=1)

        self.assertIn("must be a list", result.stderr)
        self.assertEqual(self.alertmanager.deletes, [])
        self.assertEqual(self.status_api.resolved, [])
        self.assertTrue(state_path.exists())

    def test_repair_rejects_divergent_retained_silence_definitions(self):
        silence_id = self.alertmanager.add_managed_silence()
        self.alertmanager.silences[silence_id]["status"] = {"state": "expired"}
        second = FakeAlertmanager()
        try:
            second.silences[silence_id] = json.loads(
                json.dumps(self.alertmanager.silences[silence_id])
            )
            second.silences[silence_id]["comment"] = COMMENT_PREFIX + "different reason"
            self.seed_state(silence_id)
            env = self.command_env(
                ALERTMANAGER_URLS=f"{self.alertmanager.url},{second.url}"
            )

            result = self.run_command("repair", "--id", silence_id, expected=1, env=env)

            self.assertIn("divergent silence definitions", result.stderr)
            self.assertEqual(self.status_api.resolved, [])
            self.assertTrue(Path(self.tmpdir.name, "maintenance.json").exists())
        finally:
            second.close()

    def test_repair_clears_state_after_alertmanager_garbage_collects_expired_silence(self):
        silence_id = self.alertmanager.add_managed_silence()
        self.seed_state(silence_id)
        state_path = Path(self.tmpdir.name, "maintenance.json")
        state = json.loads(state_path.read_text())
        state["endsAt"] = timestamp(dt.datetime.now(UTC) - dt.timedelta(minutes=5))
        state_path.write_text(json.dumps(state))
        del self.alertmanager.silences[silence_id]

        result = self.run_command("repair", "--id", silence_id)

        output = json.loads(result.stdout)
        self.assertEqual(output["state"], "expired")
        self.assertEqual(output["repairState"], "cleared")
        self.assertFalse(state_path.exists())

    def test_repair_rejects_absent_silence_before_saved_expiry(self):
        silence_id = self.alertmanager.add_managed_silence()
        self.seed_state(silence_id)
        del self.alertmanager.silences[silence_id]

        result = self.run_command("repair", "--id", silence_id, expected=1)

        self.assertIn("cannot treat absent silence", result.stderr)
        self.assertTrue(Path(self.tmpdir.name, "maintenance.json").exists())

    def test_repair_rejects_proxy_style_not_found_responses(self):
        silence_id = self.alertmanager.add_managed_silence()
        self.seed_state(silence_id)
        state_path = Path(self.tmpdir.name, "maintenance.json")
        state = json.loads(state_path.read_text())
        state["endsAt"] = timestamp(dt.datetime.now(UTC) - dt.timedelta(minutes=5))
        state_path.write_text(json.dumps(state))
        del self.alertmanager.silences[silence_id]
        self.alertmanager.proxy_style_not_found = "html"

        result = self.run_command("repair", "--id", silence_id, expected=1)

        self.assertIn("HTTP 404", result.stderr)
        self.assertIn("proxy not found", result.stderr)
        self.assertEqual(self.status_api.resolved, [])
        self.assertTrue(state_path.exists())

    def test_repair_rejects_empty_404_without_exact_no_store_directive(self):
        silence_id = self.alertmanager.add_managed_silence()
        self.seed_state(silence_id)
        state_path = Path(self.tmpdir.name, "maintenance.json")
        state = json.loads(state_path.read_text())
        state["endsAt"] = timestamp(dt.datetime.now(UTC) - dt.timedelta(minutes=5))
        state_path.write_text(json.dumps(state))
        del self.alertmanager.silences[silence_id]
        self.alertmanager.proxy_style_not_found = "misleading-empty"

        result = self.run_command("repair", "--id", silence_id, expected=1)

        self.assertIn("HTTP 404", result.stderr)
        self.assertEqual(self.status_api.resolved, [])
        self.assertTrue(state_path.exists())

    def test_repair_uses_recorded_early_end_after_silence_is_garbage_collected(self):
        silence_id = self.alertmanager.add_managed_silence()
        self.seed_state(silence_id)
        state_path = Path(self.tmpdir.name, "maintenance.json")
        state = json.loads(state_path.read_text())
        recorded_end = timestamp(dt.datetime.now(UTC) - dt.timedelta(minutes=1))
        state["alertmanagerEndedAt"] = recorded_end
        state_path.write_text(json.dumps(state))
        del self.alertmanager.silences[silence_id]

        result = self.run_command("repair", "--id", silence_id)

        output = json.loads(result.stdout)
        self.assertEqual(output["endedAt"], recorded_end)
        self.assertEqual(output["repairState"], "cleared")
        self.assertFalse(state_path.exists())

    def test_end_rejects_a_foreign_silence(self):
        silence_id = self.alertmanager.add_managed_silence()
        self.seed_state(silence_id)
        self.alertmanager.silences[silence_id]["createdBy"] = "someone else"
        result = self.run_command("end", "--id", silence_id, expected=1)
        self.assertIn("not owned", result.stderr)
        self.assertEqual(self.alertmanager.posts, [])

    def test_duration_is_bounded(self):
        result = self.run_command(
            "begin", "--reason", "routine maintenance", "--duration", "25h", expected=2
        )
        self.assertIn("24h", result.stderr)
        self.assertEqual(self.alertmanager.posts, [])

    def test_probe_creates_and_expires_a_nonmatching_silence(self):
        self.alertmanager.add_alert("RealAlert", severity="critical")
        result = self.run_command("probe")
        output = json.loads(result.stdout)
        self.assertEqual(output["createdState"], "active")
        self.assertEqual(output["finalState"], "expired")
        self.assertEqual(output["matchedAlerts"], 0)
        self.assertEqual(self.alertmanager.deletes, ["maintenance-1"])
        self.assertEqual(self.alertmanager.alerts[0]["status"]["silencedBy"], [])

    def test_probe_collision_is_rejected_before_creating_a_silence(self):
        collision = "__known_probe_collision__"
        self.alertmanager.add_alert(collision, severity="critical")
        env = self.command_env(MAINTENANCE_PROBE_ALERTNAME=collision)
        result = self.run_command("probe", expected=1, env=env)
        self.assertIn("collision", result.stderr)
        self.assertEqual(self.alertmanager.posts, [])

    def test_probe_always_expires_after_post_creation_failure(self):
        self.alertmanager.fail_alert_reads_after_post = True
        result = self.run_command("probe", expected=1)
        self.assertIn("injected alert read failure", result.stderr)
        self.assertEqual(self.alertmanager.deletes, ["maintenance-1"])
        self.assertEqual(
            self.alertmanager.silences["maintenance-1"]["status"]["state"],
            "expired",
        )

    def test_begin_fails_when_an_ha_member_does_not_receive_the_silence(self):
        second = FakeAlertmanager()
        try:
            env = self.command_env(
                ALERTMANAGER_URLS=f"{self.alertmanager.url},{second.url}"
            )
            result = self.run_command(
                "begin", "--reason", "replication test", expected=1, env=env
            )
            self.assertIn("every HA member", result.stderr)
            self.assertEqual(self.alertmanager.deletes, ["maintenance-1"])
        finally:
            second.close()

    def test_probe_rejects_a_broadened_matcher_on_an_ha_member(self):
        second = FakeAlertmanager()
        second.mirror_source = self.alertmanager
        second.mirror_matchers = [
            {"name": "alertname", "value": ".+", "isRegex": True, "isEqual": True}
        ]
        try:
            env = self.command_env(
                ALERTMANAGER_URLS=f"{self.alertmanager.url},{second.url}"
            )
            result = self.run_command("probe", expected=1, env=env)
            self.assertIn("definitionsMatch=False", result.stderr)
            self.assertEqual(self.alertmanager.deletes, ["maintenance-1"])
        finally:
            second.close()

    def test_lease_release_read_failure_warns_without_hiding_success(self):
        lock = self.module.KubernetesBeginLock()
        lock.acquired = True
        failure = types.SimpleNamespace(returncode=1, stdout="", stderr="temporary failure")
        stderr = io.StringIO()
        with mock.patch.object(self.module, "run_kubectl", return_value=failure):
            with redirect_stderr(stderr):
                lock.__exit__(None, None, None)
        self.assertIn("will expire safely", stderr.getvalue())

    def test_lease_release_uses_resource_versioned_expiration_not_delete(self):
        lock = self.module.KubernetesBeginLock()
        lock.acquired = True
        lease = {
            "apiVersion": "coordination.k8s.io/v1",
            "kind": "Lease",
            "metadata": {
                "name": "cluster-maintenance-begin-lock",
                "namespace": "observability",
                "resourceVersion": "42",
            },
            "spec": {
                "holderIdentity": lock.identity,
                "leaseDurationSeconds": 120,
                "acquireTime": "2026-07-28T21:00:00.000000Z",
                "renewTime": "2026-07-28T21:00:00.000000Z",
            },
        }
        responses = [
            types.SimpleNamespace(returncode=0, stdout=json.dumps(lease), stderr=""),
            types.SimpleNamespace(returncode=0, stdout="", stderr=""),
        ]
        with mock.patch.object(self.module, "run_kubectl", side_effect=responses) as run:
            lock.__exit__(None, None, None)
        args, payload = run.call_args_list[1].args
        released = json.loads(payload)
        self.assertEqual(args, ["replace", "-f", "-"])
        self.assertEqual(released["metadata"]["resourceVersion"], "42")
        self.assertEqual(released["spec"]["leaseDurationSeconds"], 1)
        self.assertTrue(released["spec"]["holderIdentity"].endswith("-released"))

    def test_lease_release_never_removes_a_successor_lock(self):
        lock = self.module.KubernetesBeginLock()
        lock.acquired = True
        lease = {
            "metadata": {"resourceVersion": "43"},
            "spec": {"holderIdentity": "successor"},
        }
        response = types.SimpleNamespace(
            returncode=0, stdout=json.dumps(lease), stderr=""
        )
        with mock.patch.object(self.module, "run_kubectl", return_value=response) as run:
            lock.__exit__(None, None, None)
        self.assertEqual(run.call_count, 1)

    def test_lease_is_renewed_while_the_operation_is_running(self):
        lock = self.module.KubernetesBeginLock()
        lease = {
            "metadata": {"resourceVersion": "44"},
            "spec": {
                "holderIdentity": lock.identity,
                "leaseDurationSeconds": 120,
                "renewTime": "2026-07-28T21:00:00.000000Z",
            },
        }

        class OneCycle:
            calls = 0

            def wait(self, timeout):
                del timeout
                self.calls += 1
                return self.calls > 1

        lock.stop_renewal = OneCycle()
        responses = [
            types.SimpleNamespace(returncode=0, stdout=json.dumps(lease), stderr=""),
            types.SimpleNamespace(returncode=0, stdout="", stderr=""),
        ]
        with mock.patch.object(self.module, "run_kubectl", side_effect=responses) as run:
            lock.renew_loop()
        self.assertIsNone(lock.renewal_error)
        args, payload = run.call_args_list[1].args
        renewed = json.loads(payload)
        self.assertEqual(args, ["replace", "-f", "-"])
        self.assertEqual(renewed["spec"]["holderIdentity"], lock.identity)
        self.assertNotEqual(
            renewed["spec"]["renewTime"], "2026-07-28T21:00:00.000000Z"
        )


if __name__ == "__main__":
    unittest.main()
