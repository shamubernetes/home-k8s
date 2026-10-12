#!/usr/bin/env python3
"""Exercise Grimmory's Retain mutation with the installed-version Kyverno CLI.

Run with KYVERNO_CLI pointing to Kyverno v1.18.2 and yq available on PATH.
No cluster access or third-party Python dependencies are used.
"""
import copy
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
POLICY = ROOT / "kubernetes/apps/kyverno/kyverno/policies/grimmory-r2-retain.yaml"
LABEL = "kopiur.home-operations.com/"
BASE = {
    "apiVersion": "kopiur.home-operations.com/v1alpha1",
    "kind": "Snapshot",
    "metadata": {
        "name": "retention-sample",
        "namespace": "media",
        "labels": {
            LABEL + "origin": "replicated",
            LABEL + "snapshot-replication": "grimmory-nas-to-r2",
            LABEL + "repository-uid": "3e49e730-4f33-4673-91ef-262e6b0bcbc5",
        },
    },
    "spec": {
        "repository": {"kind": "Repository", "name": "grimmory-r2", "namespace": "media"},
        "deletionPolicy": "Delete",
    },
}


class RetentionTests(unittest.TestCase):
    def apply(self, resource, operation="CREATE", expected="Retain"):
        binary = os.environ.get("KYVERNO_CLI", "kyverno")
        with tempfile.TemporaryDirectory(prefix="grimmory-retain-") as folder:
            source = Path(folder) / "source.yaml"
            output = Path(folder) / "result.yaml"
            source.write_text(json.dumps(resource))
            result = subprocess.run(
                [binary, "apply", str(POLICY), "--resource", str(source),
                 "--set", "request.operation=" + operation, "--output", str(output)],
                text=True, capture_output=True, timeout=30, check=True,
            )
            self.assertIn("error: 0", result.stdout)
            rendered = subprocess.check_output(
                ["yq", "-o=json", "select(.kind == \"Snapshot\")", str(output)],
                text=True, timeout=10,
            )
            if operation == "DELETE":
                # Admission DELETE has no new object for a mutation to return.
                self.assertEqual(rendered.strip(), "")
                self.assertIn("pass: 0, fail: 0", result.stdout)
                return
            value = json.loads(rendered)
            self.assertEqual(value["spec"]["deletionPolicy"], expected)
            before = copy.deepcopy(resource)
            before["spec"]["deletionPolicy"] = expected
            self.assertEqual(value, before, "Mutation changed more than deletionPolicy")

    def test_create_and_update_are_retained(self):
        for operation in ("CREATE", "UPDATE"):
            with self.subTest(operation=operation):
                self.apply(copy.deepcopy(BASE), operation)

    def test_delete_is_not_a_retirement_authorization(self):
        self.apply(copy.deepcopy(BASE), "DELETE", "Delete")

    def test_retained_original_is_unchanged(self):
        resource = copy.deepcopy(BASE)
        resource["spec"]["deletionPolicy"] = "Retain"
        self.apply(resource)

    def test_nonmatching_labels_are_unchanged(self):
        for key, wrong in (("origin", "scheduled"),
                           ("snapshot-replication", "another-nas-to-r2"),
                           ("repository-uid", "replacement-repository")):
            for value in (wrong, None):
                with self.subTest(key=key, value=value):
                    resource = copy.deepcopy(BASE)
                    if value is None:
                        del resource["metadata"]["labels"][LABEL + key]
                    else:
                        resource["metadata"]["labels"][LABEL + key] = value
                    self.apply(resource, expected="Delete")

    def test_other_namespace_is_unchanged(self):
        resource = copy.deepcopy(BASE)
        resource["metadata"]["namespace"] = "arrs"
        self.apply(resource, expected="Delete")

    def test_repository_contract_is_required(self):
        for key, wrong in (("kind", "ClusterRepository"),
                           ("name", "another-r2"), ("namespace", "arrs")):
            for value in (wrong, None):
                with self.subTest(key=key, value=value):
                    resource = copy.deepcopy(BASE)
                    if value is None:
                        del resource["spec"]["repository"][key]
                    else:
                        resource["spec"]["repository"][key] = value
                    self.apply(resource, expected="Delete")

    def test_missing_repository_is_unchanged(self):
        resource = copy.deepcopy(BASE)
        del resource["spec"]["repository"]
        self.apply(resource, expected="Delete")


if __name__ == "__main__":
    unittest.main()
