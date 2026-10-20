"""Authenticate and exercise isolated NAS/R2 AdGuard restores, never production."""
import base64
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import shutil
import socket
import stat
import struct
import subprocess
import tempfile
import time
import traceback
import urllib.request


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def roster(root):
    result = {}
    for tree in (root / "conf", root / "work"):
        if tree.is_symlink() or not tree.is_dir():
            raise ValueError("required original tree absent or linked")
        for path in [tree, *sorted(tree.rglob("*"))]:
            info = path.lstat()
            row = {"mode": stat.S_IMODE(info.st_mode), "uid": info.st_uid,
                   "gid": info.st_gid, "mtime_ns": info.st_mtime_ns}
            if stat.S_ISREG(info.st_mode):
                with path.open("rb") as stream:
                    row.update(size=info.st_size,
                               sha256=hashlib.file_digest(stream, "sha256").hexdigest())
            elif not stat.S_ISDIR(info.st_mode):
                raise ValueError("unsupported original inode")
            result[path.relative_to(root).as_posix()] = row
    if "conf/AdGuardHome.yaml" not in result:
        raise ValueError("original configuration absent")
    return result


def question_name(packet, offset):
    while True:
        size = packet[offset]
        offset += 1
        if size & 0xC0 == 0xC0:
            return offset + 1
        if size & 0xC0 or size > 63:
            raise ValueError("invalid DNS name")
        if not size:
            return offset
        offset += size
        if offset >= len(packet):
            raise ValueError("truncated DNS name")


def dns_probe(domain, expected):
    address = ipaddress.ip_address(expected)
    qtype = 1 if address.version == 4 else 28
    name = b"".join(bytes([len(label)]) + label for label in domain.encode("idna").split(b".")) + b"\0"
    transaction = int.from_bytes(os.urandom(2), "big")
    packet = struct.pack("!HHHHHH", transaction, 0x100, 1, 0, 0, 0) + name + struct.pack("!HH", qtype, 1)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as client:
        client.settimeout(5)
        client.sendto(packet, ("127.0.0.1", 15353))
        reply = client.recv(65535)
    ident, flags, questions, answers, _, _ = struct.unpack("!HHHHHH", reply[:12])
    if ident != transaction or not flags & 0x8000 or flags & 15:
        raise ValueError("native DNS returned invalid result")
    offset = 12
    for _ in range(questions):
        offset = question_name(reply, offset) + 4
    found = []
    for _ in range(answers):
        offset = question_name(reply, offset)
        kind, cls, _, length = struct.unpack("!HHIH", reply[offset:offset + 10])
        offset += 10
        data = reply[offset:offset + length]
        if len(data) != length:
            raise ValueError("truncated DNS answer")
        if kind == qtype and cls == 1:
            found.append(str(ipaddress.ip_address(data)))
        offset += length
    if str(address) not in found:
        raise ValueError("captured native DNS rewrite not served")


def native(root, binary, yq):
    config = json.loads(subprocess.check_output(
        [str(yq), "-o=json", ".", str(root / "conf/AdGuardHome.yaml")]))
    rewrites = config.get("filtering", {}).get("rewrites", [])
    candidates = []
    for row in rewrites:
        try:
            ipaddress.ip_address(row["answer"])
        except (ValueError, KeyError):
            continue
        candidates.append(row)
    if not candidates:
        raise ValueError("no captured address rewrite for native DNS proof")
    if config.get("tls", {}).get("enabled"):
        raise ValueError("captured TLS needs separately qualified isolated bindings")
    rewrite = candidates[0]
    domain = rewrite["domain"].removeprefix("*.")
    if rewrite["domain"].startswith("*."):
        domain = "recovery-proof." + domain
    credentials = (os.environ["ORIGIN_USERNAME"] + ":" + os.environ["ORIGIN_PASSWORD"]).encode()
    authorization = "Basic " + base64.b64encode(credentials).decode()

    def api(path):
        request = urllib.request.Request("http://127.0.0.1:13000/control/" + path,
                                         headers={"Authorization": authorization})
        with urllib.request.urlopen(request, timeout=10) as response:
            if response.status != 200:
                raise ValueError("native authenticated API rejected")
            return json.load(response)

    baseline = None
    with tempfile.TemporaryDirectory(prefix="adguard-native-", dir="/work") as tmp:
        target = Path(tmp) / "state"
        shutil.copytree(root / "conf", target / "conf")
        shutil.copytree(root / "work", target / "work")
        # Preserve captured state and auth. Change only disposable listener and
        # resolver bindings so this process cannot query production or the internet.
        def bindings(value):
            if isinstance(value, dict):
                return {key: bindings(item) for key, item in value.items()}
            if isinstance(value, list):
                return [bindings(item) for item in value]
            if isinstance(value, str):
                for name in ("conf", "work"):
                    prefix = "/opt/adguardhome/" + name
                    if value == prefix or value.startswith(prefix + "/"):
                        return str(target / name) + value[len(prefix):]
            return value

        config = bindings(config)
        if not isinstance(config, dict):
            raise ValueError("native configuration must be an object")
        config["http"]["address"] = "127.0.0.1:13000"
        config["dns"].update(bind_hosts=["127.0.0.1"], port=15353,
                             upstream_dns=["127.0.0.1:9"], fallback_dns=[], bootstrap_dns=[],
                             upstream_dns_file="", use_private_ptr_resolvers=False,
                             local_ptr_upstreams=[])
        (target / "conf/AdGuardHome.yaml").write_text(json.dumps(config))
        for boot in range(2):
            process = subprocess.Popen([str(binary), "--no-check-update", "--config",
                                        str(target / "conf/AdGuardHome.yaml"),
                                        "--work-dir", str(target / "work")],
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                deadline = time.monotonic() + 120
                while True:
                    if process.poll() is not None:
                        raise ValueError("isolated native process exited")
                    try:
                        status = api("status")
                        if status.get("running") is not True:
                            raise ValueError("native DNS not running")
                        break
                    except (OSError, ValueError):
                        if time.monotonic() >= deadline:
                            raise ValueError("isolated native API deadline") from None
                        time.sleep(1)
                if boot == 0:
                    history = api("querylog?limit=5")
                    statistics = api("stats")
                    profile = api("profile")
                    if not history.get("data") or statistics.get("num_dns_queries", 0) <= 0:
                        raise ValueError("captured nonempty native history was not consumed")
                    baseline = {"history": digest(history), "statistics": digest(statistics),
                                "profile": digest(profile), "version": status["version"],
                                "history_sample_count": len(history.get("data", [])),
                                "native_query_count": statistics["num_dns_queries"],
                                "statistics_fields": {key: digest(value) for key, value in statistics.items()},
                                "history_fields": {key: digest(value) for key, value in history.items()}}
                dns_probe(domain, rewrite["answer"])
            finally:
                process.terminate()
                try:
                    process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
    return baseline


def main():
    roots = {name: Path("/originals") / name for name in ("nas", "r2")}
    original = {name: roster(path) for name, path in roots.items()}
    if original["nas"] != original["r2"]:
        raise ValueError("independent restore bytes or metadata differ")
    config = original["nas"]["conf/AdGuardHome.yaml"]
    if config["mode"] != 0o600 or config["uid"] != 0 or config["gid"] != 0:
        raise ValueError("captured private configuration metadata changed")
    proofs = {}
    for name, path in roots.items():
        proofs[name] = native(path, Path("/native/AdGuardHome"), Path("/native/yq"))
        # Only aggregate counts and hashes, never a query, user or configuration.
        print(json.dumps({"native_backend": name, "result": proofs[name]}), flush=True)
    if proofs["nas"] != proofs["r2"]:
        changed = [key for key in proofs["nas"] if proofs["nas"][key] != proofs["r2"][key]]
        print(json.dumps({"native_data_mismatch_fields": changed}), flush=True)
        raise ValueError("independent native data differs")
    if {name: roster(path) for name, path in roots.items()} != original:
        raise ValueError("read-only originals changed")
    receipt = {"format": "adguard-independent-native-v1", "bytes_and_metadata_equal": True,
               "tree_digest": digest(original["nas"]), "entry_count": len(original["nas"]),
               "authenticated_native_history_statistics_profile": proofs,
               "captured_rewrite_served_on_both_initial_and_restarted_boots": True,
               "originals_unchanged": True}
    print(json.dumps(receipt, sort_keys=True), flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        lines = [frame.lineno for frame in traceback.extract_tb(error.__traceback__)
                 if frame.filename == __file__]
        print(json.dumps({"failed": True, "class": type(error).__name__, "source_lines": lines}), flush=True)
        raise SystemExit("isolated AdGuard native proof failed; private details suppressed") from None
