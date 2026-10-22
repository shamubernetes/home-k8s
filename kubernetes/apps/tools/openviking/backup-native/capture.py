"""Online native OVPack capture. Executed in the existing OpenViking app."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
import time
from urllib.request import Request, urlopen
import zipfile


def validate_pack(path):
    from openviking.storage.ovpack.operations import (
        base_name_from_entries, read_manifest, validate_manifest_root_matches_zip,
        is_backup_package, validate_manifest_content, read_dense_vectors,
    )
    with zipfile.ZipFile(path) as archive:
        entries = archive.infolist()
        base = base_name_from_entries(entries)
        manifest = read_manifest(archive, base)
        validate_manifest_root_matches_zip(manifest, base)
        if not is_backup_package(manifest):
            raise ValueError("native backup package required, export packages are insufficient")
        records = validate_manifest_content(archive, manifest, entries, base)
        vectors = read_dense_vectors(archive, manifest, base, records)
        if records and not vectors:
            raise ValueError("dense vectors required for vector_mode=require recovery")
        return {"index_records": len(records), "dense_vectors": len(vectors)}


def capture_auth(work, auth=Path("/auth/codex_auth.json")):
    # The complete private system is captured by the ordinary CSI source.
    # Never copy live database files or require a global private-writer quiet
    # period across the native public export. Only the actual auth input is read.
    if auth.is_symlink() or not auth.is_file():
        raise ValueError("OAuth state missing or linked")
    original_auth = auth.read_bytes()
    json.loads(original_auth)
    (work / "codex_auth.json").write_bytes(original_auth)
    if auth.read_bytes() != original_auth:
        raise ValueError("OAuth state changed during online escrow")
    return {"oauth_sha256": hashlib.sha256(original_auth).hexdigest()}


def capture(root=Path("/app/.openviking")):
    # Use the same app's existing scoped configuration, never another service token.
    config = os.environ["OPENVIKING_CONF_CONTENT"]
    parsed = json.loads(config)
    identities = json.loads(os.environ["OV_BACKUP_ACCOUNTS_JSON"])
    if not identities:
        raise ValueError("native tenant backup identity required")
    if parsed["storage"]["workspace"] != str(root / "data"):
        raise ValueError("unexpected effective workspace")
    base = root / "kopiur-native"
    base.mkdir(mode=0o700, exist_ok=True)
    with (base / "capture.lock").open("a") as lock:
        import fcntl
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        # Only this helper owns pending-* beneath this private capture directory.
        # Exclusive ownership permits recovery after a killed prior hook.
        for stale in base.glob("pending-*"):
            if stale.is_dir() and not stale.is_symlink():
                shutil.rmtree(stale)
        if shutil.disk_usage(root).free < 256 * 1024 * 1024:
            raise OSError("insufficient capture headroom")
        work = Path(tempfile.mkdtemp(prefix="pending-", dir=base))
        try:
            started = time.time()
            private_system = root / "data/_system"
            if private_system.is_symlink() or not private_system.is_dir():
                raise ValueError("private system missing from actual CSI source")
            packages = {}
            for account, identity in sorted(identities.items()):
                filename = "state-" + hashlib.sha256(account.encode()).hexdigest()[:16] + ".ovpack"
                path = work / filename
                request = Request(
                    os.environ.get("OV_BACKUP_BASE_URL", "http://127.0.0.1:1933") + "/api/v1/pack/backup",
                    data=b'{"include_vectors":true}', method="POST",
                    headers={"Content-Type": "application/json",
                             "X-API-Key": identity["api_key"]},
                )
                with urlopen(request, timeout=480) as response, path.open("wb") as output:
                    shutil.copyfileobj(response, output)
                receipt = validate_pack(path)
                with path.open("rb") as handle:
                    digest = hashlib.file_digest(handle, "sha256").hexdigest()
                packages[account] = {"file": filename, "sha256": digest, **receipt}
            private_receipt = capture_auth(work)
            (work / "ov.conf").write_text(config)
            for name in ["ov.conf", "codex_auth.json"] + [item["file"] for item in packages.values()]:
                os.chmod(work / name, 0o600)
            (work / "capture.json").write_text(json.dumps({
                "format": "openviking-native-v2", "started": started,
                "completed": time.time(), "packages": packages,
                "index_records": sum(item["index_records"] for item in packages.values()),
                "dense_vectors": sum(item["dense_vectors"] for item in packages.values()),
                "includes_vectors": True, "system_state_in_raw_csi_source": True,
                **private_receipt,
                "models_in_separate_csi_source": "openviking-models",
                "cross_store_atomicity_qualified": False,
            }, sort_keys=True) + "\n")
            # Never select a half-written package. Keep the prior complete capture.
            previous = base / "previous"
            if previous.exists():
                shutil.rmtree(previous)
            if (base / "current").exists():
                (base / "current").rename(previous)
            work.rename(base / "current")
        finally:
            if work.exists():
                shutil.rmtree(work)


if __name__ == "__main__":
    os.umask(0o077)
    try:
        capture()
    except Exception as error:
        import traceback
        lines = [frame.lineno for frame in traceback.extract_tb(error.__traceback__)
                 if frame.filename == __file__]
        print(json.dumps({"native_capture_failed": type(error).__name__,
                          "source_lines": lines,
                          "http_status": getattr(error, "code", None),
                          "private_details_withheld": True}), flush=True)
        raise SystemExit(1) from None
