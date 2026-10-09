#!/usr/bin/env python3
"""Local fail-closed protocol tests. Native stop/resume runs in the existing ARC drill."""
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/kopiur-radarr-recurring"


class RecurringTests(unittest.TestCase):
    def setUp(self):
        scratch = Path.home() / ".hermes/cache/scratch"
        scratch.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=scratch, prefix="radarr-recurring-test-")
        self.state = Path(self.temp.name)
        self.generation = "a" * 32
        self.token = "b" * 32
        self.deadline = int(time.time()) + 1800
        (self.state / "boot").write_text(self.generation)
        self.request = f"{self.generation} {self.token} {self.deadline}\n"
        for name in ("request", "active", "capture-owner"):
            (self.state / name).write_text(self.request)

    def tearDown(self):
        self.temp.cleanup()

    def call(self, *args):
        return subprocess.run(["sh", str(SCRIPT), *args], env={**os.environ, "QUIESCENCE_STATE_DIR": str(self.state)},
                              capture_output=True, timeout=3).returncode

    def test_matching_current_hold(self):
        self.assertEqual(self.call("check", "720"), 0)

    def test_missing_and_stale_ack(self):
        (self.state / "active").unlink()
        self.assertNotEqual(self.call("check", "1"), 0)
        (self.state / "active").write_text("old generation\n")
        self.assertNotEqual(self.call("check", "1"), 0)

    def test_restart_generation_invalidates_ack(self):
        (self.state / "boot").write_text("c" * 32)
        self.assertNotEqual(self.call("check", "1"), 0)

    def test_expired_hold(self):
        for name in ("request", "active"):
            (self.state / name).write_text(f"{self.generation} {self.token} {int(time.time()) - 1}\n")
        self.assertNotEqual(self.call("check", "1"), 0)

    def test_guard_rejects_special_objects_without_blocking(self):
        for name in ("boot", "request", "active", "capture-owner"):
            original = (self.state / name).read_bytes() if (self.state / name).exists() else None
            (self.state / name).unlink(missing_ok=True)
            for kind in ("fifo", "directory", "symlink"):
                path = self.state / name
                if kind == "fifo":
                    os.mkfifo(path)
                elif kind == "directory":
                    path.mkdir()
                else:
                    path.symlink_to(self.state / "other")
                self.assertNotEqual(self.call("check", "1"), 0, (name, kind))
                path.rmdir() if path.is_dir() else path.unlink()
            if original is not None:
                (self.state / name).write_bytes(original)

    def test_foreign_release_token_preserves_owner(self):
        self.assertEqual(self.call("release", "c" * 32), 0)
        self.assertEqual((self.state / "request").read_text(), self.request)
        self.assertTrue((self.state / "capture-owner").exists())

    def test_lock_special_objects_are_rejected_before_open(self):
        driver = ROOT / "scripts/kopiur-radarr-recurring-capture"
        for kind in ("fifo", "directory", "symlink"):
            path = self.state / "client-lock"
            if kind == "fifo":
                os.mkfifo(path)
            elif kind == "directory":
                path.mkdir()
            else:
                path.symlink_to(self.state / "other")
            result = subprocess.run(["sh", str(driver)], env={**os.environ, "QUIESCENCE_STATE_DIR": str(self.state),
                                        "CAPTURE_MODE": "single-db-stable-filetree", "PGDATABASES": "radarr_main"},
                                    capture_output=True, timeout=3)
            self.assertNotEqual(result.returncode, 0)
            path.rmdir() if path.is_dir() else path.unlink()

    def test_release_cannot_remove_another_request(self):
        (self.state / "capture-owner").write_text(f"{'d' * 32} {'c' * 32} {self.deadline}\n")
        self.assertEqual(self.call("release"), 0)
        self.assertEqual((self.state / "request").read_text(), self.request)

    def test_supervisor_cleanup_preserves_restart_generation(self):
        text = SCRIPT.read_text()
        cleanup = text.split("    cleanup() {", 1)[1].split("    }", 1)[0]
        result = subprocess.run(["sh", "-c", 'set -eu; state=$1; child=; ' + cleanup, "fixture", str(self.state)],
                                capture_output=True, timeout=3)
        self.assertEqual(result.returncode, 0)
        self.assertTrue((self.state / "boot").exists())
        self.assertTrue((self.state / "request").exists())
        self.assertFalse((self.state / "active").exists())

    def test_owned_failure_release(self):
        self.assertEqual(self.call("release"), 0)
        self.assertFalse((self.state / "request").exists())
        self.assertNotEqual(self.call("check", "1"), 0)

    def test_minimum_and_upper_bounds(self):
        for minimum in ("0", "-1", "01", "garbage", "1801"):
            self.assertNotEqual(self.call("check", minimum), 0)


if __name__ == "__main__":
    unittest.main()
