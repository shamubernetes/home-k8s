"""Offline contract regression tests. Native image drills are separate CLI commands."""
import hashlib
import json
from pathlib import Path
import runpy
import subprocess
import tempfile
import unittest
from unittest import mock

from test_kopiur_sqlite_cohort import load_documents, REPO

MODULE = runpy.run_path(str(REPO / "scripts/kopiur-postgres-drill"))
APPS = ("radarr", "radarr-3d", "sonarr", "bazarr", "whisparr")


class ManifestTests(unittest.TestCase):
    def test_bazarr_grants_are_scoped_and_do_not_leak_admin_auth_to_mover(self):
        directory = REPO / "kubernetes/apps/arrs/bazarr/app"
        job, = load_documents(directory / "kopiur-postgres-grants.yaml")
        pod = job["spec"]["template"]["spec"]
        container, = pod["containers"]
        env = {value["name"]: value for value in container["env"]}
        self.assertFalse(pod["automountServiceAccountToken"])
        self.assertEqual(job["spec"]["activeDeadlineSeconds"], 180)
        self.assertEqual(env["PGDATABASE"]["value"], "bazarr")
        self.assertEqual(env["PGPASSWORD"]["valueFrom"]["secretKeyRef"],
                         {"name": "bazarr-secret", "key": "INIT_POSTGRES_SUPER_PASS"})
        self.assertEqual(env["BACKUP_PASSWORD"]["valueFrom"]["secretKeyRef"],
                         {"name": "bazarr-kopiur-postgres", "key": "PGPASSWORD"})
        sql = (directory / "kopiur-postgres-grants.sql").read_text()
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

    def test_each_policy_exports_database_before_pvc(self):
        for app in APPS:
            with self.subTest(app=app):
                directory = REPO / "kubernetes/apps/arrs" / app / "app"
                policy, schedule, replication = load_documents(directory / "kopiur-policy.yaml")
                self.assertTrue(policy["spec"]["suspend"])
                self.assertTrue(schedule["spec"]["schedule"]["suspend"])
                self.assertNotIn("suspend", schedule["spec"])
                self.assertTrue(replication["spec"]["suspend"])
                spec = policy["spec"]
                self.assertEqual(spec["copyMethod"], "Snapshot")
                self.assertEqual(spec["sources"][0]["pvc"]["name"], app)
                hook = spec["hooks"]["beforeSnapshot"][0]["workloadExec"]
                self.assertEqual(hook["container"], "kopiur-postgres")
                self.assertFalse(hook["continueOnFailure"])
                self.assertEqual(hook["command"], ["timeout", "600", "sh", "/kopiur/capture.sh"])
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
                self.assertEqual(sidecar["envFrom"], [{"secretRef": {"name": app + "-kopiur-postgres"}}])
                self.assertEqual(sidecar["env"]["PGHOST"], "postgres17-rw.database.svc.cluster.local")
                self.assertTrue(sidecar["securityContext"]["readOnlyRootFilesystem"])
                self.assertNotIn("globalMounts", values["persistence"]["media"])
                cm = next(d for d in docs if d["kind"] == "ConfigMap" and "capture.sh" in d.get("data", {}))
                self.assertEqual(cm["data"]["capture.sh"], MODULE["CAPTURE"].read_text())
                secrets = load_documents(directory / "externalsecret-kopiur.yaml")
                for secret in secrets:
                    self.assertEqual(secret["spec"]["dataFrom"], [{"extract": {"key": "kopiur-" + app}}])
                self.assertNotIn("SUPER", json.dumps(secrets))


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
