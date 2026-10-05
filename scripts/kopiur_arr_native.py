"""Pinned Prowlarr and Chaptarr native recovery adapters.

Use isolated, network-none PostgreSQL and disposable application credentials.
The complete multi-database set is required, including Chaptarr's cache database.
No production fencing, transport identity or retention approval is inferred.
"""
import hashlib
import json
from pathlib import Path
import runpy
import time

ROOT = Path(__file__).resolve().parents[1]
CONTRACTS = {
    "prowlarr": (
        "ghcr.io/home-operations/prowlarr:2.6.5.5623@sha256:6152751c3ea2e7751564f5952173d5e83eed0e09f3fabd2cb6bdb58690c39e2f",
        ["prowlarr_main"], "PROWLARR", 9696, "/config/config.xml"),
    "chaptarr": (
        "docker.io/chaptarr/chaptarr:0.9.965@sha256:bb00d67cbe1485d1cd61ef178362a29b42aa08f86b290841aab4b115ad2647a6",
        ["chaptarr_main", "chaptarr_log", "chaptarr_cache"], "CHAPTARR", 8789, "/config/config.xml"),
}
ENDPOINTS = {"prowlarr": "/api/v1/appprofile", "chaptarr": "/api/v1/qualityprofile"}


def contract():
    native = runpy.run_path(str(ROOT / "scripts/kopiur-postgres-drill"))
    scope = native["fixture"].__globals__
    scope["CONTRACTS"] = dict(native["CONTRACTS"], **CONTRACTS)
    base = native["DockerDrill"]

    class ArrDrill(base):
        def request(self, container, path, check=True, authenticated=False):
            config = f'url = "http://127.0.0.1:{self.port}{path}"\n'
            if authenticated:
                config += 'header = "X-Api-Key: ' + self.api_key + '"\n'
            if self.app_name == 'chaptarr':
                # HTTP tooling is separate from the pinned application image.
                # Sharing its network-none namespace cannot reach an incumbent.
                return scope['run']('docker', 'run', '--rm', '-i', '--read-only',
                                    '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges',
                                    '--network', 'container:' + container,
                                    '--entrypoint', 'curl', CONTRACTS['prowlarr'][0],
                                    '-fsS', '--max-time', '5', '--config', '-',
                                    stdin=config.encode(), check=check)
            return scope['run']('docker', 'exec', '-i', container, 'curl', '-fsS',
                                '--max-time', '5', '--config', '-', stdin=config.encode(), check=check)

        def healthy(self, container):
            try:
                for _ in range(120):
                    probe = self.request(container, '/ping', check=False)
                    if probe.returncode == 0:
                        return
                    state = scope['run']('docker', 'inspect', '-f', '{{.State.Running}}', container).stdout.strip()
                    if state != b'true':
                        raise RuntimeError('isolated ARR application exited before readiness')
                    time.sleep(1)
                raise RuntimeError('isolated ARR startup timed out')
            except RuntimeError:
                # Only this disposable fixture's logs. Never production logs.
                result = scope["run"]("docker", "logs", "--tail", "80", container, check=False)
                text = (result.stdout + result.stderr).decode("utf-8", errors="replace")
                text = text.replace(self.password, "[fixture-password]").replace(self.api_key, "[fixture-api-key]")
                print(text, flush=True)
                raise

        def app(self, name, database, config):
            prefix = self.env_prefix
            env = {
                f"{prefix}__POSTGRES__HOST": "127.0.0.1",
                f"{prefix}__POSTGRES__PORT": "5432",
                f"{prefix}__POSTGRES__USER": "app",
                f"{prefix}__POSTGRES__PASSWORD": self.password,
                f"{prefix}__POSTGRES__MAINDB": self.databases[0],
                f"{prefix}__SERVER__PORT": str(self.port),
                f"{prefix}__AUTH__APIKEY": self.api_key,
                f"{prefix}__AUTH__METHOD": "External",
                f"{prefix}__AUTH__REQUIRED": "DisabledForLocalAddresses",
                f"{prefix}__LOG__DBENABLED": "False",
            }
            if self.app_name == "chaptarr":
                env[f"{prefix}__POSTGRES__LOGDB"] = self.databases[1]
                env[f"{prefix}__POSTGRES__CACHEDB"] = self.databases[2]
            return self.start(name, self.image, network="container:" + database,
                              user="568:568", env=env, mounts=[(config, "/config", "rw")])

        def isolated_config(self, data):
            updated = super().isolated_config(data)
            if self.app_name == "chaptarr":
                # Base adapter rewrites MainDb and LogDb. CacheDb must not retain
                # an incumbent endpoint/name in the isolated recovery copy.
                import xml.etree.ElementTree as ET
                # Base isolated_config already bounded and rejected DTD/entities.
                tree = ET.fromstring(updated)
                for existing in tree.findall("PostgresCacheDb"):
                    tree.remove(existing)
                ET.SubElement(tree, "PostgresCacheDb").text = self.databases[2]
                updated = ET.tostring(tree, encoding="utf-8", xml_declaration=True)
            return updated

        def application_state(self, container):
            endpoint = ENDPOINTS[self.app_name]
            response = self.request(container, endpoint, authenticated=True).stdout
            value = json.loads(response)
            if not isinstance(value, list) or not value:
                raise ValueError("native ARR profile catalog is empty or malformed")
            return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    scope["DockerDrill"] = ArrDrill
    native.update(CONTRACTS=scope["CONTRACTS"], DockerDrill=ArrDrill)
    return native


def fixture(app):
    if app not in CONTRACTS:
        raise ValueError("unsupported extended ARR fixture")
    native = contract()
    from kopiur_native_fixture import exercise
    images = [CONTRACTS[app][0]]
    if app == 'chaptarr':
        images.append(CONTRACTS['prowlarr'][0])
    return exercise(native, app, images)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app", choices=CONTRACTS, required=True)
    args = parser.parse_args()
    print(json.dumps(fixture(args.app), sort_keys=True))
