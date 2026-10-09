"""Offline contract regression tests. Native image drills are separate CLI commands."""
import hashlib
import io
import json
import os
from pathlib import Path
import runpy
import subprocess
import tarfile
import tempfile
import time
import unittest
from unittest import mock

from test_kopiur_sqlite_cohort import load_documents, REPO

MODULE = runpy.run_path(str(REPO / "scripts/kopiur-postgres-drill"))
APPS = ("bazarr", "radarr")
NATIVE_APPS = ("radarr", "radarr-3d", "sonarr", "bazarr", "whisparr")


def capture_resources(directory):
    docs = load_documents(directory / "kopiur-policy.yaml")
    by_identity = {(doc["kind"], doc["metadata"]["name"]): doc for doc in docs}
    app = directory.parent.name
    schedule_name = app + ("-primary" if ("SnapshotSchedule", app + "-primary") in by_identity else "-daily")
    return (by_identity[("SnapshotPolicy", app)],
            by_identity[("SnapshotSchedule", schedule_name)],
            by_identity[("SnapshotReplication", app + "-nas-to-r2")])


class ManifestTests(unittest.TestCase):
    def test_all_postgres_transports_use_dedicated_identities(self):
        for app in APPS:
            with self.subTest(app=app):
                directory = REPO / "kubernetes/apps/arrs" / app / "app"
                nas, r2, pg = load_documents(directory / "externalsecret-kopiur.yaml")
                self.assertEqual(nas["spec"]["target"]["template"]["data"]["KOPIA_RCLONE_CONFIG"],
                                 "{{ .NAS_RCLONE_CONFIG }}")
                self.assertNotIn("guest", json.dumps(nas))
                self.assertEqual(r2["spec"]["target"]["template"]["data"]["AWS_ACCESS_KEY_ID"],
                                 "{{ .R2_ACCESS_KEY_ID }}")
                for secret in (nas, r2, pg):
                    self.assertEqual(secret["spec"]["dataFrom"], [{"extract": {"key": "kopiur-" + app}}])
                repositories = load_documents(directory / "kopiur-repositories.yaml")
                self.assertEqual(repositories[0]["spec"]["backend"]["rclone"]["remotePath"],
                                 "mnemosyne:kopiur-" + app)
                self.assertEqual(repositories[1]["spec"]["backend"]["s3"]["bucket"], "kopiur-" + app)

    def test_scoped_sql_has_an_explicit_database_and_role_allowlist(self):
        sql = (REPO / "kubernetes/apps/arrs/kopiur-backup-identities/app/provision.sql").read_text()
        for app in NATIVE_APPS:
            if app == "bazarr":
                continue
            self.assertIn("WHEN '" + app + "'", sql)
            role = "kopiur_" + app.replace("-", "_")
            self.assertIn("expected_role := '" + role + "'", sql)
            self.assertEqual(MODULE["DockerDrill"](app).capture_user, role)
        self.assertIn("NOT current_database() = ANY(allowed_databases)", sql)
        self.assertIn("shobj_description(role_oid, 'pg_authid')", sql)
        self.assertIn("unexpected write authority", sql)
        self.assertNotIn("pg_read_all_data", sql)

    def test_bazarr_grants_are_scoped_and_do_not_leak_admin_auth_to_mover(self):
        directory = REPO / "kubernetes/apps/arrs/kopiur-backup-identities/app"
        jobs = load_documents(directory / "jobs.yaml")
        job = next(j for j in jobs if j["metadata"]["name"] == "bazarr-kopiur-scoped-grants-v2")
        container, = job["spec"]["template"]["spec"]["containers"]
        env = {value["name"]: value for value in container["env"]}
        self.assertFalse(job["spec"]["template"]["spec"]["automountServiceAccountToken"])
        self.assertEqual(job["spec"]["activeDeadlineSeconds"], 180)
        self.assertEqual(env["PGDATABASES"]["value"], "bazarr")
        self.assertEqual(env["BACKUP_PASSWORD"]["valueFrom"]["secretKeyRef"],
                         {"name": "bazarr-kopiur-grant-identity", "key": "PGPASSWORD"})
        self.assertNotIn("kopiur-postgres-grants.yaml",
                         (REPO / "kubernetes/apps/arrs/bazarr/app/kustomization.yaml").read_text())
        sql = (directory / "bazarr.sql").read_text()
        self.assertIn("shobj_description(role_oid, 'pg_authid')", sql)
        self.assertIn("ALTER DEFAULT PRIVILEGES", sql)
        self.assertNotIn("pg_read_all_data", sql)
        self.assertIn("default_transaction_read_only = on", sql)
        self.assertIn("CASE WHEN c.relkind='S' THEN has_sequence_privilege", sql)

    def test_bazarr_uses_dedicated_transport_identities(self):
        directory = REPO / "kubernetes/apps/arrs/bazarr/app"
        nas, r2, _ = load_documents(directory / "externalsecret-kopiur.yaml")
        self.assertEqual(nas["spec"]["target"]["template"]["data"]["KOPIA_RCLONE_CONFIG"],
                         "{{ .NAS_RCLONE_CONFIG }}")
        self.assertNotIn("guest", json.dumps(nas))
        repositories = load_documents(directory / "kopiur-repositories.yaml")
        self.assertEqual(repositories[0]["spec"]["backend"]["rclone"]["remotePath"],
                         "mnemosyne:kopiur-bazarr")
        self.assertEqual(repositories[1]["spec"]["backend"]["s3"]["bucket"], "kopiur-bazarr")
        self.assertEqual(r2["spec"]["target"]["template"]["data"]["AWS_ACCESS_KEY_ID"],
                         "{{ .R2_ACCESS_KEY_ID }}")

    def test_runtime_shell_survives_flux_substitution(self):
        script = MODULE["CAPTURE"].read_bytes()
        result = subprocess.run(["flux", "envsubst", "--strict"], input=script, capture_output=True, check=True)
        self.assertEqual(result.stdout, script)
        subprocess.run(["sh", "-n"], input=script, check=True)

    def assert_capture_activation(self, app, policy, schedule, replication):
        # Each named original production receipt admits only its own app.
        # Synthetic suites and every other application remain suspended.
        evidence = policy.get("metadata", {}).get("annotations", {}).get("kopiur.home.arpa/original-native-recovery")
        if evidence is not None:
            original_receipts = {
                "bazarr": "https://github.com/shamubernetes/home-k8s/actions/runs/37919745482",
                "radarr": "https://github.com/shamubernetes/home-k8s/actions/runs/37981655593",
            }
            self.assertIn(app, original_receipts)
            self.assertEqual(evidence, original_receipts[app])
        suspended = evidence is None
        self.assertIs(policy["spec"]["suspend"], suspended)
        self.assertIs(schedule["spec"]["schedule"]["suspend"], suspended)
        staged_r2 = replication.get("metadata", {}).get("annotations", {}).get(
            "kopiur.home.arpa/awaiting-first-scheduled-point") == "true"
        if staged_r2:
            self.assertEqual(app, "radarr")
            self.assertFalse(suspended)
        self.assertIs(replication["spec"]["suspend"], suspended or staged_r2)
        self.assertEqual(policy["spec"]["defaultDeletionPolicy"], "Retain")
        self.assertEqual(replication["spec"]["migrate"]["policies"], "none")

    def test_activation_requires_the_exact_original_receipt_and_retains_history(self):
        directory = REPO / "kubernetes/apps/arrs/bazarr/app"
        policy, schedule, replication = capture_resources(directory)
        policy["metadata"]["annotations"] = {"kopiur.home.arpa/original-native-recovery": "https://github.com/shamubernetes/home-k8s/actions/runs/37919745482"}
        policy["spec"]["suspend"] = schedule["spec"]["schedule"]["suspend"] = replication["spec"]["suspend"] = False
        self.assert_capture_activation("bazarr", policy, schedule, replication)
        with self.assertRaises(AssertionError):
            self.assert_capture_activation("radarr", policy, schedule, replication)
        policy["metadata"]["annotations"]["kopiur.home.arpa/original-native-recovery"] = "synthetic"
        with self.assertRaises(AssertionError):
            self.assert_capture_activation("bazarr", policy, schedule, replication)
        policy["metadata"]["annotations"] = {}
        with self.assertRaises(AssertionError):
            self.assert_capture_activation("bazarr", policy, schedule, replication)

    def test_radarr_admits_only_original_recovery_with_safe_r2_staging(self):
        directory = REPO / "kubernetes/apps/arrs/radarr/app"
        policy, schedule, replication = capture_resources(directory)
        policy["metadata"]["annotations"] = {
            "kopiur.home.arpa/original-native-recovery": "https://github.com/shamubernetes/home-k8s/actions/runs/37981655593"}
        policy["spec"]["suspend"] = schedule["spec"]["schedule"]["suspend"] = False
        replication["metadata"]["annotations"] = {"kopiur.home.arpa/awaiting-first-scheduled-point": "true"}
        replication["spec"]["suspend"] = True
        self.assert_capture_activation("radarr", policy, schedule, replication)
        replication["metadata"]["annotations"] = {}
        with self.assertRaises(AssertionError):
            self.assert_capture_activation("radarr", policy, schedule, replication)
        replication["spec"]["suspend"] = False
        self.assert_capture_activation("radarr", policy, schedule, replication)

    def test_radarr_primary_preserves_incumbent_cadence_and_inactive_history(self):
        directory = REPO / "kubernetes/apps/arrs/radarr/app"
        docs = load_documents(directory / "kopiur-policy.yaml")
        schedules = {doc["metadata"]["name"]: doc for doc in docs if doc["kind"] == "SnapshotSchedule"}
        self.assertEqual(set(schedules), {"radarr-daily", "radarr-primary"})
        self.assertTrue(schedules["radarr-daily"]["spec"]["schedule"]["suspend"])
        primary = schedules["radarr-primary"]["spec"]["schedule"]
        self.assertFalse(primary["suspend"])
        self.assertEqual(primary["cron"], "${VOLSYNC_SCHEDULE_RADARR}")
        self.assertTrue(primary["runOnCreate"])
        self.assertEqual(primary["concurrencyPolicy"], "Forbid")
        policy, _, replication = capture_resources(directory)
        self.assertEqual(replication["spec"]["schedule"]["cron"], "${VOLSYNC_R2_SCHEDULE_RADARR}")
        self.assertEqual(policy["spec"]["verification"]["deep"]["capacity"], "24Gi")
        self.assertEqual(policy["spec"]["hooks"]["beforeSnapshot"][0]["workloadExec"]["timeout"], "13m")

    def test_each_policy_exports_database_before_pvc(self):
        for app in APPS:
            with self.subTest(app=app):
                directory = REPO / "kubernetes/apps/arrs" / app / "app"
                policy, schedule, replication = capture_resources(directory)
                self.assert_capture_activation(app, policy, schedule, replication)
                self.assertNotIn("suspend", schedule["spec"])
                spec = policy["spec"]
                self.assertEqual(spec["copyMethod"], "Snapshot")
                self.assertEqual(spec["sources"][0]["pvc"]["name"], app)
                hook = spec["hooks"]["beforeSnapshot"][0]["workloadExec"]
                self.assertEqual(hook["container"], "kopiur-postgres")
                self.assertFalse(hook["continueOnFailure"])
                active_radarr = app == "radarr" and not policy["spec"]["suspend"]
                expected = ["sh", "/kopiur/recurring-capture.sh"] if active_radarr else ["timeout", "600", "sh", "/kopiur/capture.sh"]
                self.assertEqual(hook["command"], expected)
                self.assertEqual(spec["verification"]["successExpr"], "stats.files > 0 && stats.errors == 0")

    def test_sidecar_is_dedicated_and_matches_fixture(self):
        for app in APPS:
            with self.subTest(app=app):
                directory = REPO / "kubernetes/apps/arrs" / app / "app"
                rendered = subprocess.run(["kustomize", "build", str(directory)], capture_output=True, check=True).stdout
                result = subprocess.run(["yq", "-o=json", "-I=0", "."], input=rendered, capture_output=True, check=True)
                docs = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
                hr = next(d for d in docs if d["kind"] == "HelmRelease")
                values = hr["spec"]["values"]
                self.assertEqual(len(values["controllers"]), 1)
                controller = next(iter(values["controllers"].values()))
                sidecar = controller["containers"]["kopiur-postgres"]
                image, databases, _, _, config = MODULE["CONTRACTS"][app]
                app_image = controller["containers"]["app"]["image"]
                self.assertEqual(app_image["repository"] + ":" + app_image["tag"], image)
                self.assertEqual(sidecar["env"]["PGDATABASES"].split(), databases)
                self.assertEqual(sidecar["env"]["CONFIG_FILE"], config)
                self.assertEqual(sidecar["env"]["CAPTURE_MODE"], "single-db-stable-filetree")
                self.assertEqual(sidecar["envFrom"], [{"secretRef": {"name": app + "-kopiur-postgres"}}])
                self.assertEqual(sidecar["env"]["PGHOST"], "postgres17-rw.database.svc.cluster.local")
                self.assertTrue(sidecar["securityContext"]["readOnlyRootFilesystem"])
                self.assertNotIn("globalMounts", values["persistence"]["media"])
                cm = next(d for d in docs if d["kind"] == "ConfigMap" and "capture.sh" in d.get("data", {}))
                self.assertEqual(cm["data"]["capture.sh"], MODULE["CAPTURE"].read_text())
                if app == "radarr":
                    policy, _, _ = capture_resources(directory)
                    if not policy["spec"]["suspend"]:
                        recurring = next(d for d in docs if d["kind"] == "ConfigMap" and
                                         d["metadata"]["name"] == "radarr-kopiur-recurring")
                        self.assertEqual(recurring["data"]["recurring.sh"],
                                         MODULE["RADARR_RECURRING"].read_text())
                        self.assertEqual(recurring["data"]["recurring-capture.sh"],
                                         (REPO / "scripts/kopiur-radarr-recurring-capture").read_text())
                        state = "/config/.kopiur-postgres/coordination"
                        self.assertEqual(controller["containers"]["app"]["env"]["QUIESCENCE_STATE_DIR"], state)
                        self.assertEqual(sidecar["env"]["QUIESCENCE_STATE_DIR"], state)
                        self.assertEqual(controller["containers"]["app"]["command"],
                                         ["/usr/bin/catatonit", "--", "sh", "/kopiur/recurring.sh", "supervise", "/entrypoint.sh"])
                        self.assertEqual(controller["containers"]["app"]["probes"]["readiness"]["spec"]["httpGet"]["path"], "/ping")
                secrets = load_documents(directory / "externalsecret-kopiur.yaml")
                for secret in secrets:
                    self.assertEqual(secret["spec"]["dataFrom"], [{"extract": {"key": "kopiur-" + app}}])
                self.assertNotIn("SUPER", json.dumps(secrets))


class RadarrQuiescenceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="radarr-hold-")
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.state = self.base / "state"
        self.env = dict(os.environ, QUIESCENCE_STATE_DIR=str(self.state))
        self.script = str(MODULE["RADARR_QUIESCE"])
        self.command = ["sh", "-c", 'printf resumed > "$1"', "entrypoint", str(self.base / "resumed")]

    def call(self, mode, deadline, *args):
        return subprocess.run(["sh", self.script, mode, str(deadline), *map(str, args)],
                              env=self.env, capture_output=True, timeout=10)

    def active(self, deadline):
        self.state.mkdir(exist_ok=True)
        (self.state / "active").write_text(str(deadline) + "\n")

    def test_deadline_is_absolute_and_expiry_restores_native_command(self):
        deadline = int(time.time()) + 3
        process = subprocess.Popen(["sh", self.script, "start", str(deadline), *self.command],
                                   env=self.env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        try:
            for _ in range(40):
                if (self.state / "active").exists():
                    break
                time.sleep(0.025)
            self.assertFalse((self.base / "resumed").exists())
            self.assertEqual(self.call("check", deadline, 1).returncode, 0)
            self.assertEqual(process.wait(timeout=6), 0)
            self.assertEqual((self.base / "resumed").read_text(), "resumed")
            self.assertFalse((self.state / "active").exists())
            self.assertNotEqual(self.call("check", deadline, 1).returncode, 0)
            # A container restart reuses the same expiry, never another 30 minutes.
            self.assertEqual(self.call("start", deadline, *self.command).returncode, 0)
            self.assertFalse((self.state / "active").exists())
        finally:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=3)
            if process.stderr is not None:
                process.stderr.close()

    def test_invalid_expired_or_overlong_holds_restore_service(self):
        now = int(time.time())
        # Stay outside the hold bound even if earlier invalid cases cross a second.
        for deadline in ("", "bad", "01", "1" * 100, now - 1, now + 1900):
            with self.subTest(deadline=deadline):
                self.assertEqual(self.call("start", deadline, *self.command).returncode, 0)
                self.assertEqual((self.base / "resumed").read_text(), "resumed")
                self.assertFalse((self.state / "active").exists())

    def test_capture_guard_checks_remaining_budget_and_exact_deadline(self):
        deadline = int(time.time()) + 60
        self.active(deadline)
        self.assertEqual(self.call("check", deadline, 30).returncode, 0)
        for value, minimum in ((deadline, 61), (deadline + 1, 1), (deadline, "01"),
                               (deadline, 0), (deadline, 1801), ("bad", 1)):
            with self.subTest(value=value, minimum=minimum):
                self.assertNotEqual(self.call("check", value, minimum).returncode, 0)
        (self.state / "active").unlink()
        self.assertNotEqual(self.call("check", deadline, 1).returncode, 0)

    def test_symlinked_state_never_admits_capture_or_blocks_resume(self):
        deadline = int(time.time()) + 60
        outside = self.base / "outside"
        outside.mkdir()
        self.state.symlink_to(outside, target_is_directory=True)
        self.assertNotEqual(self.call("check", deadline, 1).returncode, 0)
        self.assertEqual(self.call("start", deadline, *self.command).returncode, 0)
        self.assertEqual(list(outside.iterdir()), [])
        self.state.unlink()
        self.state.mkdir()
        destination = outside / "untouched"
        destination.write_text("keep")
        for name in ("active", "pending"):
            marker = self.state / name
            marker.symlink_to(destination)
            self.assertNotEqual(self.call("check", deadline, 1).returncode, 0)
            self.assertEqual(self.call("start", deadline, *self.command).returncode, 0)
            self.assertEqual(destination.read_text(), "keep")
            marker.unlink(missing_ok=True)

    def test_invalid_restart_clears_prior_valid_pause_evidence(self):
        deadline = int(time.time()) + 60
        self.active(deadline)
        self.assertEqual(self.call("check", deadline, 1).returncode, 0)
        self.assertEqual(self.call("start", "bad", *self.command).returncode, 0)
        self.assertEqual((self.base / "resumed").read_text(), "resumed")
        self.assertNotEqual(self.call("check", deadline, 1).returncode, 0)

    def test_special_marker_objects_are_rejected_without_blocking(self):
        deadline = int(time.time()) + 60
        self.state.mkdir()
        for name in ("active", "pending"):
            marker = self.state / name
            os.mkfifo(marker)
            self.assertNotEqual(self.call("check", deadline, 1).returncode, 0)
            self.assertEqual(self.call("start", deadline, *self.command).returncode, 0)
            marker.unlink(missing_ok=True)

    def test_active_directory_rejects_capture_but_restores_service(self):
        deadline = int(time.time()) + 60
        self.state.mkdir()
        (self.state / "active").mkdir()
        for expiry in (deadline, int(time.time()) - 1):
            self.assertEqual(self.call("start", expiry, *self.command).returncode, 0)
            self.assertEqual((self.base / "resumed").read_text(), "resumed")
            self.assertNotEqual(self.call("check", expiry, 1).returncode, 0)

    def test_runtime_shell_survives_flux_substitution(self):
        script = Path(self.script).read_bytes()
        subprocess.run(["sh", "-n"], input=script, check=True)
        result = subprocess.run(["flux", "envsubst", "--strict"], input=script,
                                capture_output=True, check=True)
        self.assertEqual(result.stdout, script)


class DatabaseStartupTests(unittest.TestCase):
    def test_socket_only_init_server_does_not_trigger_provisioning(self):
        drill = MODULE["DockerDrill"]("whisparr")
        container = "fixture-postgres"
        final_server = False
        operations = mock.Mock()
        operations.start.return_value = container

        def readiness(*args, **kwargs):
            # Socket probes succeed even during init; TCP only after the final start.
            tcp = "-h" in args and args[args.index("-h") + 1] == "127.0.0.1"
            return subprocess.CompletedProcess(args, 0 if final_server or not tcp else 2)

        def finish_init(seconds):
            nonlocal final_server
            final_server = True

        operations.run.side_effect = readiness
        operations.sleep.side_effect = finish_init
        # runpy's returned mapping is not the function's live globals dictionary.
        with mock.patch.dict(drill.database.__globals__, {"run": operations.run}), \
             mock.patch.object(MODULE["time"], "sleep", operations.sleep), \
             mock.patch.object(drill, "start", operations.start), \
             mock.patch.object(drill, "sql", operations.sql):
            result = drill.database("source-db", drill.databases)

        self.assertEqual(result, container)
        probe = mock.call.run("docker", "exec", container, "pg_isready", "-h", "127.0.0.1",
                              "-U", "postgres", check=False)
        self.assertEqual(operations.mock_calls, [
            mock.call.start("source-db", MODULE["PG_IMAGE"], env={
                "POSTGRES_PASSWORD": drill.password,
                "POSTGRES_INITDB_ARGS": "--auth-host=scram-sha-256",
            }),
            probe,
            mock.call.sleep(1),
            probe,
            mock.call.sql(container, f"CREATE ROLE app LOGIN PASSWORD '{drill.password}';\n"
                          f"CREATE ROLE backup LOGIN PASSWORD '{drill.backup_password}';"),
            mock.call.sql(container, 'CREATE DATABASE "whisparrv3_main" OWNER app;'),
            mock.call.sql(container, 'CREATE DATABASE "whisparrv3_logs" OWNER app;'),
        ])


class ConfigTests(unittest.TestCase):
    def test_capture_mode_matches_the_two_paired_app_contracts(self):
        for app in NATIVE_APPS:
            with self.subTest(app=app):
                drill = MODULE["DockerDrill"](app)
                with mock.patch.object(drill, "start", return_value="helper") as start, \
                     mock.patch.dict(drill.capture.__globals__, {"run": mock.Mock(return_value=subprocess.CompletedProcess([], 0))}):
                    drill.capture("database", "config", drill.databases)
                expected = "single-db-stable-filetree" if app in ("bazarr", "radarr") else "legacy"
                self.assertEqual(start.call_args.kwargs["env"]["CAPTURE_MODE"], expected)

    def test_xml_database_endpoint_and_credentials_replaced(self):
        drill = MODULE["DockerDrill"]("radarr")
        original = b"<Config><PostgresHost>source</PostgresHost><PostgresHost>duplicate</PostgresHost><PostgresPassword>old</PostgresPassword><ApiKey>old</ApiKey><InstanceName>kept</InstanceName></Config>"
        tree = MODULE["ET"].fromstring(drill.isolated_config(original))
        self.assertEqual([node.text for node in tree.findall("PostgresHost")], ["127.0.0.1"])
        self.assertEqual(tree.findtext("PostgresPassword"), drill.password)
        self.assertEqual(tree.findtext("ApiKey"), drill.api_key)
        self.assertEqual(tree.findtext("InstanceName"), "kept")

    def test_xml_entities_rejected(self):
        with self.assertRaises(ValueError):
            MODULE["DockerDrill"]().isolated_config(b'<!DOCTYPE Config [<!ENTITY x "expansion">]><Config>&x;</Config>')

    def test_bazarr_db_url_cleared(self):
        drill = MODULE["DockerDrill"]("bazarr")
        result = drill.isolated_config(b'postgresql:\n  host: source\n  url: old\nauth:\n  apikey: old\n')
        decoded = subprocess.run(["yq", "-o=json", "."], input=result, capture_output=True, check=True)
        data = json.loads(decoded.stdout)
        self.assertEqual(data["postgresql"]["host"], "127.0.0.1")
        self.assertEqual(data["postgresql"]["url"], "")
        self.assertEqual(data["postgresql"]["password"], drill.password)
        self.assertEqual(data["auth"]["apikey"], drill.api_key)


class BundleTests(unittest.TestCase):
    def setUp(self):
        scratch = Path.home() / ".hermes/cache/scratch"
        scratch.mkdir(parents=True, exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=scratch, prefix="k8s92-pg-unit-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        current = self.root / "current"
        current.mkdir()
        # Synthetic bytes test inventory validation, never substitute for a native DB drill.
        artifacts = {"application-config": b"fixture-config", "radarr_main.dump": b"unit-test-only",
                     "radarr_main.toc": b"unit-test-only", "metadata":
                     b"format=1\nconsistency=per-database-snapshot-before-pvc\ndatabase=radarr_main server_version=170000 tables=1\n"}
        for name, data in artifacts.items():
            (current / name).write_bytes(data)
        (current / "SHA256SUMS").write_text("".join(hashlib.sha256(data).hexdigest() + "  " + name + "\n" for name, data in artifacts.items()))
        (self.root / "COMPLETE").write_text("complete\n")

    def test_valid_inventory(self):
        self.assertEqual(MODULE["verify_bundle"](self.root), ["radarr_main"])

    def test_archive_paths_types_duplicates_and_capacity_are_checked(self):
        for names, kind, capacity in ((["../escape"], tarfile.REGTYPE, None),
                                      (["/absolute"], tarfile.REGTYPE, None),
                                      ([".kopiur-postgres/current"], tarfile.REGTYPE, None),
                                      (["link"], tarfile.SYMTYPE, None),
                                      (["same", "./same"], tarfile.REGTYPE, None),
                                      (["bounded"], tarfile.REGTYPE, 0)):
            with self.subTest(names=names, kind=kind, capacity=capacity):
                data = io.BytesIO()
                with tarfile.open(fileobj=data, mode="w") as archive:
                    for name in names:
                        member = tarfile.TarInfo(name)
                        member.type = kind
                        member.size = 1 if kind == tarfile.REGTYPE else 0
                        archive.addfile(member, io.BytesIO(b"x") if member.size else None)
                data.seek(0)
                with tarfile.open(fileobj=data, mode="r:") as archive:
                    with self.assertRaises(ValueError):
                        MODULE["validate_tree_members"](archive, capacity)

    def test_corrupt_dump_rejected(self):
        (self.root / "current/radarr_main.dump").write_bytes(b"changed")
        with self.assertRaises(ValueError):
            MODULE["verify_bundle"](self.root)

    def test_partial_attempt_rejected(self):
        (self.root / "COMPLETE").unlink()
        with self.assertRaises(FileNotFoundError):
            MODULE["verify_bundle"](self.root)

    def test_checksum_file_link_rejected(self):
        sums = self.root / "current/SHA256SUMS"
        sums.rename(self.root / "other")
        sums.symlink_to(self.root / "other")
        with self.assertRaises(ValueError):
            MODULE["verify_bundle"](self.root)

    def test_traversal_rejected(self):
        with (self.root / "current/SHA256SUMS").open("a") as handle:
            handle.write("0" * 64 + "  ../outside\n")
        with self.assertRaises(ValueError):
            MODULE["verify_bundle"](self.root)

    def test_radarr_legacy_bundle_rejected_before_docker(self):
        bundle = self.root / ".kopiur-postgres"
        bundle.mkdir()
        (self.root / "current").rename(bundle / "current")
        (self.root / "COMPLETE").rename(bundle / "COMPLETE")
        docker = mock.Mock()
        with mock.patch.dict(MODULE["restore_pvc"].__globals__, {"DockerDrill": docker}):
            with self.assertRaisesRegex(ValueError, "paired stable-filetree"):
                MODULE["restore_pvc"](self.root, "radarr")
        docker.assert_not_called()

    def test_bazarr_legacy_bundle_rejected_before_docker(self):
        current = self.root / "current"
        for path in tuple(current.iterdir()):
            if path.name.startswith("radarr_main"):
                path.rename(current / path.name.replace("radarr_main", "bazarr"))
        metadata = current / "metadata"
        metadata.write_text(metadata.read_text().replace("radarr_main", "bazarr"))
        artifacts = [p for p in current.iterdir() if p.name != "SHA256SUMS"]
        (current / "SHA256SUMS").write_text("".join(
            hashlib.sha256(p.read_bytes()).hexdigest() + "  " + p.name + "\n"
            for p in artifacts))
        bundle = self.root / ".kopiur-postgres"
        bundle.mkdir()
        current.rename(bundle / "current")
        (self.root / "COMPLETE").rename(bundle / "COMPLETE")
        with mock.patch.dict(MODULE["restore_pvc"].__globals__, {"DockerDrill": mock.Mock()}) as patched:
            with self.assertRaisesRegex(ValueError, "paired stable-filetree"):
                MODULE["restore_pvc"](self.root, "bazarr")
            patched["DockerDrill"].assert_not_called()

    def test_mismatched_application_rejected_before_docker(self):
        # A valid checksum inventory is not permission to restore an arbitrary DB name.
        bundle = self.root / ".kopiur-postgres"
        bundle.mkdir()
        (self.root / "current").rename(bundle / "current")
        (self.root / "COMPLETE").rename(bundle / "COMPLETE")
        with self.assertRaisesRegex(ValueError, "does not match"):
            MODULE["restore_pvc"](self.root, "sonarr")


if __name__ == "__main__":
    unittest.main()
