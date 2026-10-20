"""Validate a disposable original restore, never a production source mount."""
import argparse
import hashlib
import json
from pathlib import Path
import stat
import subprocess
import shutil
import tempfile


def roster(root):
    result = {}
    for name in ["conf", "work"]:
        parent = root / name
        if parent.is_symlink() or not parent.is_dir():
            raise ValueError("required conf/work tree missing or linked")
        for path in sorted(parent.rglob("*")):
            mode = path.lstat().st_mode
            if stat.S_ISDIR(mode):
                continue
            if not stat.S_ISREG(mode):
                raise ValueError("unsupported original state file")
            with path.open("rb") as stream:
                result[path.relative_to(root).as_posix()] = {
                    "sha256": hashlib.file_digest(stream, "sha256").hexdigest(),
                    "size": path.stat().st_size,
                }
    if "conf/AdGuardHome.yaml" not in result:
        raise ValueError("original native configuration missing")
    return result


def verify(root, binary):
    before = roster(root)
    # v0.107.79 --check-config can rewrite configuration. Never run it on the
    # original receipt tree, even when the caller mounted that tree read-write.
    with tempfile.TemporaryDirectory(prefix="adguard-native-check-") as tmp:
        check = Path(tmp) / "copy"
        shutil.copytree(root, check)
        if roster(check) != before:
            raise ValueError("disposable native check copy differs from original")
        subprocess.run([str(binary), "--check-config", "--config", str(check / "conf/AdGuardHome.yaml"),
                        "--work-dir", str(check / "work")], check=True, timeout=30,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if roster(root) != before:
        raise ValueError("configuration validation modified original restored bytes")
    return {"format": "adguard-original-check-v1", "file_count": len(before),
            "native_configuration_accepted": True,
            "native_dns_and_history_recovery_qualified": False,
            "tree": before}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--restore-root", type=Path, required=True)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    args = parser.parse_args()
    try:
        receipt = verify(args.restore_root.resolve(), args.binary.resolve())
        # Receipts contain private file names/hashes. Keep them inside private evidence.
        with args.receipt.open("x") as output:
            args.receipt.chmod(0o600)
            json.dump(receipt, output, sort_keys=True)
            output.write("\n")
    except Exception:
        raise SystemExit("AdGuard native original validation failed; private details suppressed") from None
