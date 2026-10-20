"""Download a completed native HA backup without restarting the application."""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import tarfile
import time
import uuid


def validate_archive(path):
    with tarfile.open(path) as archive:
        metadata = archive.extractfile("./backup.json")
        if metadata is None:
            raise ValueError("missing native backup metadata")
        data = json.load(metadata)
        if data.get("protected") or not data.get("homeassistant", {}).get("exclude_database"):
            raise ValueError("expected unprotected config archive with external database excluded")
        payload = next((x for x in archive if x.name.lstrip("./") == "homeassistant.tar.gz"), None)
        if payload is None:
            raise ValueError("native config payload missing")
        names = set()
        with tarfile.open(fileobj=archive.extractfile(payload), mode="r|gz") as inner:
            for member in inner:
                names.add(member.name.lstrip("./"))
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
                async for chunk in response.content.iter_chunked(1024 * 1024):
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
        asyncio.run(capture())
    except Exception:
        raise SystemExit("native Home Assistant capture failed; details suppressed to protect credentials") from None
