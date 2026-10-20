#!/usr/bin/env python3
"""App-only online capture regression, using an isolated native PG17 fixture.

Run with python3 -m unittest discover -s scripts/tests
-p test_k92_radarr_whisparr_online_capture.py -v.
No production connection, credentials, volumes or application images are used.
"""
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import threading
import time
import unittest
import uuid

ROOT = Path(__file__).resolve().parents[2]
IMAGE = "docker.io/library/postgres:17@sha256:2d2b8998d31037bf721cfdf764d76ba74171b4fab3431b7f72c27c56ddbdf9e3"
APPS = {"radarr": ["radarr_main"], "whisparr": ["whisparrv3_main", "whisparrv3_logs"]}


def run(*args, **kwargs):
    return subprocess.run(args, text=True, capture_output=True, timeout=90, **kwargs)


def appdir(app):
    return ROOT / "kubernetes/apps/arrs" / app / "app"


class RenderTests(unittest.TestCase):
    def test_inline_delivery_and_unchanged_helmrelease(self):
        for app in APPS:
            rendered = run("kubectl", "kustomize", str(appdir(app)))
            self.assertEqual(rendered.returncode, 0, rendered.stderr)
            # yq emits one JSON object per document; slurp with its eval-all mode.
            docs = json.loads(run("yq", "ea", "-o=json", "-I=0", "[.]", input=rendered.stdout).stdout)
            policy = next(d for d in docs if d.get("kind") == "SnapshotPolicy" and d["metadata"]["name"] == app)
            command = policy["spec"]["hooks"]["beforeSnapshot"][0]["workloadExec"]["command"]
            self.assertEqual(command[:2], ["timeout", "--kill-after=30"])
            self.assertEqual(command[3:5], ["sh", "-c"])
            script = (appdir(app) / "recurring-capture.sh").read_text()
            self.assertEqual(command[5], script)
            substituted = run("flux", "envsubst", "--strict", input=script)
            self.assertEqual(substituted.returncode, 0, substituted.stderr)
            self.assertEqual(substituted.stdout, script)
            self.assertTrue(policy["spec"]["suspend"])
            cm = next(d for d in docs if d.get("kind") == "ConfigMap" and d["metadata"]["name"] == app + "-kopiur-recurring")
            self.assertEqual(cm["metadata"]["annotations"]["reloader.stakater.com/ignore"], "true")
            # No HelmRelease inputs changed, including command, mounts and probes.
            for filename in ("helmrelease.yaml", "kopiur-postgres-patch.yaml", "kopiur-recurring-runtime-patch.yaml"):
                rel = str((appdir(app) / filename).relative_to(ROOT))
                original = run("git", "show", "HEAD:" + rel, cwd=ROOT)
                self.assertEqual(original.returncode, 0)
                self.assertEqual((ROOT / rel).read_text(), original.stdout)
            for filename in ("recurring.sh", "recurring-capture.sh"):
                source = (appdir(app) / filename).read_text()
                self.assertEqual(run("sh", "-n", input=source).returncode, 0)
                for forbidden in ("kill -", "stop_child", " acquire ", "pg_advisory", "LOCK TABLE", "pg_backup_start", "request\" >"):
                    self.assertNotIn(forbidden, source)

    def test_stale_acquire_is_rejected(self):
        for app in APPS:
            self.assertEqual(run("sh", str(appdir(app) / "recurring.sh"), "acquire", "test").returncode, 2)


class NativeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.local = os.environ.get("K92_NATIVE_LOCAL") == "1"
        if cls.local:
            # Native PG17 fallback when Docker's filesystem is full. Only the
            # fixture root/socket paths change. No command or database is mocked.
            scratch = Path.home() / ".hermes/cache/scratch"
            cls.temp = tempfile.TemporaryDirectory(prefix="k92-arr-native-", dir=scratch)
            cls.addClassCleanup(cls.temp.cleanup)
            cls.fixture = Path(cls.temp.name)
            cls.config = cls.fixture / "config"
            cls.config.mkdir()
            cls.socket = cls.fixture / "socket"
            cls.socket.mkdir()
            version = run("postgres", "--version")
            if " 17." not in version.stdout:
                raise RuntimeError("local native tests require PG17")
            run("initdb", "-D", str(cls.fixture / "pg"), "-A", "trust", "-U", "postgres", check=True)
            # No TCP listener and a unique owned Unix socket. No existing server.
            started = run("pg_ctl", "-D", str(cls.fixture / "pg"), "-l", str(cls.fixture / "server.log"),
                          "-o", "-c listen_addresses='' -c unix_socket_directories=" + str(cls.socket), "-w", "start")
            if started.returncode:
                raise RuntimeError((cls.fixture / "server.log").read_text())
            cls.addClassCleanup(lambda: run("pg_ctl", "-D", str(cls.fixture / "pg"), "-m", "immediate", "-w", "stop"))
        else:
            cls.name = "k92-arr-online-test-" + uuid.uuid4().hex[:12]
            result = run("docker", "run", "-d", "--rm", "--network", "none", "--name", cls.name,
                         "-e", "POSTGRES_HOST_AUTH_METHOD=trust", IMAGE)
            if result.returncode:
                raise RuntimeError(result.stderr)
            cls.addClassCleanup(lambda: run("docker", "rm", "-f", cls.name))
        for _ in range(60):
            if cls.ex("pg_isready", "-U", "postgres").returncode == 0:
                break
            time.sleep(0.25)
        else:
            raise RuntimeError("isolated PostgreSQL did not become ready")
        cls.sql("postgres", "CREATE ROLE fixture LOGIN", user="postgres")
        for databases in APPS.values():
            for db in databases:
                cls.ex("createdb", "-U", "postgres", "-O", "fixture", db, check=True)
                cls.sql(db, "CREATE TABLE items (id integer primary key, value integer NOT NULL); INSERT INTO items SELECT n, 0 FROM generate_series(1,64) n")
        if not cls.local:
            cls.ex("sh", "-c", "mkdir /config; chown postgres:postgres /config", user="root", check=True)

    @classmethod
    def ex(cls, *args, user="postgres", check=False):
        if cls.local:
            args = tuple(re.sub(r'(?<![\w/])/config(?![\w.-])', str(cls.config),
                                arg.replace("/var/run/postgresql", str(cls.socket))) for arg in args)
            environment = {k: v for k, v in os.environ.items() if not k.startswith("PG")}
            environment.update(PGHOST=str(cls.socket), PGUSER="postgres", PGPORT="5432", PGSSLMODE="disable")
            return run(*args, check=check, env=environment)
        return run("docker", "exec", "-u", user, cls.name, *args, check=check)

    @classmethod
    def sql(cls, db, sql, user="fixture"):
        result = cls.ex("psql", "-X", "-A", "-t", "-v", "ON_ERROR_STOP=1", "-U", user, "-d", db, "-c", sql)
        if result.returncode:
            raise AssertionError(result.stderr)
        return result.stdout.strip()

    def reset_files(self):
        self.ex("sh", "-c", "rm -rf /config/* /config/.[!.]*; printf '<Config>fixture</Config>\\n' >/config/config.xml; mkdir /config/MediaCover; printf cover >/config/MediaCover/fixture", check=True)

    def capture(self, app, user="fixture", prefix=""):
        script = (appdir(app) / "recurring-capture.sh").read_text()
        return self.ex("env", "PGHOST=/var/run/postgresql", "PGUSER=" + user,
                       "PGPASSWORD=isolated-fixture-not-a-credential", "PGDATABASES=" + " ".join(APPS[app]),
                       "CONFIG_FILE=/config/config.xml", "sh", "-c", prefix + script)

    def test_native_restore_with_writers_active(self):
        for app, databases in APPS.items():
            with self.subTest(app=app):
                self.reset_files()
                before = int(self.sql(databases[0], "SELECT sum(value) FROM items"))
                stop = threading.Event()
                failures = []
                def write():
                    while not stop.is_set():
                        try:
                            for db in databases:
                                self.sql(db, "UPDATE items SET value=value+1")
                        except Exception as error:
                            failures.append(str(error))
                            return
                writer = threading.Thread(target=write)
                writer.start()
                try:
                    result = self.capture(app)
                finally:
                    stop.set()
                    writer.join(timeout=30)
                self.assertFalse(writer.is_alive())
                self.assertEqual(failures, [])
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertGreater(int(self.sql(databases[0], "SELECT sum(value) FROM items")), before)
                self.ex("sh", "-c", "cd /config/.kopiur-postgres/current; sha256sum -c SHA256SUMS; test -f ../COMPLETE; test ! -e ../coordination/request; tar -tf application-state.tar | grep -q './MediaCover/fixture'", check=True)
                metadata = self.ex("cat", "/config/.kopiur-postgres/current/metadata").stdout
                consistency = "single-db-stable-filetree" if app == "radarr" else "online-per-database-stable-filetree"
                self.assertIn("consistency=" + consistency, metadata)
                for db in databases:
                    target = db + "_restore"
                    self.ex("dropdb", "--if-exists", "-U", "postgres", target, check=True)
                    self.ex("createdb", "-U", "postgres", "-O", "fixture", target, check=True)
                    self.ex("pg_restore", "-U", "fixture", "--exit-on-error", "--no-owner", "--no-privileges", "-d", target,
                            "/config/.kopiur-postgres/current/" + db + ".dump", check=True)
                    self.assertEqual(self.sql(target, "SELECT count(*), count(DISTINCT value) FROM items"), "64|1")

    def test_diagnostic_text_log_changes_are_still_archived(self):
        self.reset_files()
        self.ex("sh", "-c", "mkdir /config/logs; printf before >/config/logs/system.txt", check=True)
        prefix = 'pg_dump() { printf changed >>/config/logs/system.txt; command pg_dump "$@"; }; '
        result = self.capture("whisparr", prefix=prefix)
        self.assertEqual(result.returncode, 0, result.stderr)
        archived = self.ex("tar", "-xOf", "/config/.kopiur-postgres/current/application-state.tar", "./logs/system.txt")
        self.assertEqual(archived.returncode, 0)
        self.assertEqual(archived.stdout, "beforechangedchanged")
        self.assertIn("all-log-bytes-archived", self.ex("cat", "/config/.kopiur-postgres/current/metadata").stdout)

    def test_non_text_state_in_log_directory_is_not_exempted(self):
        self.reset_files()
        self.ex("sh", "-c", "mkdir /config/logs; printf native-state >/config/logs/state.db", check=True)
        self.assertNotEqual(self.capture("whisparr").returncode, 0)
        self.assertNotEqual(self.ex("test", "-e", "/config/.kopiur-postgres/COMPLETE").returncode, 0)

    def test_mutating_files_fail_without_publishing(self):
        for app in APPS:
            self.reset_files()
            # Deterministic mutation at the first native pg_dump boundary.
            prefix = "pg_dump() { printf changed >>/config/MediaCover/fixture; command pg_dump \"$@\"; }; "
            result = self.capture(app, prefix=prefix)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("application file state changed", result.stderr)
            self.assertNotEqual(self.ex("test", "-e", "/config/.kopiur-postgres/COMPLETE").returncode, 0)

    def test_whisparr_owned_filter_and_rollback(self):
        self.reset_files()
        result = self.capture("whisparr")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.ex("test", "-f", "/config/.kopiaignore").returncode, 0)
        result = self.capture("whisparr", prefix="export KOPIUR_BUNDLE_ONLY=false; ")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotEqual(self.ex("test", "-e", "/config/.kopiaignore").returncode, 0)
        self.ex("sh", "-c", "printf foreign >/config/.kopiaignore", check=True)
        self.assertNotEqual(self.capture("whisparr").returncode, 0)
        self.assertEqual(self.ex("cat", "/config/.kopiaignore").stdout, "foreign")

    def test_failed_dump_invalidates_success_and_preserves_current(self):
        for app in APPS:
            self.reset_files()
            self.assertEqual(self.capture(app).returncode, 0)
            old = self.ex("cat", "/config/.kopiur-postgres/current/SHA256SUMS").stdout
            result = self.capture(app, prefix="pg_dump() { return 17; }; ")
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(self.ex("cat", "/config/.kopiur-postgres/current/SHA256SUMS").stdout, old)
            self.assertNotEqual(self.ex("test", "-e", "/config/.kopiur-postgres/COMPLETE").returncode, 0)
            self.assertNotEqual(self.ex("test", "-e", "/config/.kopiur-postgres/pending").returncode, 0)

    def test_elevated_identity_rejected(self):
        for app in APPS:
            self.reset_files()
            result = self.capture(app, user="postgres")
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("refusing elevated capture identity", result.stderr)

    def test_special_lock_and_legacy_hold_rejected(self):
        for app in APPS:
            for setup in ("mkfifo /config/.kopiur-postgres/lock",
                          "ln -s /missing /config/.kopiur-postgres/lock",
                          "mkdir -p /config/.kopiur-postgres/coordination; touch /config/.kopiur-postgres/coordination/request"):
                self.reset_files()
                self.ex("mkdir", "-p", "/config/.kopiur-postgres", check=True)
                self.ex("sh", "-c", setup, check=True)
                self.assertNotEqual(self.capture(app).returncode, 0)


if __name__ == "__main__":
    unittest.main()
