"""Cohort B candidate tests. Native transport and production restores are separate gates."""
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
APPS = {
    "adguard-home": "adguard-home/adguard-home",
    "home-assistant": "home-assistant/home-assistant",
    "pocket-id": "security/pocket-id",
    "openviking": "tools/openviking",
}


def candidate(app):
    return ROOT / "kubernetes/apps" / APPS[app] / "backup-candidate"


def documents(app):
    output = subprocess.check_output(["kubectl", "kustomize", str(candidate(app))])
    rows = subprocess.check_output(["yq", "-o=json", "-I=0", "."], input=output)
    return [json.loads(line) for line in rows.splitlines() if line != b"null"]


def load(app):
    spec = importlib.util.spec_from_file_location(app, candidate(app) / "capture.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class CandidateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rendered = {app: documents(app) for app in APPS}

    def item(self, app, kind):
        return next(x for x in self.rendered[app] if x["kind"] == kind)

    def test_all_four_render(self):
        self.assertEqual(set(self.rendered), set(APPS))

    def test_no_registration_until_prerequisites(self):
        for rel in APPS.values():
            for name in ["ks.yaml", "app/kustomization.yaml"]:
                self.assertNotIn("backup-candidate", (ROOT / "kubernetes/apps" / rel / name).read_text())

    def test_no_speculative_externalsecrets(self):
        for rows in self.rendered.values():
            self.assertNotIn("ExternalSecret", [x["kind"] for x in rows])

    def test_daily_capture_not_hourly(self):
        for app in APPS:
            s = self.item(app, "SnapshotSchedule")["spec"]["schedule"]
            self.assertRegex(s["cron"], r"^\d+ 18 \* \* \*$")
            self.assertEqual(s["concurrencyPolicy"], "Forbid")
            self.assertIs(s["runOnCreate"], False)

    def test_adguard_complete_primary(self):
        s = self.item("adguard-home", "SnapshotPolicy")["spec"]
        self.assertEqual([x["pvc"]["name"] for x in s["sources"]], ["data-adguard-home-0"])
        self.assertNotIn("hooks", s)
        self.assertNotIn("ignore", json.dumps(s))

    def test_ha_config_git_and_native_bundle(self):
        s = self.item("home-assistant", "SnapshotPolicy")["spec"]
        self.assertEqual([x["pvc"]["name"] for x in s["sources"]], ["home-assistant", "home-assistant-git", "home-assistant-kopiur-capture"])

    def test_native_ha_export_writes_into_the_paired_bundle(self):
        s = self.item("home-assistant", "SnapshotPolicy")["spec"]
        pod = s["hooks"]["beforeSnapshot"][0]["runJob"]["jobSpec"]["template"]["spec"]
        native = next(x for x in pod["initContainers"] if x["name"] == "native-config")
        mount = next(x for x in native["volumeMounts"] if x["name"] == "capture")
        self.assertEqual(mount["mountPath"], "/capture")
        self.assertEqual(mount["subPath"], "pending")
        self.assertEqual(pod["initContainers"][0]["name"], "prepare")

    def test_pocket_does_not_mistake_unmounted_pvc_for_state(self):
        s = self.item("pocket-id", "SnapshotPolicy")["spec"]
        self.assertEqual([x["pvc"]["name"] for x in s["sources"]], ["pocket-id-kopiur-capture"])
        body = json.dumps(s)
        for expected in ["pg_dump", "ENCRYPTION_KEY", "pocket-id-bucket", "objects-before.json", "objects-after.json"]:
            self.assertIn(expected, body)
        self.assertNotIn("--delete", body)

    def test_openviking_all_persistent_state(self):
        s = self.item("openviking", "SnapshotPolicy")["spec"]
        self.assertEqual([x["pvc"]["name"] for x in s["sources"]], ["openviking", "openviking-auth", "openviking-models"])
        hook = s["hooks"]["beforeSnapshot"][0]["workloadExec"]
        self.assertEqual(hook["podSelector"]["matchLabels"]["app.kubernetes.io/controller"], "openviking")
        self.assertEqual(hook["command"][2], (candidate("openviking") / "capture.py").read_text())
        compile(hook["command"][2], "rendered-hook", "exec")

    def test_exact_replication_identity_per_source(self):
        for app in APPS:
            p = self.item(app, "SnapshotPolicy")["spec"]
            r = self.item(app, "SnapshotReplication")["spec"]
            self.assertEqual(r["selection"]["identities"]["include"], [dict(p["identity"], sourcePath=x["sourcePathOverride"]) for x in p["sources"]])

    def test_native_jobs_have_deadlines_and_no_api_identity(self):
        for app in ["pocket-id", "home-assistant"]:
            hook = self.item(app, "SnapshotPolicy")["spec"]["hooks"]["beforeSnapshot"][0]["runJob"]
            self.assertIs(hook["continueOnFailure"], False)
            job = hook["jobSpec"]
            self.assertEqual(job["activeDeadlineSeconds"], 600)
            self.assertEqual(job["backoffLimit"], 0)
            pod = job["template"]["spec"]
            self.assertIs(pod["automountServiceAccountToken"], False)
            for container in pod["containers"] + pod["initContainers"]:
                self.assertIn("@sha256:", container["image"])
                self.assertIs(container["securityContext"]["allowPrivilegeEscalation"], False)
                self.assertNotIn("envFrom", container)
                if container["command"][0] == "sh":
                    subprocess.run(["sh", "-n", "-c", container["command"][2]], check=True)

    def test_retain_capture_points_and_strict_verification(self):
        for app in APPS:
            s = self.item(app, "SnapshotPolicy")["spec"]
            self.assertEqual(s["defaultDeletionPolicy"], "Retain")
            self.assertEqual(s["verification"]["quick"]["maxErrors"], 0)
            self.assertEqual(s["verification"]["successExpr"], "snapshot != null && stats.errors == 0")

    def test_no_application_lifecycle_commands(self):
        for rows in self.rendered.values():
            text = json.dumps(rows)
            for forbidden in ["SIGSTOP", "SIGTERM", "fsfreeze", "scale --replicas", "homeassistant/restart", "lock_database("]:
                self.assertNotIn(forbidden, text)


class NativeArchiveTests(unittest.TestCase):
    def archive(self, path, *, include_database=False, payload=True):
        with tarfile.open(path, "w") as archive:
            data = json.dumps({"protected": False, "homeassistant": {"exclude_database": not include_database}}).encode()
            item = tarfile.TarInfo("./backup.json")
            item.size = len(data)
            archive.addfile(item, io.BytesIO(data))
            if payload:
                payload_bytes = io.BytesIO()
                with tarfile.open(fileobj=payload_bytes, mode="w:gz") as inner:
                    for name in ["data/configuration.yaml", "data/.storage/auth"]:
                        item = tarfile.TarInfo(name)
                        item.size = 1
                        inner.addfile(item, io.BytesIO(b"x"))
                raw = payload_bytes.getvalue()
                item = tarfile.TarInfo("./homeassistant.tar.gz")
                item.size = len(raw)
                archive.addfile(item, io.BytesIO(raw))

    def test_native_archive_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "backup.tar"
            self.archive(path)
            load("home-assistant").validate_archive(path)

    def test_database_included_archive_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "backup.tar"
            self.archive(path, include_database=True)
            with self.assertRaises(ValueError):
                load("home-assistant").validate_archive(path)

    def test_missing_native_payload_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "backup.tar"
            self.archive(path, payload=False)
            with self.assertRaises(ValueError):
                load("home-assistant").validate_archive(path)

    def test_openviking_workspace_guard(self):
        with patch.dict(os.environ, {"OPENVIKING_CONF_CONTENT": json.dumps({"storage": {"workspace": "/wrong"}})}):
            with self.assertRaises(ValueError):
                load("openviking").capture()

    def test_openviking_failure_preserves_previous_capture(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            current = root / "kopiur-native/current"
            current.mkdir(parents=True)
            (current / "state.ovpack").write_bytes(b"prior-original")
            private = root / "data/_system"
            private.mkdir(parents=True)
            (private / "accounts.json").write_text('{"fixture":true}')
            config = json.dumps({"storage": {"workspace": str(root / "data")}, "server": {"root_api_key": "fixture-only"}})
            module = load("openviking")
            with patch.dict(os.environ, {"OPENVIKING_CONF_CONTENT": config}), patch.object(module, "urlopen", side_effect=OSError("fixture")):
                with self.assertRaises(OSError):
                    module.capture(root)
            self.assertEqual((current / "state.ovpack").read_bytes(), b"prior-original")
            self.assertEqual(list((root / "kopiur-native").glob("pending-*")), [])


@unittest.skipUnless(all(shutil.which(x) for x in ["initdb", "pg_ctl", "psql", "pg_dump", "pg_restore"]), "native PostgreSQL tools unavailable")
class NativePostgresTests(unittest.TestCase):
    def test_real_online_dump_and_original_native_restore(self):
        # Independent local database, no network listener or production credentials.
        with tempfile.TemporaryDirectory(prefix="k92-system-pg-") as tmp:
            base = Path(tmp)
            data = base / "db"
            native_env = dict(os.environ, LC_ALL="C")
            subprocess.run(["initdb", "-A", "trust", "-U", "fixture", "-D", str(data)], env=native_env, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            started = subprocess.run(["pg_ctl", "-D", str(data), "-l", str(base / "postgres.log"), "-o", f"-k {base} -c listen_addresses=''", "-w", "start"], env=native_env, stdout=subprocess.DEVNULL)
            if started.returncode:
                raise RuntimeError((base / "postgres.log").read_text())
            try:
                env = dict(os.environ, PGHOST=str(base), PGUSER="fixture", PGDATABASE="postgres", PGSSLMODE="disable", PGPORT="5432")
                def sql(command, db="postgres"):
                    return subprocess.check_output(["psql", "-XAt", "-v", "ON_ERROR_STOP=1", "-d", db, "-c", command], env=env, stderr=subprocess.DEVNULL)
                sql("CREATE TABLE original(id int primary key, value text); INSERT INTO original SELECT n, md5(n::text) FROM generate_series(1,100) n")
                expected = sql("SELECT md5(string_agg(id::text || value, ',' ORDER BY id)) FROM original")
                # An existing producer session stays connected during native capture.
                writer = subprocess.Popen(["psql", "-XAt", "-d", "postgres", "-c", "SELECT pg_sleep(20)"], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                try:
                    for app in ["home-assistant", "pocket-id"]:
                        capture = base / (app + "-capture")
                        capture.mkdir()
                        policy = next(x for x in documents(app) if x["kind"] == "SnapshotPolicy")
                        pod = policy["spec"]["hooks"]["beforeSnapshot"][0]["runJob"]["jobSpec"]["template"]["spec"]
                        c = next(x for x in pod["initContainers"] + pod["containers"] if x["name"] == "database")
                        # Exercise the exact pg_dump and pg_restore-list commands from the candidate.
                        script = c["command"][2].split("pg_restore --list")[0] + "pg_restore --list /capture/pending/database.dump >/dev/null 2>&1\n"
                        script = script.replace("/capture", str(capture))
                        subprocess.run(["sh", "-ec", script], env=env, check=True)
                        self.assertIsNone(writer.poll())
                        db = app.replace("-", "_") + "_restored"
                        sql("CREATE DATABASE " + db)
                        subprocess.run(["pg_restore", "--no-owner", "--no-privileges", "--exit-on-error", "-d", db, str(capture / "pending/database.dump")], env=env, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                        self.assertEqual(sql("SELECT md5(string_agg(id::text || value, ',' ORDER BY id)) FROM original", db), expected)
                finally:
                    writer.terminate()
                    writer.wait(timeout=10)
            finally:
                subprocess.run(["pg_ctl", "-D", str(data), "-m", "immediate", "-w", "stop"], check=True, stdout=subprocess.DEVNULL)


if __name__ == "__main__":
    unittest.main()
