"""Exercise the real capture archive boundary without production data or credentials."""
from pathlib import Path
import os
import shutil
import subprocess
import tempfile
import unittest

SOURCE = Path(__file__).with_name("recurring-capture.sh").read_text()
BLOCK = SOURCE[SOURCE.index("archive_result=0"):SOURCE.index('filetree_inventory >"$root/pending/filetree.after"')]
TAR = shutil.which("gtar") or shutil.which("tar")
assert TAR is not None, "GNU tar is required"


class ArchiveTests(unittest.TestCase):
    def exercise(self, diagnostic="", code=0, growing=False):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "config"
            root.mkdir()
            binary = Path(directory) / "bin"
            binary.mkdir()
            (binary / "tar").symlink_to(TAR)
            (root / "pending").mkdir()
            (root / "logs").mkdir()
            (root / "logs/app.txt").write_text("retained diagnostic bytes" * (200000 if growing else 1))
            (root / "state").write_text("state")
            if growing:
                # GNU tar really sees a diagnostic log grow while archiving it.
                wrapper = 'tar() { command tar --checkpoint=1 --checkpoint-action="exec=printf x >> $root/logs/app.txt" "$@"; }\n'
            else:
                # Write a real readable archive, then inject only the failure boundary.
                wrapper = 'tar() { command tar "$@"; printf "%s\\n" "$DIAGNOSTIC" >&2; return "$CODE"; }\n'
            script = 'set -eu\nroot="$FIXTURE_ROOT"\n' + wrapper + BLOCK.replace('--directory=/config', '--directory="$root"').replace("--exclude='./.kopiur-postgres'", "--exclude='./pending'")
            result = subprocess.run(["sh", "-c", script], capture_output=True, text=True,
                                    env=dict(os.environ, PATH=str(binary) + os.pathsep + os.environ["PATH"], FIXTURE_ROOT=str(root), DIAGNOSTIC=diagnostic, CODE=str(code)))
            if result.returncode == 0:
                archived = subprocess.run([TAR, "-xOf", str(root / "pending/application-state.tar"), "./logs/app.txt"], capture_output=True, check=True)
                self.assertTrue(archived.stdout.startswith(b"retained diagnostic bytes"))
                if growing:
                    self.assertIn("diagnostic_log_growth_during_archive=", (root / "pending/metadata").read_text())
            return result

    def test_actual_concurrent_log_growth(self):
        result = self.exercise(growing=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_exact_diagnostic_growth(self):
        self.assertEqual(self.exercise("tar: ./logs/app.txt: file changed as we read it", 1).returncode, 0)

    def test_application_state_mutation_rejected(self):
        self.assertNotEqual(self.exercise("tar: ./state: file changed as we read it", 1).returncode, 0)

    def test_permission_error_rejected(self):
        self.assertNotEqual(self.exercise("tar: ./logs/app.txt: Cannot open: Permission denied", 2).returncode, 0)

    def test_mixed_errors_rejected(self):
        self.assertNotEqual(self.exercise("tar: ./logs/app.txt: file changed as we read it\ntar: ./state: Cannot open: Permission denied", 1).returncode, 0)

    def test_non_text_log_rejected(self):
        self.assertNotEqual(self.exercise("tar: ./logs/app.db: file changed as we read it", 1).returncode, 0)

    def test_unexplained_exit_rejected(self):
        self.assertNotEqual(self.exercise("", 1).returncode, 0)

    def test_fatal_code_rejected(self):
        self.assertNotEqual(self.exercise("tar: ./logs/app.txt: file changed as we read it", 2).returncode, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
