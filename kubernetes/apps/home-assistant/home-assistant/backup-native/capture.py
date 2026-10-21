"""Download a completed native HA backup without restarting the application."""
import asyncio
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import tarfile
import time
import uuid


def member_name(member):
    name = member.name.removeprefix("./")
    if PurePosixPath(name).is_absolute() or ".." in PurePosixPath(name).parts:
        raise ValueError("unsafe native archive path")
    if not (member.isfile() or member.isdir()):
        raise ValueError("native archive link or special file requires explicit restore handling")
    return name


def validate_archive(path):
    with tarfile.open(path) as archive:
        members = archive.getmembers()
        names = [member_name(x) for x in members]
        if len(names) != len(set(names)):
            raise ValueError("duplicate native archive member")
        metadata_member = next((x for x in members if member_name(x) == "backup.json"), None)
        if metadata_member is None or not metadata_member.isfile():
            raise ValueError("missing native backup metadata")
        metadata = archive.extractfile(metadata_member)
        if metadata is None:
            raise ValueError("unreadable native metadata")
        data = json.load(metadata)
        if data.get("protected") or data.get("homeassistant", {}).get("exclude_database") is not True:
            raise ValueError("expected unprotected config archive with external database excluded")
        payload_name = "homeassistant.tar.gz" if data.get("compressed", True) else "homeassistant.tar"
        payload = next((x for x in members if member_name(x) == payload_name), None)
        if payload is None or not payload.isfile():
            raise ValueError("native config payload missing")
        names = set()
        with tarfile.open(fileobj=archive.extractfile(payload), mode="r|*") as inner:
            for member in inner:
                name = member_name(member)
                if name in names:
                    raise ValueError("duplicate native config member")
                names.add(name)
                if member.isfile():
                    handle = inner.extractfile(member)
                    if handle is None:
                        raise ValueError("unreadable native config member")
                    with handle:
                        while handle.read(1024 * 1024):
                            pass
        if not {"data/configuration.yaml", "data/.storage/auth"}.issubset(names):
            raise ValueError("native archive lacks configuration or original authentication state")


async def capture():
    import aiohttp
    root = Path("/capture")
    root.mkdir(mode=0o700, exist_ok=True)
    token = Path("/credentials/token").read_text().strip()
    base = "http://home-assistant:8123"
    # Recorder.lock_database returns immediately for PostgreSQL in the pinned image.
    # Activation requires proving the production recorder really uses PostgreSQL.
    if os.environ.get("RECORDER_BACKEND_QUALIFIED") != "postgresql":
        raise ValueError("non-pausing PostgreSQL recorder qualification required")
    name = "kopiur-" + uuid.uuid4().hex
    timeout = aiohttp.ClientTimeout(total=480)
    async with aiohttp.ClientSession(timeout=timeout) as client:
        async with client.ws_connect(base + "/api/websocket") as ws:
            if (await ws.receive_json())["type"] != "auth_required":
                raise ValueError("unexpected native auth handshake")
            await ws.send_json({"type": "auth", "access_token": token})
            if (await ws.receive_json())["type"] != "auth_ok":
                raise ValueError("native backup authentication failed")
            ident = 0
            async def command(kind, **fields):
                nonlocal ident
                ident += 1
                await ws.send_json({"id": ident, "type": kind, **fields})
                result = await ws.receive_json()
                if result.get("id") != ident or not result.get("success"):
                    raise ValueError("native backup command failed")
                return result.get("result")
            await command("backup/generate", agent_ids=["backup.local"],
                          include_database=False, include_homeassistant=True,
                          include_all_addons=False, name=name, password=None)
            deadline = time.monotonic() + 420
            backup = None
            while time.monotonic() < deadline:
                info = await command("backup/info")
                matches = [b for b in info["backups"] if b.get("name") == name]
                if info["state"] == "idle" and len(matches) == 1:
                    backup = matches[0]
                    break
                await asyncio.sleep(2)
            if backup is None:
                raise TimeoutError("native backup completion deadline")
            backup_id = backup["backup_id"]
        path = root / "homeassistant.tar.pending"
        async with client.get(base + "/api/backup/download/" + backup_id,
                              params={"agent_id": "backup.local"},
                              headers={"Authorization": "Bearer " + token}) as response:
            response.raise_for_status()
            with path.open("wb") as output:
                os.chmod(path, 0o600)
                received = 0
                async for chunk in response.content.iter_chunked(1024 * 1024):
                    received += len(chunk)
                    if received > 1024 * 1024 * 1024:
                        raise ValueError("native backup exceeds 1 GiB capture budget")
                    output.write(chunk)
        validate_archive(path)
        with path.open("rb") as handle:
            digest = hashlib.file_digest(handle, "sha256").hexdigest()
        path.replace(root / "homeassistant.tar")
        (root / "native.json").write_text(json.dumps({
            "format": "home-assistant-native-v1", "backup_id": backup_id,
            "sha256": digest, "completed": time.time(),
            "database": "external-postgresql", "cross_store_atomicity_qualified": False,
        }, sort_keys=True) + "\n")


if __name__ == "__main__":
    os.umask(0o077)
    try:
        asyncio.run(asyncio.wait_for(capture(), timeout=480))
    except Exception:
        raise SystemExit("native Home Assistant capture failed; details suppressed to protect credentials") from None
