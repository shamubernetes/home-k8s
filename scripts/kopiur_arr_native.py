"""Pinned Prowlarr and Chaptarr native recovery adapters.

Use isolated, network-none PostgreSQL and disposable application credentials.
The complete multi-database set is required, including Chaptarr's cache database.
No production fencing, transport identity or retention approval is inferred.
"""
import hashlib
import json
from pathlib import Path
import runpy

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
        def app_ready(self, container):
            try:
                return super().app_ready(container)
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
            config = ('url = "http://127.0.0.1:' + str(self.port) + endpoint + '"\n'
                      'header = "X-Api-Key: ' + self.api_key + '"\n')
            response = scope["run"]("docker", "exec", "-i", container, "curl", "-fsS",
                                    "--config", "-", stdin=config.encode()).stdout
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
    native["run"]("docker", "pull", "--platform", "linux/amd64", native["PG_IMAGE"], timeout=600)
    native["run"]("docker", "pull", "--platform", "linux/amd64", CONTRACTS[app][0], timeout=600)
    results = []

    def exported(source, expected, original_key, visible):
        from kopiur_stateful_native import artifact_manifest
        manifest = artifact_manifest(source)
        proof = native["restore_pvc"](source, app, config_mib=256, database_mib=512,
                                       expected_fingerprints=expected, original_api_key=original_key,
                                       expected_application_state=visible)
        if not all(proof[key] for key in ("native_table_contents_equal", "original_fixture_identity_used",
                                          "application_visible_state_equal", "restored_app_ping")):
            raise ValueError("missing extended ARR recovery assertion")
        results.append(dict(proof, artifact_entries=len(manifest["entries"]),
                            producer_removed_before_restore=True, encrypted_transport_qualified=False,
                            production_recovery_accepted=False))

    native["fixture"](app, export=exported)
    if len(results) != 1:
        raise ValueError("native ARR fixture did not return one recovery proof")
    return results[0]


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app", choices=CONTRACTS, required=True)
    args = parser.parse_args()
    print(json.dumps(fixture(args.app), sort_keys=True))
