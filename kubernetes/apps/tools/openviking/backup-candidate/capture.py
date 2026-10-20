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
        if not vectors:
            raise ValueError("dense vectors required for vector_mode=require recovery")
        return {"index_records": len(records), "dense_vectors": len(vectors)}


def private_roster(root):
    """Hash every private file, fail on links/special files and never hide read errors."""
    import stat
    result = {}
    if root.is_symlink() or not root.is_dir():
        raise ValueError("private system directory missing or linked")
    for path in sorted(root.rglob("*")):
        mode = path.lstat().st_mode
        if stat.S_ISDIR(mode):
            continue
        if not stat.S_ISREG(mode):
            raise ValueError("unsupported private system file")
        with path.open("rb") as stream:
            result[path.relative_to(root).as_posix()] = hashlib.file_digest(stream, "sha256").hexdigest()
    if not result:
        raise ValueError("empty private system is not identity recovery evidence")
    return result


def escrow_private(root, work, auth=Path("/auth/codex_auth.json")):
    source = root / "data/_system"
    before = private_roster(source)
    if auth.is_symlink() or not auth.is_file():
        raise ValueError("OAuth state missing or linked")
    original_auth = auth.read_bytes()
    json.loads(original_auth)
    shutil.copytree(source, work / "private_system")
    (work / "codex_auth.json").write_bytes(original_auth)
    if before != private_roster(source) or before != private_roster(work / "private_system"):
        raise ValueError("private system changed during online escrow")
    if auth.read_bytes() != original_auth:
        raise ValueError("OAuth state changed during online escrow")
    return {"private_system_sha256": before,
            "oauth_sha256": hashlib.sha256(original_auth).hexdigest()}


def capture(root=Path("/app/.openviking")):
    # Use the same app's existing scoped configuration, never another service token.
    config = os.environ["OPENVIKING_CONF_CONTENT"]
    parsed = json.loads(config)
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
            private_before = private_roster(root / "data/_system")
            request = Request(
                "http://127.0.0.1:1933/api/v1/pack/backup",
                data=b'{"include_vectors":true}', method="POST",
                headers={"Content-Type": "application/json",
                         "X-API-Key": parsed["server"]["root_api_key"]},
            )
            with urlopen(request, timeout=480) as response, (work / "state.ovpack").open("wb") as output:
                shutil.copyfileobj(response, output)
            vector_receipt = validate_pack(work / "state.ovpack")
            # Private system and refreshed OAuth bytes are required in the logical
            # bundle as well as their independently captured raw CSI sources.
            private_receipt = escrow_private(root, work)
            if private_before != private_receipt["private_system_sha256"]:
                raise ValueError("private system changed across native public capture")
            (work / "ov.conf").write_text(config)
            for name in ["ov.conf", "codex_auth.json", "state.ovpack"]:
                os.chmod(work / name, 0o600)
            with (work / "state.ovpack").open("rb") as handle:
                digest = hashlib.file_digest(handle, "sha256").hexdigest()
            (work / "capture.json").write_text(json.dumps({
                "format": "openviking-native-v1", "started": started,
                "completed": time.time(), "sha256": digest,
                "includes_vectors": True, "system_state_in_raw_csi_source": True,
                **vector_receipt, **private_receipt,
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
    except Exception:
        raise SystemExit("native OpenViking capture failed; details suppressed to protect credentials") from None
