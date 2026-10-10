#!/usr/bin/env python3
"""Qualify recurring Radarr stop/capture/resume on the owned network-none ARC fixture."""
from pathlib import Path
import os
import sys
import tempfile
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/kopiur-radarr-recurring"
DRIVER = ROOT / "scripts/kopiur-radarr-recurring-capture"
CAPTURE = ROOT / "kubernetes/components/kopiur-postgres/capture.sh"


def qualify(drill, source, config, run, pg_image, restore_pvc):
    run(sys.executable, "-m", "unittest", "discover", "-s", str(ROOT / "scripts/tests"),
        "-p", "test_kopiur_radarr_recurring.py", "-v")
    driver = ROOT / "scripts/kopiur-sonarr-recurring-capture" if drill.app_name == "sonarr" else DRIVER
    state = drill.config_volume("recurring-state")
    app = drill.app("recurring-app", source, config, recurring_state=state)
    drill.healthy(app)
    helper = drill.start("recurring-exporter", pg_image,
                         user="568:568", network="container:" + source,
                         mounts=[(state, "/kopiur-quiescence", "rw"), (config, "/config", "rw"),
                                 (SCRIPT, "/kopiur/recurring.sh", "ro"),
                                 (driver, "/kopiur/recurring-capture.sh", "ro"),
                                 (CAPTURE, "/kopiur/capture.sh", "ro")],
                         env={"PGHOST": "127.0.0.1", "PGPORT": "5432", "PGUSER": drill.capture_user,
                              "PGPASSWORD": drill.backup_password, "PGDATABASES": drill.databases[0],
                              "PGSSLMODE": "disable", "CONFIG_FILE": "/config/config.xml",
                              "CAPTURE_MODE": "single-db-stable-filetree"}, command=["sleep", "infinity"])

    def control(*args, check=True):
        return run("docker", "exec", helper, "sh", "/kopiur/recurring.sh", *args, check=check)

    def ping():
        return run("docker", "exec", app, "curl", "-fsS", "--max-time", "2",
                   f"http://127.0.0.1:{drill.port}/ping", check=False).returncode

    def writers():
        # Read only comm names, never credential-bearing argv or app logs.
        raw = run("docker", "exec", app, "sh", "-ec",
                  'for file in /proc/[0-9]*/comm; do cat "$file" 2>/dev/null || true; done').stdout.decode().splitlines()
        return sum(name.lower() in ("radarr", "sonarr", "dotnet", "ffprobe", "ffmpeg") for name in raw)

    assert writers() > 0, "native process inventory did not identify Radarr"
    control("acquire")
    assert ping() != 0 and writers() == 0, "native writer survived the hold"
    assert control("acquire", check=False).returncode, "overlapping capture was admitted"
    control("release")
    drill.healthy(app)
    started = time.monotonic()
    result = run("docker", "exec", helper, "sh", "/kopiur/recurring-capture.sh", timeout=780, check=False)
    if result.returncode:
        raise RuntimeError("native recurring capture failed; sanitized helper stderr: " +
                           result.stderr.decode().replace(drill.backup_password, "[redacted]"))
    assert run("docker", "exec", helper, "sh", "-ec",
               "cd /config/.kopiur-postgres/current; sha256sum -c SHA256SUMS >/dev/null").returncode == 0
    drill.healthy(app)
    first_seconds = round(time.monotonic() - started, 2)
    # Exercise the newly captured generation after normal native startup has
    # resumed. Mutable later PVC bytes must never replace the paired archive.
    scratch = Path(os.environ.get("RUNNER_TEMP", str(Path.home() / ".hermes/cache/scratch")))
    scratch.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="recurring-paired-", dir=scratch) as temporary:
        copied = Path(temporary) / "config"
        run("docker", "cp", helper + ":/config", str(copied))
        (copied / "config.xml").write_bytes(b"later-unpaired-state")
        (copied / "k8s92-private-fixture.bin").write_bytes(b"later-unpaired-state")
        restored = restore_pvc(copied, drill.app_name, config_mib=256, database_mib=512)
        assert restored["native_restore"] and restored["restored_app_ping"]
        assert restored["table_counts"][drill.databases[0]]["kopiur_fixture"] == 1
        assert restored["paired_filetree_bytes_equal"] > 0

    # Database rejection releases the writer even though no COMPLETE is accepted.
    failed = run("docker", "exec", "-e", "PGPORT=1", helper,
                 "sh", "/kopiur/recurring-capture.sh", timeout=780, check=False)
    assert failed.returncode
    assert run("docker", "exec", helper, "test", "-e", "/config/.kopiur-postgres/COMPLETE", check=False).returncode
    drill.healthy(app)

    # Short fixture inputs exercise the same absolute expiry and elapsed cap.
    control("acquire", uuid.uuid4().hex, "30", "1")
    expiry = int(run("docker", "exec", helper, "cat", "/kopiur-quiescence/request").stdout.split()[-1])
    drill.healthy(app)
    assert int(time.time()) >= expiry
    assert control("check", "1", check=False).returncode
    control("acquire", uuid.uuid4().hex, "30", "1")
    expiry = int(run("docker", "exec", helper, "cat", "/kopiur-quiescence/request").stdout.split()[-1])
    generation = run("docker", "exec", helper, "cat", "/kopiur-quiescence/boot").stdout
    run("docker", "restart", "-t", "30", app)
    # A replacement cannot resume early or extend the original lease.
    assert ping() != 0
    drill.healthy(app)
    assert int(time.time()) >= expiry
    assert run("docker", "exec", helper, "cat", "/kopiur-quiescence/boot").stdout != generation
    assert control("check", "1", check=False).returncode
    run("docker", "stop", "-t", "30", app)
    return {"status": "passed", "production_data": False, "native_capture_seconds": first_seconds,
            "native_writer_stopped": True, "current_bundle_checksums": True,
            "overlap_rejected": True, "failure_resumed": True, "expiry_resumed": True,
            "restart_preserved_absolute_hold": True, "paired_generation_native_restore": True,
            "later_unpaired_state_rejected": True, "network": "none-shared-namespace"}
