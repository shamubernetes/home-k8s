#!/usr/bin/env python3
"""Fail-closed contract regressions for Bazarr's retained originals."""
import copy
import json
from pathlib import Path
import runpy
import unittest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/kopiur-bazarr-original.py"
M = runpy.run_path(str(SCRIPT))


class OriginalTests(unittest.TestCase):
    def fields(self):
        return {"NAS_RCLONE_CONFIG": "[mnemosyne]\ntype=smb\nhost=10.100.47.100\nuser=kp-bazarr\npass=synthetic\ndomain=WORKGROUP\n",
                "NAS_KOPIA_PASSWORD": "synthetic-nas", "R2_KOPIA_PASSWORD": "synthetic-r2",
                "R2_ACCESS_KEY_ID": "synthetic-id", "R2_SECRET_ACCESS_KEY": "synthetic-secret",
                "R2_BUCKET": "kopiur-bazarr"}

    def results(self):
        proof = {"bundle_checksums_sha256": "a" * 64, "metadata": ["format=2"],
                 "table_counts": {"bazarr": {"table_shows": 1}},
                 "table_fingerprints": {"bazarr": {"table_shows": "b" * 32}},
                 "api": {"series": {"total": 1, "representative_record_equal": True}},
                 "paired_filetree_bytes_equal": 2, "native_restore": True,
                 "restored_app_ping": True, "network": "none-shared-namespace"}
        return {k: copy.deepcopy(proof) for k in ("nas", "r2")}

    def test_only_existing_dedicated_bazarr_destinations(self):
        M["validate_fields"](self.fields())
        for key, value in (("R2_BUCKET", "other-bucket"), ("R2_ACCESS_KEY_ID", ""), ("EXTRA", "admin")):
            fields = self.fields()
            fields[key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                M["validate_fields"](fields)
        for before, after in (("kp-bazarr", "administrator"), ("10.100.47.100", "192.0.2.1"),
                              ("[mnemosyne]", "[unrelated]")):
            fields = self.fields()
            fields["NAS_RCLONE_CONFIG"] = fields["NAS_RCLONE_CONFIG"].replace(before, after)
            with self.subTest(after=after), self.assertRaises(ValueError):
                M["validate_fields"](fields)

    def test_independent_originals_require_identical_files_and_native_content(self):
        M["compare_results"](self.results())
        for key in ("bundle_checksums_sha256", "metadata", "table_counts", "table_fingerprints",
                    "api", "paired_filetree_bytes_equal"):
            results = self.results()
            results["r2"][key] = "different"
            with self.subTest(key=key), self.assertRaises(ValueError):
                M["compare_results"](results)

    def test_each_original_requires_native_boot_files_and_network_isolation(self):
        for key in ("native_restore", "restored_app_ping", "paired_filetree_bytes_equal", "network"):
            results = self.results()
            results["nas"][key] = results["r2"][key] = False
            with self.subTest(key=key), self.assertRaises(ValueError):
                M["compare_results"](results)

    def test_readonly_retained_ids_no_new_capture_or_pruning(self):
        self.assertEqual(M["ORIGINALS"], {"nas": "9a623d4c77684a42b970984ce9df7076",
                                        "r2": "c889e51bdf7ce94ee2f6b555e546842d"})
        text = SCRIPT.read_text()
        self.assertIn(" --readonly >/dev/null", text)
        self.assertNotIn("snapshot create", text)
        self.assertNotIn("rclone purge", text)
        self.assertNotIn("repository create", text)
        self.assertLess(text.index('containers.remove(name)'), text.index('NATIVE["restore_pvc"]'))
        self.assertIn('"media_dependency_qualified": False', text)
        self.assertIn('"legacy_retirement_authorized": False', text)

    def test_config_parser_never_echoes_secret_payload(self):
        import traceback
        fields = self.fields()
        fields["NAS_RCLONE_CONFIG"] = "SECRET_CANARY_WITHOUT_SECTION"
        try:
            M["validate_fields"](fields)
        except ValueError:
            output = traceback.format_exc()
        else:
            self.fail("malformed config accepted")
        self.assertNotIn("SECRET_CANARY", output)
        self.assertIn("invalid NAS configuration", output)

    def test_wrong_record_content_does_not_count_as_equality(self):
        check = M["NATIVE"]["validate_bazarr_api"]
        response = {"total": 1, "data": [{"sonarrSeriesId": 7, "title": "synthetic", "path": "/fixture"}]}
        native = lambda query: json.dumps({"title": "synthetic", "path": "/fixture"})
        self.assertTrue(check(response, 1, "table_shows", "sonarrSeriesId", native)["representative_record_equal"])
        for field in ("title", "path"):
            altered = copy.deepcopy(response)
            altered["data"][0][field] = "wrong"
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "record differs"):
                check(altered, 1, "table_shows", "sonarrSeriesId", native)
        with self.assertRaises(ValueError):
            check(response, 2, "table_shows", "sonarrSeriesId", native)
        with self.assertRaises(ValueError):
            check(response, 1, "table_shows", "sonarrSeriesId", lambda query: "")

    def test_tmpfs_transfer_has_an_exact_safe_inventory(self):
        import io
        import tarfile
        import tempfile
        names = [".kopiur-postgres/COMPLETE", ".kopiur-postgres/current", ".kopiur-postgres/current/SHA256SUMS"]
        names += [".kopiur-postgres/current/" + n for n in ("bazarr.dump", "bazarr.toc", "application-config", "application-state.tar", "filetree.sha256", "metadata")]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive = root / "transfer.tar"
            for bad in (None, "../escape", ".kopiur-postgres/current", "link"):
                with tarfile.open(archive, "w") as bundle:
                    for name in names + ([bad] if bad else []):
                        member = tarfile.TarInfo(name)
                        if name == ".kopiur-postgres/current":
                            member.type = tarfile.DIRTYPE
                        elif name == "link":
                            member.type = tarfile.SYMTYPE
                            member.linkname = "/etc/passwd"
                        else:
                            member.size = 1
                        bundle.addfile(member, io.BytesIO(b"x") if member.isfile() else None)
                if bad:
                    with self.subTest(bad=bad), self.assertRaises(ValueError):
                        M["extract_original"](archive, root / "restored")
                else:
                    M["extract_original"](archive, root / "restored")
                    self.assertEqual((root / "restored/.kopiur-postgres/COMPLETE").read_bytes(), b"x")

    def test_native_recovery_compares_representative_application_records(self):
        text = (ROOT / "scripts/kopiur-postgres-drill").read_text()
        self.assertIn('"table_fingerprints": fingerprints', text)
        self.assertIn('response["total"] != expected', text)
        self.assertIn('restored API record differs from native data', text)
        self.assertIn('target_info["HostConfig"]["NetworkMode"] != "none"', text)

    def test_original_suite_retains_trusted_dispatch_boundary(self):
        text = (ROOT / ".github/workflows/recovery-verify.yaml").read_text()
        self.assertIn('postgres-bazarr-original|postgres-radarr', text)
        self.assertIn("github.ref == 'refs/heads/main'", text)
        self.assertIn('persist-credentials: false', text)
        self.assertIn('QUALIFICATION_COMMIT: ${{ inputs.commit }}', text)
        self.assertIn('"arrs/${app%-original}"', text)
        self.assertNotIn('upload-artifact', text)
        self.assertNotIn('pull_request:', text)


if __name__ == "__main__":
    unittest.main()
