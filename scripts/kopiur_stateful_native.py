"""Native PostgreSQL application recovery through independently encrypted NAS/R2.

Called only by the owned trusted-main ARC identity channel. Service credentials
remain in memory. Repositories and resources are UUID-scoped nonproduction data.
This does not accept production CSI, shared media or original production key escrow.
"""
import hashlib
import io
import json
import os
from pathlib import Path
import re
import runpy
import secrets
import stat
import shutil
import tempfile
import tarfile
import time

from kopiur_shared import validate_artifact, generation_lineage, retention_contract

ROOT = Path(__file__).resolve().parents[1]


def artifact_manifest(root):
    entries = []
    for path in sorted(root.rglob("*")):
        info = path.lstat()
        if not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
            raise ValueError("unsafe native export file type")
        entry = {"path": path.relative_to(root).as_posix(), "kind": "file" if path.is_file() else "directory",
                 "uid": info.st_uid, "gid": info.st_gid, "mode": stat.S_IMODE(info.st_mode)}
        if path.is_file():
            with path.open("rb") as stream:
                entry.update(size=info.st_size, sha256=hashlib.file_digest(stream, "sha256").hexdigest())
        entries.append(entry)
    manifest = {"schema": "k8s92-artifact/v1", "entries": entries}
    validate_artifact(root, manifest)
    return manifest


def extract_export(data, destination):
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as archive:
        members = archive.getmembers()
        names = set()
        total = 0
        for member in members:
            path = Path(member.name)
            if (path.is_absolute() or ".." in path.parts or str(path) in names
                    or not (member.isfile() or member.isdir()) or member.size < 0):
                raise ValueError("unsafe native restored archive")
            names.add(str(path))
            total += member.size
        if not members or total > 192 * 1024 * 1024:
            raise ValueError("native restored archive exceeds bounded capacity")
        destination.mkdir(mode=0o700)
        # Keep fixture file permissions, never chown the ARC host or follow links.
        archive.extractall(destination, members=members, filter=lambda item, _: item.replace(
            uid=os.getuid(), gid=os.getgid(), uname="", gname=""))


def exercise(payload, transport, deadline):
    app = payload["app"]
    native = runpy.run_path(str(ROOT / "scripts/kopiur-postgres-drill"))
    if app == "grimmory":
        from kopiur_grimmory_native import contract
        native = contract()
    if app not in native["CONTRACTS"]:
        raise ValueError("native PostgreSQL application is not allowlisted")
    # Reuse the exact initialized tooling volume, not another credential receiver.
    transport.deadline = min(deadline - 90, time.monotonic() + 1500)
    for image in (native["PG_IMAGE"], native["CONTRACTS"][app][0]):
        transport.run(["docker", "pull", "--platform", "linux/amd64", image], timeout=600)
    results = []

    def exported(source, expected, api_key, visible):
        manifest = artifact_manifest(source)
        snapshots = []
        for kind in ("nas", "r2"):
            transport.stage = kind + "-native-capture"
            password = transport.fields["NAS_KOPIA_PASSWORD" if kind == "nas" else "R2_KOPIA_PASSWORD"]
            producer = transport.start()
            # Docker cp attempts daemon-owned writes into the read-only root.
            # Extract through the actual unprivileged writer into its tmpfs.
            archive = io.BytesIO()
            with tarfile.open(fileobj=archive, mode="w") as stream:
                stream.add(source, arcname=".")
            transport.run(["docker", "exec", producer, "/tools/busybox", "mkdir", "/work/source"])
            transport.run(["docker", "exec", "-i", producer, "/tools/busybox", "tar",
                           "-xof", "-", "-C", "/work/source"], stdin=archive.getvalue())
            result = transport.exec(producer, "k repository create " + transport.backend(kind) +
                                    " >/dev/null\nk snapshot create /work/source --json\n", password=password)
            snapshot = json.loads(result.stdout)
            object_id = snapshot["rootEntry"]["obj"]
            if not re.fullmatch(r"[A-Za-z0-9]+", object_id):
                raise ValueError("unsafe native snapshot object ID")
            snapshots.append((kind, password, snapshot, object_id))
            transport.remove(producer)
        # All application, database and producer containers have already gone.
        # Also delete the plaintext exported generation before either restore.
        shutil.rmtree(source)
        for kind, password, snapshot, object_id in snapshots:
            transport.stage = kind + "-native-password-denial"
            wrong = transport.start()
            denied = transport.exec(wrong, "k repository connect " + transport.backend(kind) + " >/dev/null\n",
                                     password=secrets.token_hex(32), check=False)
            if not denied.returncode or not any(x in denied.stderr.lower() for x in
                                                (b"invalid repository password", b"unable to decrypt")):
                raise ValueError("wrong native repository key was not denied")
            transport.remove(wrong)
            transport.stage = kind + "-native-restore"
            restorer = transport.start()
            transport.exec(restorer, "k repository connect " + transport.backend(kind) +
                           " >/dev/null\nk snapshot restore " + object_id + " /work/restored >/dev/null\n",
                           password=password)
            with tempfile.TemporaryDirectory(prefix="native-restored-", dir=Path.home() / ".hermes/cache/scratch") as scratch:
                restored = Path(scratch) / "generation"
                restored_tar = transport.run(["docker", "exec", restorer, "/tools/busybox", "tar",
                                              "-cf", "-", "-C", "/work/restored", "."]).stdout
                extract_export(restored_tar, restored)
                comparison = validate_artifact(restored, manifest)
                transport.remove(restorer)
                proof = native["restore_pvc"](restored, app, config_mib=256, database_mib=512,
                    expected_fingerprints=expected, original_api_key=api_key, expected_application_state=visible)
                if not all(proof[x] for x in ("native_table_contents_equal", "original_fixture_identity_used",
                                             "application_visible_state_equal", "restored_app_ping")):
                    raise ValueError("native acceptance assertions missing")
            results.append(dict(proof, backend=kind, snapshot_id=snapshot["id"], object_id=object_id,
                manifest_sha256=hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest(),
                artifact_comparison=comparison, producer_and_plaintext_source_removed=True,
                wrong_encryption_password_denied=True, fresh_native_restore=True))
        nas, r2 = results
        lineage = generation_lineage(app, [{"application": app, "source": "native-pg-and-config",
            "generation": transport.nonce, "nas": nas, "r2": dict(r2, source_nas_id=nas["snapshot_id"])}],
            ["native-pg-and-config"])
        # Fixture proof never supplies or fabricates production retention approval.
        results.append({"lineage": lineage, "retention": retention_contract({}),
                        "production_recovery_accepted": False})

    native["fixture"](app, export=exported)
    return {"app": app, "native_fixture_recovery": results, "production_recovery_accepted": False}
