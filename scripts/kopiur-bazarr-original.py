#!/usr/bin/env python3
"""Read the two retained Bazarr originals on ARC, without modifying either backend.

Only dedicated Bazarr transport secrets arrive on the owned Unix socket. No
source DB credentials, Kubernetes config, public endpoint or artifact upload.
"""
import configparser
import hashlib
import json
import os
from pathlib import Path
import re
import runpy
import shlex
import signal
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
NATIVE = runpy.run_path(str(ROOT / "scripts/kopiur-postgres-drill"))
run = NATIVE["run"]
MOVER = "ghcr.io/home-operations/kopiur-mover@sha256:49d3c4cb6fce429bad8ec9f694f1f8bb5d00b791654d79429928e95db54f4b22"
TOOLS = "docker.io/library/busybox:1.37.0-musl@sha256:5cec3fc171c87218698e85a52af7087de727372aae264a787b8112901a5b0092"
ACCOUNT = "0834f4848c703f1fcf5b524bdf5f1722"
ORIGINALS = {"nas": "9a623d4c77684a42b970984ce9df7076", "r2": "c889e51bdf7ce94ee2f6b555e546842d"}
# One coherent production generation, matched through the copiedFrom source manifest.
RADARR_ORIGINALS = {"nas": "485dc5199b9575fa03551f0e6ffc1030", "r2": "2662d4ed229fd7b7751cebe634a390b1"}
RADARR_3D_ORIGINALS = {"nas": "4b974fb1e16ad236f02a98a32e7df660", "r2": "5ce5f1e5dfc49c707e15efd19f94ad6f"}
SONARR_ORIGINALS = {"nas": "fae589bd0c33f21731c9ef5f0ee6c4fa", "r2": "0b4d52a245eb24cc6421d0e96d4b2f20"}


def validate_fields(fields, app="bazarr"):
    if app not in ("bazarr", "radarr", "radarr-3d", "sonarr", "whisparr"):
        raise ValueError("unsupported original application")
    required = {"NAS_RCLONE_CONFIG", "NAS_KOPIA_PASSWORD", "R2_KOPIA_PASSWORD",
                "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET"}
    if set(fields) != required or not all(isinstance(v, str) and v for v in fields.values()):
        raise ValueError("invalid dedicated transport payload")
    if fields["R2_BUCKET"] != "kopiur-" + app:
        raise ValueError("destination is not the approved application bucket")
    config = configparser.ConfigParser(interpolation=None)
    try:
        config.read_string(fields["NAS_RCLONE_CONFIG"])
    except configparser.Error:
        raise ValueError("invalid NAS configuration") from None
    if config.sections() != ["mnemosyne"]:
        raise ValueError("unexpected NAS remotes")
    actual = dict(config["mnemosyne"])
    expected = {"type": "smb", "host": "10.100.47.100", "user": "kp-" + app,
                "pass": actual.get("pass"), "domain": "WORKGROUP"}
    if actual != expected or not actual["pass"]:
        raise ValueError("unexpected NAS identity")


def compare_results(results):
    left, right = (results[k] for k in ("nas", "r2"))
    for key in ("bundle_checksums_sha256", "metadata", "table_counts", "table_fingerprints",
                "api", "paired_filetree_bytes_equal"):
        if left[key] != right[key]:
            raise ValueError("independent originals differ: " + key)
    for result in results.values():
        if not result["native_restore"] or not result["restored_app_ping"] or \
                result["network"] != "none-shared-namespace" or \
                not result["paired_filetree_bytes_equal"]:
            raise ValueError("native original recovery is incomplete")


def extract_original(archive, destination, *, database="bazarr", capacity=2 * 1024**3):
    allowed = {".kopiur-postgres/COMPLETE", ".kopiur-postgres/current", ".kopiur-postgres/current/SHA256SUMS"}
    databases = (database,) if isinstance(database, str) else tuple(database)
    if databases not in (("bazarr",), ("radarr_main",), ("radarr_3d_main",), ("sonarr_main",),
                         ("whisparrv3_main", "whisparrv3_logs")):
        raise ValueError("unsupported native database transfer inventory")
    filenames = ["application-config", "application-state.tar", "filetree.sha256", "metadata"]
    filenames += [db + suffix for db in databases for suffix in (".dump", ".toc")]
    allowed.update(".kopiur-postgres/current/" + name for name in filenames)
    with tarfile.open(archive) as bundle:
        members = bundle.getmembers()
        if len(members) > len(allowed) or sum(m.size for m in members) > capacity:
            raise ValueError("original transfer exceeds existing restore capacity")
        seen = set()
        for member in members:
            name = member.name.rstrip("/")
            if name not in allowed or name in seen or not (member.isfile() or member.isdir()):
                raise ValueError("unexpected original transfer member")
            seen.add(name)
        if seen != allowed:
            raise ValueError("original transfer is incomplete")
        bundle.extractall(destination, filter="data")


def originals_for(app):
    if app == "whisparr":
        originals = {kind: os.environ.get("ORIGINAL_" + kind.upper() + "_SNAPSHOT", "") for kind in ("nas", "r2")}
        if not all(re.fullmatch(r"[0-9a-f]{32}", value) for value in originals.values()):
            raise ValueError("missing exact original Whisparr snapshot IDs")
        if originals["nas"] == originals["r2"]:
            raise ValueError("original Whisparr backends must have distinct manifests")
        return originals
    return {"bazarr": ORIGINALS, "radarr": RADARR_ORIGINALS, "radarr-3d": RADARR_3D_ORIGINALS,
            "sonarr": SONARR_ORIGINALS}[app]


def original_stage(backend, stage, operation, *args, **kwargs):
    """Print only fixed stage labels and elapsed time, never operation data."""
    started = time.monotonic()
    print(json.dumps({"original_stage": stage, "backend": backend, "state": "started"}), flush=True)
    state = "failed"
    try:
        result = operation(*args, **kwargs)
        state = "failed" if getattr(result, "returncode", 0) else "completed"
        return result
    finally:
        print(json.dumps({"original_stage": stage, "backend": backend, "state": state,
                          "elapsed_seconds": round(time.monotonic() - started, 3)}), flush=True)


def qualify(fields, app="bazarr"):
    validate_fields(fields, app)
    originals = originals_for(app)
    database = NATIVE["CONTRACTS"][app][1]
    if set(originals) != {"nas", "r2"}:
        raise ValueError("original generation is not retained on both backends")
    large_app = app in ("radarr", "sonarr", "whisparr")
    # Whisparr source PVC is 15Gi, with 12614Mi of original files observed.
    # Bound disposable capacity to that original tree plus bundle/cache overhead.
    capacity_mib = 16384 if app == "whisparr" else 8192 if large_app else 2048
    config_mib = 15360 if app == "whisparr" else 7168 if large_app else 1024
    nonce = uuid.uuid4().hex
    tools = "k8s92-bazarr-tools-" + nonce
    containers = []
    run("docker", "volume", "create", "--label", "k8s92.bazarr=" + nonce, tools)
    try:
        run("docker", "run", "--rm", "--network", "none", "--read-only", "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges", "--mount", "type=volume,src=" + tools + ",dst=/tools,volume-nocopy",
            TOOLS, "sh", "-c", "cp /bin/busybox /tools/busybox && /tools/busybox --install -s /tools")
        results = {}
        # The original Whisparr NAS bundle exceeded the small-reader 900s limit.
        # Bound only its read-only restore; production capture/hold is unchanged.
        reader_timeout = 1800 if app == "whisparr" else 900
        # The complete Whisparr bundle exceeded the 300s ARC-local tar transfer.
        # Reuse its bounded reader budget; production capture/hold is unchanged.
        transfer_timeout = reader_timeout if app == "whisparr" else 300
        with tempfile.TemporaryDirectory(prefix="bazarr-original-", dir=os.environ["RUNNER_TEMP"]) as temporary:
            for kind, snapshot in originals.items():
                name = "k8s92-bazarr-original-" + nonce + "-" + kind
                containers.append(name)
                run("docker", "run", "-d", "--name", name, "--label", "k8s92.bazarr=" + nonce,
                    "--user", "568:568", "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
                    "--memory", f"{capacity_mib + 1024 if large_app else capacity_mib}m", "--tmpfs", "/tmp:rw,nosuid,size=16m,mode=1777",
                    "--tmpfs", f"/work:rw,nosuid,size={capacity_mib}m,uid=568,gid=568,mode=0700",
                    "--mount", "type=volume,src=" + tools + ",dst=/tools,readonly,volume-nocopy",
                    "--entrypoint", "/tools/busybox", MOVER, "sleep",
                    str(reader_timeout + transfer_timeout + 600) if app == "whisparr" else "1800")
                values = {"HOME": "/work", "PATH": "/tools:/usr/local/bin:/usr/bin:/bin", "TMPDIR": "/work/tmp",
                          "KOPIA_PASSWORD": fields["NAS_KOPIA_PASSWORD" if kind == "nas" else "R2_KOPIA_PASSWORD"],
                          "KOPIA_CONFIG_PATH": "/work/repository.config", "KOPIA_LOG_DIR": "/work/log",
                          "KOPIA_CACHE_DIRECTORY": "/work/cache", "KOPIA_CHECK_FOR_UPDATES": "false"}
                body = "set -eu\numask 077\n" + "\n".join("export " + k + "=" + shlex.quote(v) for k, v in values.items())
                body += "\nmkdir /work/tmp\n"
                if kind == "nas":
                    body += "printf '%s' " + shlex.quote(fields["NAS_RCLONE_CONFIG"]) + " > /work/rclone.conf\n"
                    backend = "rclone --remote-path=mnemosyne:kopiur-" + app + " --rclone-args=--config=/work/rclone.conf"
                else:
                    body += "export AWS_ACCESS_KEY_ID=" + shlex.quote(fields["R2_ACCESS_KEY_ID"]) + "\n"
                    body += "export AWS_SECRET_ACCESS_KEY=" + shlex.quote(fields["R2_SECRET_ACCESS_KEY"]) + "\n"
                    backend = "s3 --bucket=kopiur-" + app + " --prefix=kopiur/" + app + "/r2/ --endpoint=" + ACCOUNT + ".r2.cloudflarestorage.com --region=auto"
                # Read-only connect prevents maintenance, snapshots or repository writes.
                # The Radarr bundle and Kopia's default cache compete for
                # the same 8Gi tmpfs. Bound only this reader's local cache.
                cache = "" if app == "bazarr" else (
                    " --content-cache-size-mb=64 --content-cache-size-limit-mb=128"
                    " --metadata-cache-size-mb=64 --metadata-cache-size-limit-mb=128"
                    " --content-min-sweep-age=0s --metadata-min-sweep-age=0s")
                body += "kopia repository connect " + backend + cache + " --readonly >/dev/null\n"
                if app == "bazarr":
                    body += "kopia snapshot restore " + snapshot + " /work/restored >/dev/null\n"
                else:
                    # Restore only the complete paired generation, not the redundant previous archive.
                    body += "mkdir -p /work/restored/.kopiur-postgres\n"
                    for entry in ("COMPLETE", "current"):
                        source_path = snapshot + "/.kopiur-postgres/" + entry
                        target_path = "/work/restored/.kopiur-postgres/" + entry
                        body += "kopia snapshot restore " + source_path + " " + target_path + " >/dev/null\n"
                result = original_stage(kind, "native-restore", run,
                             "docker", "exec", "-i", name, "/tools/busybox", "sh", "-s",
                             stdin=body.encode(), check=False, timeout=reader_timeout)
                if result.returncode:
                    # Never print restored application config or secret-bearing provider logs.
                    text = result.stderr.decode(errors="replace").lower()
                    categories = [c for c in ("unknown long flag", "accessdenied", "invalid repository password",
                                               "out of memory", "no space left on device") if c in text]
                    raise RuntimeError("original " + kind + " restore failed: " + json.dumps(categories))
                source = Path(temporary) / kind
                source.mkdir(mode=0o700)
                probe = run("docker", "exec", name, "/tools/busybox", "sh", "-c",
                    "test -d /work/restored/.kopiur-postgres && test -f /work/restored/.kopiur-postgres/COMPLETE",
                    check=False)
                if probe.returncode:
                    raise RuntimeError("original " + kind + " lacks the required complete capture bundle at PVC root")
                # Docker cp cannot archive a container's tmpfs. Keep the binary
                # stream entirely inside ARC, not through kubectl or Hermes.
                archive = source.parent / (kind + ".tar")
                with archive.open("wb") as stream:
                    copied = original_stage(kind, "archive-export", subprocess.run,
                        ["docker", "exec", name, "/tools/busybox", "tar",
                        "-C", "/work/restored", "-cf", "-", ".kopiur-postgres/COMPLETE", ".kopiur-postgres/current"],
                        stdout=stream, stderr=subprocess.PIPE, timeout=transfer_timeout)
                if copied.returncode:
                    raise RuntimeError("original " + kind + " transfer failed")
                original_stage(kind, "archive-validation-extraction", extract_original,
                                 archive, source, database=database,
                                 capacity=capacity_mib * 1024 * 1024)
                archive.unlink()
                # Destroy the only networked restorer before application boot.
                run("docker", "rm", "-fv", name)
                containers.remove(name)
                current = source / ".kopiur-postgres/current"
                checksum = hashlib.sha256((current / "SHA256SUMS").read_bytes()).hexdigest()
                metadata = (current / "metadata").read_text().splitlines()
                native_started = time.monotonic()

                def observe(stage):
                    print(json.dumps({"native_stage": stage, "backend": kind, "state": "entered",
                                      "elapsed_seconds": round(time.monotonic() - native_started, 3)}), flush=True)
                proof = original_stage(kind, "database-file-api-qualification", NATIVE["restore_pvc"],
                                            source, app, config_mib=config_mib, database_mib=4096,
                                            memory_mib=16384 if app == "whisparr" else 8192 if large_app else None,
                                            observe=observe)
                proof.update(snapshot_id=snapshot, bundle_checksums_sha256=checksum, metadata=metadata,
                             original_backend_readonly=True, networked_restorer_removed_before_boot=True)
                results[kind] = proof
        compare_results(results)
        return {"app": app, "candidate": os.environ["QUALIFICATION_COMMIT"],
                "runner": os.environ["RUNNER_NAME"], "results": results,
                "independent_original_native_recovery": True, "nas_r2_native_data_equal": True,
                "media_dependency_qualified": False, "legacy_retirement_authorized": False}
    finally:
        for name in containers:
            label = run("docker", "inspect", "-f", '{{ index .Config.Labels "k8s92.bazarr" }}', name).stdout.decode().strip()
            if label != nonce:
                raise RuntimeError("restorer ownership changed")
            run("docker", "rm", "-fv", name)
        run("docker", "volume", "rm", tools)


def main():
    arguments = sys.argv[1:]
    app = arguments[2] if len(arguments) == 3 and arguments[:2] == ["--serve", "--app"] else "bazarr"
    valid_arguments = ["--serve"] if app == "bazarr" else ["--serve", "--app", app]
    if app not in ("bazarr", "radarr", "radarr-3d", "sonarr", "whisparr") or sys.platform != "linux" or \
            not os.environ.get("RUNNER_NAME") or arguments != valid_arguments:
        raise RuntimeError("original recovery requires the assigned ARC dispatch")
    if not re.fullmatch(r"[0-9a-f]{40}", os.environ.get("QUALIFICATION_COMMIT", "")):
        raise ValueError("missing reviewed qualification commit")
    for image in (MOVER, TOOLS, NATIVE["PG_IMAGE"], NATIVE["CONTRACTS"][app][0]):
        run("docker", "pull", "--platform", "linux/amd64", image, timeout=600)
    def interrupted(signum, frame):
        raise RuntimeError("original qualification interrupted")
    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    directory = Path(os.environ["RUNNER_TEMP"]) / ("k8s92-" + app + "-original-channel")
    directory.mkdir(mode=0o700)
    path = str(directory / "socket")
    try:
        with socket.socket(socket.AF_UNIX) as listener:
            listener.bind(path)
            os.chmod(path, 0o600)
            listener.listen(1)
            listener.settimeout(900)
            print(json.dumps({"bazarr_original_channel_ready": True, "runner": os.environ["RUNNER_NAME"], "socket": path}), flush=True)
            with listener.accept()[0] as connection:
                deadline = time.monotonic() + 30
                data = bytearray()
                while True:
                    connection.settimeout(max(0.01, deadline - time.monotonic()))
                    chunk = connection.recv(8192)
                    if not chunk:
                        break
                    data.extend(chunk)
                    if len(data) > 65536 or time.monotonic() > deadline:
                        raise ValueError("transport payload exceeds bounds")
                receipt = qualify(json.loads(data), app)
                connection.sendall(json.dumps(receipt).encode())
                print(json.dumps(receipt), flush=True)
    finally:
        if os.path.exists(path):
            os.unlink(path)
        directory.rmdir()


if __name__ == "__main__":
    main()
