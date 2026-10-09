import runpy
from pathlib import Path
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
M = runpy.run_path(str(ROOT / "scripts/kopiur-radarr-transport.py"))

class TransportTests(unittest.TestCase):
    def fields(self):
        return {"NAS_RCLONE_CONFIG": "[mnemosyne]\ntype=smb\nhost=10.100.47.100\nuser=kp-radarr\npass=synthetic\ndomain=WORKGROUP\n",
                "NAS_KOPIA_PASSWORD": "synthetic", "NAS_USERNAME": "kp-radarr", "NAS_SHARE": "kopiur-radarr",
                "R2_KOPIA_PASSWORD": "synthetic", "R2_ACCESS_KEY_ID": "synthetic",
                "R2_SECRET_ACCESS_KEY": "synthetic", "R2_BUCKET": "kopiur-radarr"}

    def test_only_radarr_is_allowed(self):
        self.assertEqual(M["APPS"], {"radarr"})
        with self.assertRaises(AssertionError):
            M["exercise_payload"]({"app": "bazarr", "fields": self.fields()})

    def test_rejects_other_bucket_before_any_docker_call(self):
        fields = self.fields()
        fields["R2_BUCKET"] = "kopiur-bazarr"
        with self.assertRaises(AssertionError):
            M["exercise_payload"]({"app": "radarr", "fields": fields})

    def test_malformed_config_error_never_exposes_input(self):
        fields = self.fields()
        fields["NAS_RCLONE_CONFIG"] = "sensitive-invalid-config"
        with self.assertRaises(ValueError) as caught:
            M["exercise_payload"]({"app": "radarr", "fields": fields})
        self.assertNotIn("sensitive", str(caught.exception))
        self.assertTrue(caught.exception.__suppress_context__)

    def test_extra_credential_is_rejected(self):
        fields = self.fields()
        fields["OTHER_PASSWORD"] = "synthetic"
        with self.assertRaises(ValueError):
            M["exercise_payload"]({"app": "radarr", "fields": fields})

    def test_cleanup_is_scoped_and_verified(self):
        drill = M["Drill"]("radarr", self.fields())
        self.assertRegex(drill.path, r"^identity-fixtures/run7-[0-9a-f]{32}$")
        source = (ROOT / "scripts/kopiur-radarr-transport.py").read_text()
        self.assertIn('"owned_fixture_repositories_removed": True', source)
        self.assertIn('self.path + "/" not in listing.stdout.decode().splitlines()', source)
        self.assertIn('"producer_removed_before_restore": True', source)
        self.assertIn('"wrong_encryption_password_denied": True', source)
        self.assertNotIn("--privileged", source)
        self.assertNotIn("cgroup", source)

    def test_interruption_runs_bounded_drill_cleanup(self):
        globals_ = M["exercise_payload"].__globals__
        drill = M["Drill"]("radarr", self.fields())
        with patch.object(M["signal"], "signal") as signals, \
             patch.dict(globals_, {"run": lambda *a, **k: None, "Drill": lambda *a: drill}), \
             patch.object(drill, "exercise", side_effect=lambda kind: M["interrupted"](15, None)), \
             patch.object(drill, "cleanup") as cleanup:
            with self.assertRaisesRegex(RuntimeError, "interrupted"):
                M["exercise_payload"]({"app": "radarr", "fields": self.fields()})
            cleanup.assert_called_once_with()
            self.assertEqual(signals.call_count, 2)
        source = (ROOT / "scripts/kopiur-radarr-transport.py").read_text()
        self.assertLess(source.index('signal.signal(signal.SIGTERM, interrupted)'), source.index('os.mkdir(directory'))

    def test_normal_cleanup_ignores_signals_and_checks_both_backends(self):
        from types import SimpleNamespace
        drill = M["Drill"]("radarr", self.fields())
        with patch.object(M["signal"], "signal") as signals, \
             patch.object(drill, "start", return_value="owned"), \
             patch.object(drill, "exec", return_value=SimpleNamespace(stdout=b"")) as execute:
            drill.cleanup()
            self.assertEqual(signals.call_count, 2)
            self.assertEqual(execute.call_count, 4)
            self.assertIn("mnemosyne:kopiur-radarr", execute.call_args_list[0].args[1])
            self.assertIn("r2:kopiur-radarr", execute.call_args_list[2].args[1])

    def test_interrupted_volume_creation_is_cleaned(self):
        import tempfile
        from types import SimpleNamespace
        globals_ = M["main"].__globals__
        calls = []
        def fake_run(args, **kwargs):
            calls.append(args)
            if args[:3] == ["docker", "volume", "create"]:
                M["interrupted"](15, None)
            if args[:3] == ["docker", "volume", "inspect"]:
                nonce = globals_["TOOLS"].rsplit("-", 1)[1]
                return SimpleNamespace(returncode=0, stdout=nonce.encode(), stderr=b"")
            return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")
        with tempfile.TemporaryDirectory() as temporary, \
             patch.dict(M["os"].environ, {"RUNNER_NAME": "owned", "RUNNER_TEMP": temporary}), \
             patch.object(M["sys"], "platform", "linux"), \
             patch.object(M["sys"], "argv", ["script", "--serve"]), \
             patch.object(M["signal"], "signal"), patch.dict(globals_, {"run": fake_run}):
            with self.assertRaisesRegex(RuntimeError, "interrupted"):
                M["main"]()
            self.assertFalse((Path(temporary) / "k8s92-radarr-transport-channel").exists())
            self.assertTrue(any(args[:3] == ["docker", "volume", "rm"] for args in calls))

    def test_dispatch_stays_on_trusted_main(self):
        source = (ROOT / ".github/workflows/recovery-verify.yaml").read_text()
        self.assertIn("github.ref == 'refs/heads/main'", source)
        self.assertIn("persist-credentials: false", source)
        self.assertIn("inputs.suite == 'radarr-transport'", source)
        self.assertIn("KUBECONFIG: /dev/null", source)

if __name__ == "__main__":
    unittest.main()
