"""Fail closed on inventory, downloaded byte, native DB-reference or key defects."""
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import stat
import time


def object_path(root, key):
    path = PurePosixPath(key)
    if not key or path.is_absolute() or any(x in {"", ".", ".."} for x in key.split("/")):
        raise ValueError("unsafe object key")
    result = root
    for part in path.parts:
        result = result / part
        if result.is_symlink():
            raise ValueError("object path contains a link")
    return result


def inventory(path):
    rows = json.loads(path.read_text())
    if rows is None:  # AWS list-objects-v2 query on an empty bucket.
        rows = []
    result = {}
    for row in rows:
        key, size, etag = row["Key"], row["Size"], row["ETag"]
        if key in result or not isinstance(size, int) or size < 0:
            raise ValueError("invalid object inventory")
        # Multipart/encrypted ETags do not prove downloaded byte equality.
        if not re.fullmatch(r'"[0-9a-f]{32}"', etag):
            raise ValueError("unsupported ETag, conditional/checksummed capture required")
        result[key] = (size, etag.strip('"'))
    return result


def validate(root):
    before = inventory(root / "objects-before.json")
    after = inventory(root / "objects-after.json")
    if before != after:
        raise ValueError("objects changed across native database export")
    required = json.loads((root / "required-objects.json").read_text())
    if not isinstance(required, list) or not all(isinstance(x, str) for x in required):
        raise ValueError("invalid restored database references")
    if not set(required).issubset(before):
        raise ValueError("restored database references a missing object")
    hashes = {}
    for key, (size, etag) in before.items():
        path = object_path(root / "rgw", key)
        if not stat.S_ISREG(path.stat().st_mode) or path.stat().st_size != size:
            raise ValueError("missing or truncated downloaded object")
        with path.open("rb") as stream:
            if hashlib.file_digest(stream, "md5").hexdigest() != etag:
                raise ValueError("downloaded object differs from observed ETag")
        with path.open("rb") as stream:
            hashes[key] = hashlib.file_digest(stream, "sha256").hexdigest()
    actual = set()
    if (root / "rgw").exists():
        for path in (root / "rgw").rglob("*"):
            if path.is_symlink() or not (path.is_dir() or path.is_file()):
                raise ValueError("unsupported downloaded file type")
            if path.is_file():
                actual.add(path.relative_to(root / "rgw").as_posix())
    if actual != set(before):
        raise ValueError("downloaded object inventory differs")
    key = root / "ENCRYPTION_KEY"
    if key.is_symlink() or not key.is_file() or not key.stat().st_size or key.stat().st_mode & 0o077:
        raise ValueError("missing or non-private original encryption key")
    return {"format": "pocket-id-native-v2", "completed": time.time(),
            "object_sha256": hashes, "required_object_count": len(required),
            "restored_database_references_equal": True,
            "original_key_native_auth_qualified": False,
            "cross_store_atomicity_qualified": False}


if __name__ == "__main__":
    import os
    os.umask(0o077)
    root = Path("/capture/pending")
    try:
        receipt = validate(root)
        (root / "capture.json").write_text(json.dumps(receipt, sort_keys=True) + "\n")
    except Exception:
        raise SystemExit("Pocket ID native bundle validation failed; private details suppressed") from None
