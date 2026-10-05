"""Run only on authorized Linux ARC with the candidate checkout."""
import os
import io
import json
from types import SimpleNamespace
import tarfile
from pathlib import Path
import runpy
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import kopiur_stateful_native as native
import kopiur_grimmory_native as grimmory
from kopiur_shared import InvalidEvidence, validate_artifact


class NativeExportTests(unittest.TestCase):
    def test_full_tree_bytes_permissions_and_coverage(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "nested").mkdir()
            path = root / "nested/state"
            path.write_bytes(b"same-generation")
            path.chmod(0o600)
            manifest = native.artifact_manifest(root)
            self.assertEqual(validate_artifact(root, manifest)["entries_equal"], 2)
            path.write_bytes(b"wronggeneration")
            with self.assertRaises(InvalidEvidence):
                validate_artifact(root, manifest)
            path.write_bytes(b"same-generation")
            path.chmod(0o644)
            with self.assertRaises(InvalidEvidence):
                validate_artifact(root, manifest)
            path.chmod(0o600)
            (root / "extra").write_text("unlisted")
            with self.assertRaises(InvalidEvidence):
                validate_artifact(root, manifest)

    def test_symlink_denied(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "state").write_text("data")
            (root / "link").symlink_to("state")
            with self.assertRaises(ValueError):
                native.artifact_manifest(root)

    def test_repeated_owned_cleanup_is_idempotent(self):
        scope = runpy.run_path(str(native.ROOT / "scripts/kopiur-postgres-drill"))
        drill = scope["DockerDrill"]()
        drill.volumes = ["fixture-owned"]
        with patch.dict(scope["DockerDrill"].close.__globals__, run=lambda *a, **kw: type("Result", (), {"returncode": 0})()):
            drill.close()
            self.assertEqual(drill.volumes, [])
            drill.close()

    def test_native_allowlist_before_provider_write(self):
        with self.assertRaises(ValueError):
            native.exercise({"app": "not-a-native-app"}, None, 0)

    def test_bounded_regular_export_only(self):
        for name, kind in (("../escape", tarfile.REGTYPE), ("link", tarfile.SYMTYPE)):
            data = io.BytesIO()
            with tarfile.open(fileobj=data, mode="w") as archive:
                member = tarfile.TarInfo(name)
                member.type = kind
                archive.addfile(member)
            with tempfile.TemporaryDirectory() as temporary:
                destination = Path(temporary) / "restore"
                with self.assertRaises(ValueError):
                    native.extract_export(data.getvalue(), destination)
                self.assertFalse(destination.exists())

    def test_grimmory_request_timestamps_only_are_volatile(self):
        payload = {'status': 200, 'message': 'Pong', 'timestamp': 'request-1',
                   'data': {'status': 'UP', 'message': 'healthy', 'version': '3.5.0', 'timestamp': 'request-1'}}
        def docker(*args):
            return SimpleNamespace(stdout=json.dumps(payload))
        original = grimmory.visible_state('fixture', docker)
        payload['timestamp'] = payload['data']['timestamp'] = 'request-2'
        self.assertEqual(original, grimmory.visible_state('fixture', docker))
        payload['data']['version'] = 'different'
        self.assertNotEqual(original, grimmory.visible_state('fixture', docker))


if __name__ == "__main__":
    unittest.main()
