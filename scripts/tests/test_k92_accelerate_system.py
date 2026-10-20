"""Native fixture and fail-closed integration tests for the four system candidates."""
import hashlib
import importlib.util
import json
import os
import shutil
import socket
import struct
import time
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest

from test_k92_deadline_system import candidate, documents, load

FIXTURES = Path(__file__).parent / "fixtures/k92-system"


def pocket_validator():
    spec = importlib.util.spec_from_file_location("pocket_validation", candidate("pocket-id") / "validate.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class BundleTests(unittest.TestCase):
    def test_actual_native_ha_archive(self):
        receipt = json.loads((FIXTURES / "provenance.json").read_text())
        path = FIXTURES / "ha-native-2026.8.0.tar"
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), receipt["ha_fixture_sha256"])
        load("home-assistant").validate_archive(path)
        with tarfile.open(path) as outer:
            with tarfile.open(fileobj=outer.extractfile("homeassistant.tar.gz")) as inner:
                self.assertNotIn("data/home-assistant_v2.db", inner.getnames())
                self.assertIn("data/.storage/auth", inner.getnames())

    def make_pocket(self, root):
        key = "uploads/oidc-client-images/native.client.png"
        path = root / "rgw" / key
        path.parent.mkdir(parents=True)
        path.write_bytes(b"native-object-byte-fixture")
        inventory = [{"Key": key, "Size": path.stat().st_size,
                      "ETag": '"' + hashlib.md5(path.read_bytes()).hexdigest() + '"'}]
        for name in ["objects-before.json", "objects-after.json"]:
            (root / name).write_text(json.dumps(inventory))
        (root / "required-objects.json").write_text(json.dumps([key]))
        (root / "ENCRYPTION_KEY").write_bytes(b"synthetic-original-key-not-a-production-secret")
        (root / "ENCRYPTION_KEY").chmod(0o600)
        return path, inventory

    def test_actual_downloaded_object_bytes_and_references(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_pocket(root)
            result = pocket_validator().validate(root)
            self.assertTrue(result["restored_database_references_equal"])
            self.assertFalse(result["original_key_native_auth_qualified"])
            self.assertEqual(result["required_object_count"], 1)

    def test_changed_same_length_bytes_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path, _ = self.make_pocket(root)
            path.write_bytes(b"x" * path.stat().st_size)
            with self.assertRaises(ValueError):
                pocket_validator().validate(root)

    def test_missing_database_reference_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_pocket(root)
            (root / "required-objects.json").write_text('["uploads/missing.png"]')
            with self.assertRaises(ValueError):
                pocket_validator().validate(root)

    def test_unsafe_and_linked_object_paths_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for key in ["../outside", "/absolute", "a//b", "a/./b"]:
                with self.subTest(key=key), self.assertRaises(ValueError):
                    pocket_validator().object_path(root, key)
            (root / "link").symlink_to(root, target_is_directory=True)
            with self.assertRaises(ValueError):
                pocket_validator().object_path(root, "link/file")

    def test_private_key_and_unsupported_etags_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _, inventory = self.make_pocket(root)
            (root / "ENCRYPTION_KEY").chmod(0o644)
            with self.assertRaises(ValueError):
                pocket_validator().validate(root)
            (root / "ENCRYPTION_KEY").chmod(0o600)
            inventory[0]["ETag"] = '"multipart-2"'
            (root / "objects-before.json").write_text(json.dumps(inventory))
            with self.assertRaises(ValueError):
                pocket_validator().validate(root)

    def test_private_ov_system_and_oauth_escrow(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            private = root / "data/_system"
            private.mkdir(parents=True)
            (private / "accounts.json").write_text('{"accounts":["fixture"]}')
            auth = root / "auth.json"
            auth.write_text('{"fixture":"refresh-state"}')
            work = root / "pending"
            work.mkdir()
            result = load("openviking").escrow_private(root, work, auth)
            self.assertEqual((work / "codex_auth.json").read_bytes(), auth.read_bytes())
            self.assertEqual(result["private_system_sha256"], load("openviking").private_roster(work / "private_system"))
            (private / "link").symlink_to(auth)
            with self.assertRaises(ValueError):
                load("openviking").private_roster(private)

    def test_missing_private_system_is_not_public_pack_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                load("openviking").private_roster(Path(tmp))

    def test_required_native_reference_stage_precedes_publication(self):
        policy = next(x for x in documents("pocket-id") if x["kind"] == "SnapshotPolicy")
        pod = policy["spec"]["hooks"]["beforeSnapshot"][0]["runJob"]["jobSpec"]["template"]["spec"]
        self.assertEqual([x["name"] for x in pod["initContainers"]],
                         ["objects", "database", "database-references", "objects-validate"])
        self.assertIn("python /scripts/validate.py", pod["containers"][0]["command"][2])
        refs = next(x for x in pod["initContainers"] if x["name"] == "database-references")
        self.assertEqual(refs["env"], [])
        self.assertNotIn("identity", [x["name"] for x in refs["volumeMounts"]])
        self.assertIn("listen_addresses=''", refs["command"][2])


class SharedDeadlineDeltaTests(unittest.TestCase):
    def test_real_kyverno_exact_minimal_shared_delta(self):
        repo = Path(__file__).resolve().parents[2]
        source = repo / "kubernetes/apps/kyverno/kyverno/policies/kopiur-movers.yaml"
        policy = json.loads(subprocess.check_output(["yq", "-o=json", ".", str(source)]))
        contracts = []
        for app in ["adguard-home", "home-assistant", "pocket-id", "openviking"]:
            c = json.loads((candidate(app) / "integration-prerequisites.json").read_text())["shared_deadline_delta"]
            contracts.append(c)
            matches = []
            for key, values in c["labels"].items():
                matches.append({"resources": {"kinds": ["batch/v1/Job"], "namespaces": [c["namespace"]],
                    "selector": {"matchLabels": {"app.kubernetes.io/managed-by": "kopiur"},
                    "matchExpressions": [{"key": "kopiur.home-operations.com/" + key, "operator": "In", "values": values}]}}})
            matches.append({"resources": {"kinds": ["batch/v1/Job"], "namespaces": [c["namespace"]],
                "selector": {"matchLabels": {"app.kubernetes.io/managed-by": "kopiur", "kopiur.home-operations.com/op": c["deletion_op"]}}}})
            policy["spec"]["rules"].append({"name": "cap-system-" + app, "match": {"any": matches},
                "mutate": {"patchStrategicMerge": {"spec": {"activeDeadlineSeconds": 3600}}}})
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            proposed = root / "proposed.yaml"
            proposed.write_bytes(subprocess.check_output(["yq", "-P", "."], input=json.dumps(policy).encode()))
            for c in contracts:
                cases = [(key, value, c["namespace"], "kopiur", 3600)
                         for key, values in c["labels"].items() for value in values]
                cases += [("op", c["deletion_op"], c["namespace"], "kopiur", 3600),
                          ("config", c["labels"]["config"][0], "default", "kopiur", 172800),
                          ("config", c["labels"]["config"][0], c["namespace"], "volsync", 172800),
                          ("config", c["labels"]["config"][0] + "-unrelated", c["namespace"], "kopiur", 172800)]
                for key, value, namespace, manager, expected in cases:
                    with self.subTest(key=key, value=value, namespace=namespace, manager=manager):
                        job = {"apiVersion": "batch/v1", "kind": "Job", "metadata": {"name": "system-deadline-fixture", "namespace": namespace,
                            "labels": {"app.kubernetes.io/managed-by": manager, "kopiur.home-operations.com/" + key: value}},
                            "spec": {"activeDeadlineSeconds": 172800, "template": {"spec": {"restartPolicy": "Never", "containers": [{"name": "mover", "image": "fixture.invalid/not-executed:v1"}]}}}}
                        resource, output = root / "resource.json", root / "output.yaml"
                        resource.write_text(json.dumps(job))
                        subprocess.run(["kyverno", "apply", str(proposed), "--resource", str(resource), "--output", str(output), "--remove-color"], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                        rows = subprocess.check_output(["yq", "-o=json", "-I=0", "select(. != null)", str(output)])
                        results = [json.loads(line) for line in rows.splitlines() if line.strip()]
                        self.assertEqual(len(results), 1)
                        self.assertEqual(results[0]["spec"]["activeDeadlineSeconds"], expected)


class NativeAdGuardTests(unittest.TestCase):
    @unittest.skipUnless(os.environ.get("K92_ADGUARD_BINARY"), "set K92_ADGUARD_BINARY to verified v0.107.79 native binary")
    def test_native_original_check_boot_dns_and_independent_copy(self):
        binary = Path(os.environ["K92_ADGUARD_BINARY"]).resolve()
        self.assertIn(b"v0.107.79", subprocess.check_output([str(binary), "--version"]))
        spec = importlib.util.spec_from_file_location("adguard_verify", candidate("adguard-home") / "verify.py")
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory(prefix="k92-adguard-") as tmp:
            root = Path(tmp)
            original = root / "original"
            (original / "conf").mkdir(parents=True)
            (original / "work").mkdir()
            shutil.copyfile(FIXTURES / "AdGuardHome.yaml", original / "conf/AdGuardHome.yaml")
            for tree in [original, root / "restored"]:
                if tree != original:
                    shutil.copytree(original, tree)
                receipt = module.verify(tree, binary)
                self.assertTrue(receipt["native_configuration_accepted"])
                self.assertFalse(receipt["native_dns_and_history_recovery_qualified"])
                with (root / "native.log").open("ab") as log:
                    proc = subprocess.Popen([str(binary), "--no-check-update", "--config", str(tree / "conf/AdGuardHome.yaml"), "--work-dir", str(tree / "work")], stdout=log, stderr=log)
                    try:
                        name = b"".join(bytes([len(x)]) + x.encode() for x in "restore.example.invalid".split(".")) + b"\0"
                        query = struct.pack("!6H", 1234, 0x100, 1, 0, 0, 0) + name + struct.pack("!2H", 1, 1)
                        deadline = time.monotonic() + 20
                        response = b""
                        while time.monotonic() < deadline:
                            self.assertIsNone(proc.poll(), (root / "native.log").read_text())
                            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                                sock.settimeout(0.5)
                                sock.sendto(query, ("127.0.0.1", 15353))
                                try:
                                    response, _ = sock.recvfrom(4096)
                                except TimeoutError:
                                    continue
                            break
                        self.assertTrue(response)
                        self.assertEqual(struct.unpack("!H", response[6:8])[0], 1, response.hex() + (root / "native.log").read_text() + (tree / "conf/AdGuardHome.yaml").read_text())
                        self.assertEqual(response[-4:], socket.inet_aton("192.0.2.17"))
                    finally:
                        # Only this disposable native fixture is stopped.
                        proc.terminate()
                        proc.wait(timeout=15)
            self.assertTrue((root / "restored/work/data/stats.db").is_file())


class NativePocketDatabaseTests(unittest.TestCase):
    def test_real_upstream_schema_restricted_dump_and_reference_restore(self):
        # Execute all actual upstream migrations, not a hand-written schema mock.
        schema = FIXTURES / "pocket-id-v2.18.0.sql"
        receipt = json.loads((FIXTURES / "provenance.json").read_text())
        self.assertEqual(hashlib.sha256(schema.read_bytes()).hexdigest(), receipt["pocket_id_sql_sha256"])
        with tempfile.TemporaryDirectory(prefix="k92-native-pg-") as tmp:
            root = Path(tmp)
            native_env = dict(os.environ, LC_ALL="C")
            subprocess.run(["initdb", "-A", "trust", "-U", "fixture", "-D", str(root / "db")], env=native_env, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.run(["pg_ctl", "-D", str(root / "db"), "-l", str(root / "pg.log"), "-o", f"-k {root} -c listen_addresses=''", "-w", "start"], env=native_env, check=True, stdout=subprocess.DEVNULL)
            env = dict(os.environ, PGHOST=str(root), PGUSER="fixture", PGDATABASE="postgres", PGSSLMODE="disable")
            def sql(command, *, db="postgres", user="fixture", check=True):
                return subprocess.run(["psql", "-XAt", "-v", "ON_ERROR_STOP=1", "-d", db, "-c", command], env=dict(env, PGUSER=user), check=check, capture_output=True)
            try:
                sql(schema.read_text())
                sql("INSERT INTO oidc_clients(id,name,image_type,dark_image_type) VALUES ('native.client','fixture','png','webp'), ('https://app.example.invalid/oauth/client','CIMD','svg',null)")
                sql("CREATE ROLE backup LOGIN; GRANT CONNECT ON DATABASE postgres TO backup; GRANT USAGE ON SCHEMA public TO backup; GRANT SELECT ON ALL TABLES IN SCHEMA public TO backup")
                dump = root / "database.dump"
                subprocess.run(["pg_dump", "--format=custom", "--no-owner", "--no-privileges", "--file=" + str(dump)], env=dict(env, PGUSER="backup"), check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                denied = sql("INSERT INTO oidc_clients(id,name) VALUES ('forbidden','write')", user="backup", check=False)
                self.assertNotEqual(denied.returncode, 0)
                sql("CREATE DATABASE restored")
                subprocess.run(["pg_restore", "--no-owner", "--no-privileges", "--exit-on-error", "-d", "restored", str(dump)], env=env, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                refs_sql = (candidate("pocket-id") / "references.sql").read_text()
                expected = json.loads(sql(refs_sql).stdout)
                actual = json.loads(sql(refs_sql, db="restored").stdout)
                self.assertEqual(actual, expected)
                self.assertEqual(len(actual), 3)
                self.assertIn("uploads/oidc-client-images/native.client-dark.webp", actual)
                cimd = hashlib.sha256(b"https://app.example.invalid/oauth/client").hexdigest()
                self.assertIn("uploads/oidc-client-images/cimd-" + cimd + ".svg", actual)
                sql("ALTER TABLE oidc_clients DROP COLUMN dark_image_type", db="restored")
                self.assertNotEqual(sql(refs_sql, db="restored", check=False).returncode, 0)
            finally:
                subprocess.run(["pg_ctl", "-D", str(root / "db"), "-m", "immediate", "-w", "stop"], check=True, stdout=subprocess.DEVNULL)


if __name__ == "__main__":
    unittest.main()
