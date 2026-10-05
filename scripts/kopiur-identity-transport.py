#!/usr/bin/env python3
"""Run only on the assigned ARC runner. Credentials arrive via stdin, never argv.

Create tiny isolated encrypted repositories in each owned real backend, destroy
producer containers, restore in fresh containers, then delete only the UUID-owned
synthetic repositories. No production captures, policies or retained points change.
"""
import hashlib
import configparser
import json
import os
import re
import secrets
import shlex
import signal
import socket
import subprocess
import sys
import time
import uuid

IMAGE = "ghcr.io/home-operations/kopiur-mover@sha256:49d3c4cb6fce429bad8ec9f694f1f8bb5d00b791654d79429928e95db54f4b22"
ACCOUNT = "0834f4848c703f1fcf5b524bdf5f1722"
APPS = {"canary", "kometa", "listenarr", "tautulli", "sabnzbd", "wizarr", "homarr",
        "profilarr", "audiobookshelf", "seerr", "cwa-bdl", "changedetection",
        "radarr", "radarr-3d", "sonarr", "whisparr", "grimmory", "tubearchivist", "bazarr"}


def run(args, *, stdin=None, check=True, timeout=180.0):
    result = subprocess.run(args, input=stdin, capture_output=True, timeout=timeout)
    if check and result.returncode:
        raise RuntimeError("isolated identity command failed with exit " + str(result.returncode))
    return result


class Drill:
    def __init__(self, app, fields):
        self.app, self.fields = app, fields
        self.nonce = uuid.uuid4().hex
        self.path = "identity-fixtures/run7-" + self.nonce
        self.containers = []
        self.bytes = secrets.token_bytes(4096)
        self.deadline = time.monotonic() + 180

    def run(self, args, *, stdin=None, check=True, timeout=180):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeError("identity request exceeded absolute deadline")
        return run(args, stdin=stdin, check=check, timeout=min(timeout, remaining))

    def start(self):
        name = "k8s92-identity-" + self.nonce + "-" + str(len(self.containers))
        self.run(["docker", "run", "-d", "--name", name, "--label", "k8s92.identity=" + self.nonce,
             "--user", "568:568", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
             "--read-only", "--memory", "256m", "--cpus", "0.5", "--tmpfs",
             "/tmp:rw,nosuid,size=16m,mode=1777", "--tmpfs",
             "/work:rw,nosuid,size=192m,uid=568,gid=568,mode=0700", "--entrypoint", "/bin/sh",
             IMAGE, "-c", "sleep 900"])
        self.containers.append(name)
        return name

    def exec(self, container, body, *, password=None, check=True):
        f = self.fields
        config = f["NAS_RCLONE_CONFIG"] + ("\n[r2]\ntype = s3\nprovider = Cloudflare\n"
            "access_key_id = " + f["R2_ACCESS_KEY_ID"] + "\nsecret_access_key = " + f["R2_SECRET_ACCESS_KEY"] +
            "\nendpoint = https://" + ACCOUNT + ".r2.cloudflarestorage.com\nregion = auto\n")
        values = {"HOME": "/work", "KOPIA_PASSWORD": password or f["NAS_KOPIA_PASSWORD"],
                  "AWS_ACCESS_KEY_ID": f["R2_ACCESS_KEY_ID"], "AWS_SECRET_ACCESS_KEY": f["R2_SECRET_ACCESS_KEY"],
                  "RCLONE_CONFIG": "/work/rclone.conf"}
        values["TMPDIR"] = "/work/tmp"
        prefix = "set -eu\numask 077\n" + "\n".join("export " + k + "=" + shlex.quote(v) for k, v in values.items())
        prefix += "\nmkdir -p /work/tmp\n"
        prefix += "\nprintf '%s' " + shlex.quote(config) + " > /work/rclone.conf\n"
        prefix += "k() { kopia --config-file=/work/repository.config --log-dir=/work/log --no-check-for-updates \"$@\"; }\n"
        return self.run(["docker", "exec", "-i", container, "/bin/sh", "-s"],
                   stdin=(prefix + body).encode(), check=check)

    def remove(self, name):
        owner = self.run(["docker", "inspect", "--format", '{{ index .Config.Labels "k8s92.identity" }}', name]).stdout.decode().strip()
        assert owner == self.nonce, "container ownership changed"
        self.run(["docker", "rm", "-f", name])
        self.containers.remove(name)

    def backend(self, kind):
        if kind == "nas":
            return "rclone --remote-path=" + shlex.quote("mnemosyne:kopiur-" + self.app + "/" + self.path) + " --rclone-args=--config=/work/rclone.conf"
        return "s3 --bucket=kopiur-" + self.app + " --prefix=" + self.path + "/ --endpoint=" + ACCOUNT + ".r2.cloudflarestorage.com --region=auto"

    def exercise(self, kind):
        password = self.fields["NAS_KOPIA_PASSWORD" if kind == "nas" else "R2_KOPIA_PASSWORD"]
        producer = self.start()
        octal = "".join("\\%03o" % b for b in self.bytes)
        body = "mkdir -p /work/source\nprintf '%b' " + shlex.quote(octal) + " > /work/source/fixture.bin\n"
        body += "k repository create " + self.backend(kind) + " >/dev/null 2>/dev/null\n"
        body += "k snapshot create /work/source --json 2>/dev/null\n"
        result = self.exec(producer, body, password=password)
        snapshot = json.loads(result.stdout)
        assert isinstance(snapshot, dict)
        object_id = snapshot["rootEntry"]["obj"]
        assert re.fullmatch(r"[A-Za-z0-9]+", object_id), "unexpected snapshot object ID"
        if kind == "nas":
            guest_config = "[mnemosyne]\ntype = smb\nhost = 10.100.47.100\nuser = guest\npass =\ndomain = WORKGROUP\n"
            body = "printf '%s' " + shlex.quote(guest_config) + " > /work/guest.conf\n"
            body += "rclone --config=/work/guest.conf lsf mnemosyne:kopiur-" + self.app + " --max-depth=1\n"
            guest = self.exec(producer, body, check=False)
            assert guest.returncode and b"access_denied" in guest.stderr.lower(), "guest share denial not proven"
        self.remove(producer)
        wrong = self.start()
        denied = self.exec(wrong, "k repository connect " + self.backend(kind) + " >/dev/null\n",
                           password=secrets.token_hex(32), check=False)
        assert denied.returncode and b"invalid repository password" in denied.stderr.lower(), "password denial was not proven"
        self.remove(wrong)
        restorer = self.start()
        body = "k repository connect " + self.backend(kind) + " >/dev/null 2>/dev/null\n"
        body += "k snapshot restore " + object_id + " /work/restored >/dev/null 2>/dev/null\nsha256sum /work/restored/fixture.bin\n"
        result = self.exec(restorer, body, password=password)
        assert result.stdout.decode().split()[0] == hashlib.sha256(self.bytes).hexdigest(), "restored bytes differ"
        self.remove(restorer)
        return {"backend": kind, "snapshot_id": snapshot["id"], "object_id": object_id,
                "producer_removed_before_restore": True, "wrong_encryption_password_denied": True,
                "fresh_container_direct_restore": True, "bytes_equal": 4096,
                "guest_access_denied": True if kind == "nas" else None}

    def cleanup(self):
        # Terminate owned active exec processes before purging their repository.
        # Cleanup has its own short absolute budget even after request expiry.
        self.deadline = time.monotonic() + 90
        failures = []
        for container in list(self.containers):
            try:
                self.remove(container)
            except Exception:
                failures.append("owned-container-stop")
        cleanup = None
        try:
            cleanup = self.start()
            for remote in ("mnemosyne:kopiur-" + self.app + "/" + self.path,
                           "r2:kopiur-" + self.app + "/" + self.path):
                try:
                    root = remote.split("/", 1)[0]
                    command = "rclone --config=/work/rclone.conf lsf " + shlex.quote(root) + " --recursive --dirs-only 2>/dev/null\n"
                    listing = self.exec(cleanup, command)
                    if self.path + "/" in listing.stdout.decode().splitlines():
                        self.exec(cleanup, "rclone --config=/work/rclone.conf purge " + shlex.quote(remote) + " >/dev/null 2>/dev/null\n")
                    listing = self.exec(cleanup, command)
                    assert self.path + "/" not in listing.stdout.decode().splitlines(), "owned repository remains"
                except Exception:
                    failures.append("owned-repository-purge-readback")
        except Exception:
            failures.append("cleanup-container-start")
        finally:
            for container in list(self.containers):
                try:
                    self.remove(container)
                except Exception:
                    failures.append("owned-container-remove")
        if failures:
            raise RuntimeError("identity cleanup failed: " + ",".join(failures))


def exercise_payload(payload, deadline=None):
    app, fields = payload["app"], payload["fields"]
    assert app in APPS and fields["R2_BUCKET"] == "kopiur-" + app
    assert fields["NAS_USERNAME"] == "kp-" + app and fields["NAS_SHARE"] == "kopiur-" + app
    config = configparser.ConfigParser(interpolation=None)
    config.read_string(fields["NAS_RCLONE_CONFIG"])
    assert config.sections() == ["mnemosyne"]
    assert dict(config["mnemosyne"]) == {"type": "smb", "host": "10.100.47.100",
        "user": "kp-" + app, "domain": "WORKGROUP", "pass": config["mnemosyne"]["pass"]}
    assert config["mnemosyne"]["pass"]
    remaining = 600 if deadline is None else max(0.01, deadline - time.monotonic() - 90)
    run(["docker", "pull", "--platform", "linux/amd64", IMAGE], timeout=min(600, remaining))
    drill = Drill(app, fields)
    if deadline is not None:
        drill.deadline = min(drill.deadline, deadline - 90)
    try:
        results = [drill.exercise(kind) for kind in ("nas", "r2")]
    finally:
        drill.cleanup()
    return {"app": app, "runner": os.environ["RUNNER_NAME"], "mover_image": IMAGE,
            "results": results, "owned_fixture_repositories_removed": True}


def main():
    if sys.platform != "linux" or not os.environ.get("RUNNER_NAME"):
        raise RuntimeError("identity drill is restricted to an assigned Linux ARC runner")
    def interrupted(signum, frame):
        raise RuntimeError("identity drill interrupted by signal " + str(signum))
    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    if sys.argv[1:] != ["--serve"]:
        print(json.dumps(exercise_payload(json.load(sys.stdin))))
        return
    # A dedicated trusted-main ARC dispatch owns this private in-memory channel.
    # The operator resolves each approved vault item and sends it through stdin
    # to the exact allocated runner, never through GitHub secrets or disk files.
    directory = os.environ["RUNNER_TEMP"] + "/k8s92-identity-channel"
    os.mkdir(directory, 0o700)
    path = directory + "/socket"
    results = {}
    deadline = time.monotonic() + 1800
    with socket.socket(socket.AF_UNIX) as listener:
        listener.bind(path)
        os.chmod(path, 0o600)
        listener.listen(1)
        print(json.dumps({"identity_channel_ready": True, "runner": os.environ["RUNNER_NAME"],
                          "socket": path, "required_apps": len(APPS)}), flush=True)
        try:
            while len(results) < len(APPS):
                listener.settimeout(max(0.01, deadline - time.monotonic()))
                with listener.accept()[0] as connection:
                    receive_deadline = min(deadline - 90, time.monotonic() + 30)
                    data = bytearray()
                    while True:
                        connection.settimeout(max(0.01, receive_deadline - time.monotonic()))
                        if time.monotonic() >= receive_deadline:
                            raise RuntimeError("identity receive absolute deadline exceeded")
                        chunk = connection.recv(8192)
                        if not chunk:
                            break
                        data.extend(chunk)
                        if len(data) > 65536:
                            raise RuntimeError("identity request exceeds bound")
                    payload = json.loads(data)
                    assert payload["app"] in APPS and payload["app"] not in results
                    receipt = exercise_payload(payload, deadline)
                    results[payload["app"]] = receipt
                    connection.sendall(json.dumps(receipt).encode())
                    print(json.dumps(receipt), flush=True)
        finally:
            os.unlink(path)
            os.rmdir(directory)
    assert set(results) == APPS
    print(json.dumps({"identity_qualification_count": len(results), "all_passed": True}))


if __name__ == "__main__":
    main()
