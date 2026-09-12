#!/usr/bin/env python3
"""Synthetic fixtures for the Talos 1.14 config migration boundary."""
import importlib.machinery
import importlib.util
import json
import pathlib
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
APPEND = ROOT / "scripts/append-talos-config-documents"
loader = importlib.machinery.SourceFileLoader("guard", str(ROOT / "scripts/require-talos-config-version"))
spec = importlib.util.spec_from_loader(loader.name, loader)
assert spec is not None
guard = importlib.util.module_from_spec(spec)
loader.exec_module(guard)

BASE = """version: v1alpha1
machine:
  type: controlplane
---
apiVersion: v1alpha1
kind: KubeAdmissionControlConfig
name: PodSecurity
configuration:
  defaults: {enforce: baseline, warn: restricted, audit: restricted}
---
apiVersion: v1alpha1
kind: KubeAdmissionControlConfig
name: OtherPlugin
configuration: {fixture: preserved}
"""
EXTRA = "apiVersion: v1alpha1\nkind: EtcFileConfig\nname: fixture.conf\ncontents: fixture-data\n"


class AppendTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name)
        self.out = self.root / "out"
        self.out.mkdir()
        self.config = self.out / "node.yaml"
        self.config.write_text(BASE)
        self.extra = self.root / "extra.yaml"
        self.extra.write_text(EXTRA)

    def invoke(self, extra=None):
        return subprocess.run([str(APPEND), str(self.out), str(extra or self.extra)], text=True, capture_output=True)

    def test_only_generated_podsecurity_removed(self):
        result = self.invoke()
        self.assertEqual(result.returncode, 0, result.stderr)
        raw = subprocess.check_output(["yq", "ea", "-o=json", "-I=0", ".", str(self.config)], text=True)
        docs = [json.loads(line) for line in raw.splitlines() if line.strip()]
        self.assertEqual(len(docs), 3)
        self.assertEqual(docs[0]["machine"]["type"], "controlplane")
        self.assertEqual(docs[1]["name"], "OtherPlugin")
        self.assertEqual(docs[2]["name"], "fixture.conf")
        self.assertEqual(self.config.stat().st_mode & 0o777, 0o600)

    def test_reentry_fails_without_changes(self):
        self.assertEqual(self.invoke().returncode, 0)
        before = self.config.read_bytes()
        self.assertNotEqual(self.invoke().returncode, 0)
        self.assertEqual(self.config.read_bytes(), before)

    def test_nondefault_policy_refused(self):
        self.config.write_text(BASE.replace("enforce: baseline", "enforce: restricted"))
        before = self.config.read_bytes()
        self.assertNotEqual(self.invoke().returncode, 0)
        self.assertEqual(self.config.read_bytes(), before)

    def test_missing_document_refused(self):
        before = self.config.read_bytes()
        self.assertNotEqual(self.invoke(self.root / "missing.yaml").returncode, 0)
        self.assertEqual(self.config.read_bytes(), before)

    def test_empty_batch_refused(self):
        self.config.unlink()
        self.assertNotEqual(self.invoke().returncode, 0)


class VersionTests(unittest.TestCase):
    def test_server_not_client_controls_boundary(self):
        output = "Client:\nTalos v1.14.0\nServer:\n\tTag: v1.13.10\n"
        with self.assertRaises(ValueError):
            guard.require_server(output, "v1.14.0")

    def test_matching_server_passes(self):
        self.assertEqual(guard.require_server("Server:\n Tag: v1.14.1\n", "v1.14.0"), "v1.14.1")

    def test_multiple_or_missing_servers_refused(self):
        for output in ["Client:\nTalos v1.14.0\n", "Server:\n Tag: v1.14.0\n Tag: v1.14.0\n"]:
            with self.subTest(output=output), self.assertRaises(ValueError):
                guard.require_server(output, "v1.14.0")

    def test_prerelease_and_future_minor_refused(self):
        for tag in ["v1.14.0-beta.1", "v1.15.0"]:
            with self.subTest(tag=tag), self.assertRaises(ValueError):
                guard.require_server(f"Server:\n Tag: {tag}\n", "v1.14.0")


if __name__ == "__main__":
    unittest.main()
