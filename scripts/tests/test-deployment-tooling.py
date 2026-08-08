#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path


class DeploymentToolingTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.repo = Path(__file__).resolve().parents[2]

    def executable(self, path: Path, content: str) -> Path:
        path.write_text(content)
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
        return path

    def test_validate_yaml_routes_app_roots_to_validate_app(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            temp = Path(raw)
            called = temp / "called"
            validate = self.executable(
                temp / "validate-app",
                f"#!/bin/sh\nprintf '%s' \"$*\" > {called}\n",
            )
            result = subprocess.run(
                [str(self.repo / "scripts/validate-yaml"), "kubernetes/apps/arrs/chaptarr"],
                cwd=self.repo,
                env={
                    **os.environ,
                    "VALIDATE_APP_BIN": str(validate),
                    "VALIDATE_YAML_APP_MODE": "online",
                },
                text=True,
                capture_output=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(called.read_text(), "arrs/chaptarr")
            self.assertIn("detected GitOps app", result.stderr)

    def test_validate_yaml_falls_back_offline_when_cluster_is_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            temp = Path(raw)
            called = temp / "called"
            validate = self.executable(
                temp / "validate-app",
                f"#!/bin/sh\nprintf '%s' \"$*\" > {called}\n",
            )
            self.executable(temp / "kubectl", "#!/bin/sh\nexit 1\n")
            result = subprocess.run(
                [str(self.repo / "scripts/validate-yaml"), "kubernetes/apps/arrs/chaptarr"],
                cwd=self.repo,
                env={
                    **os.environ,
                    "PATH": f"{temp}:{os.environ['PATH']}",
                    "VALIDATE_APP_BIN": str(validate),
                },
                text=True,
                capture_output=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(called.read_text(), "--offline arrs/chaptarr")
            self.assertIn("cluster unavailable", result.stderr)

    def test_validate_yaml_explains_yayamlls_201(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            fake = self.executable(Path(raw) / "yayamlls", "#!/bin/sh\nexit 201\n")
            result = subprocess.run(
                [
                    str(self.repo / "scripts/validate-yaml"),
                    "kubernetes/flux/vars/volsync-schedules.yaml",
                ],
                cwd=self.repo,
                env={**os.environ, "YAYAMLLS_BIN": str(fake)},
                text=True,
                capture_output=True,
            )
            self.assertEqual(result.returncode, 201)
            self.assertIn("schema validation failed or the resource schema is unsupported", result.stderr)

    def test_new_app_preflight_resolves_image_and_records_source_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            temp = Path(raw)
            source = temp / "source"
            source.mkdir()
            (source / "compose.yaml").write_text(
                "services:\n  app:\n    ports: ['8789:8789']\n    volumes: ['/config:/config']\n"
                "    environment: ['POSTGRES_HOST=db', 'HEALTH_PATH=/ping']\n"
            )
            pin = self.executable(
                temp / "pin-image",
                "#!/bin/sh\nprintf '%s\\n' 'ghcr.io/example/app@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'\n",
            )
            crane = self.executable(
                temp / "crane",
                """#!/bin/sh
if [ "$1" = manifest ]; then
  printf '%s\n' '{"manifests":[{"platform":{"os":"linux","architecture":"amd64"}},{"platform":{"os":"linux","architecture":"arm64"}}]}'
else
  printf '%s\n' '{"config":{"Entrypoint":["/entrypoint"],"Cmd":["serve"],"ExposedPorts":{"8789/tcp":{}},"Volumes":{"/config":{}},"Env":["POSTGRES_HOST=db"]}}'
fi
""",
            )
            result = subprocess.run(
                [
                    str(self.repo / "scripts/preflight-new-app"),
                    "arrs/example",
                    "--image",
                    "ghcr.io/example/app:1.0.0",
                    "--port",
                    "8789",
                    "--health-path",
                    "/ping",
                    "--persistence",
                    "/config",
                    "--database",
                    "postgres",
                    "--auth",
                    "split-api",
                    "--source",
                    str(source),
                    "--strict-evidence",
                    "--json",
                ],
                cwd=self.repo,
                env={**os.environ, "PIN_IMAGE_BIN": str(pin), "CRANE_BIN": str(crane)},
                text=True,
                capture_output=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            document = json.loads(result.stdout)
            self.assertIn(":1.0.0@sha256:", document["image"])
            self.assertEqual(document["imageMetadata"]["platforms"], ["linux/amd64", "linux/arm64"])
            self.assertEqual(document["runtime"]["persistence"], ["/config"])
            self.assertFalse(document["warnings"])
            self.assertIn("--health-path /ping", document["scaffoldCommand"])

    def test_new_app_preflight_strict_mode_rejects_missing_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            temp = Path(raw)
            source = temp / "source"
            source.mkdir()
            (source / "README.md").write_text("no runtime contract here\n")
            crane = self.executable(
                temp / "crane",
                "#!/bin/sh\nprintf '%s\\n' '{\"config\":{}}'\n",
            )
            image = "ghcr.io/example/app:1.0.0@sha256:" + "b" * 64
            result = subprocess.run(
                [
                    str(self.repo / "scripts/preflight-new-app"),
                    "tools/example",
                    "--image",
                    image,
                    "--port",
                    "8080",
                    "--health-path",
                    "/health",
                    "--persistence",
                    "none",
                    "--database",
                    "none",
                    "--auth",
                    "internal",
                    "--source",
                    str(source),
                    "--strict-evidence",
                    "--json",
                ],
                cwd=self.repo,
                env={**os.environ, "CRANE_BIN": str(crane)},
                text=True,
                capture_output=True,
            )
            self.assertEqual(result.returncode, 2)
            document = json.loads(result.stdout)
            self.assertTrue(any("port 8080" in warning for warning in document["warnings"]))

    def test_preflight_strict_evidence_rejects_incidental_substrings(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            source = Path(raw)
            (source / "compose.yaml").write_text(
                "PORT=18789\nHEALTH=/pingable\nVOLUME=/configuration\nDB=postgrestate\n"
            )
            image = "ghcr.io/example/app:1.0.0@sha256:" + "c" * 64
            result = subprocess.run(
                [
                    str(self.repo / "scripts/preflight-new-app"),
                    "tools/example",
                    "--image",
                    image,
                    "--port",
                    "8789",
                    "--health-path",
                    "/ping",
                    "--persistence",
                    "/config",
                    "--database",
                    "postgres",
                    "--auth",
                    "internal",
                    "--source",
                    str(source),
                    "--strict-evidence",
                ],
                cwd=self.repo,
                text=True,
                capture_output=True,
            )
            self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
            self.assertIn("port 8789", result.stderr)
            self.assertIn("health path /ping", result.stderr)
            self.assertIn("persistence path /config", result.stderr)
            self.assertIn("database postgres", result.stderr)

    def test_preflight_strict_evidence_requires_source(self) -> None:
        image = "ghcr.io/example/app:1.0.0@sha256:" + "d" * 64
        result = subprocess.run(
            [
                str(self.repo / "scripts/preflight-new-app"),
                "tools/example",
                "--image",
                image,
                "--port",
                "8080",
                "--health-path",
                "/health",
                "--persistence",
                "none",
                "--database",
                "none",
                "--auth",
                "internal",
                "--strict-evidence",
            ],
            cwd=self.repo,
            text=True,
            capture_output=True,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("requires --source", result.stderr)

    def test_new_app_scaffold_uses_current_oci_chart_and_envoy_route(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            category = root / "kubernetes/apps/tools"
            category.mkdir(parents=True)
            (category / "kustomization.yaml").write_text(
                "apiVersion: kustomize.config.k8s.io/v1beta1\nkind: Kustomization\nresources:\n"
            )
            digest = "a" * 64
            undecided = subprocess.run(
                [
                    str(self.repo / "scripts/new-app"),
                    "tools",
                    "example",
                    "--image",
                    f"ghcr.io/example/app:1.0.0@sha256:{digest}",
                    "--port",
                    "8789",
                ],
                cwd=root,
                text=True,
                capture_output=True,
            )
            self.assertNotEqual(undecided.returncode, 0)
            self.assertIn("choose --health-path", undecided.stderr)
            mutable = subprocess.run(
                [
                    str(self.repo / "scripts/new-app"),
                    "tools",
                    "example",
                    "--image",
                    "ghcr.io/example/app:1.0.0",
                    "--port",
                    "8789",
                    "--health-path",
                    "/ping",
                ],
                cwd=root,
                text=True,
                capture_output=True,
            )
            self.assertNotEqual(mutable.returncode, 0)
            self.assertIn("immutable image:tag@sha256", mutable.stderr)
            untagged = subprocess.run(
                [
                    str(self.repo / "scripts/new-app"),
                    "tools",
                    "example",
                    "--image",
                    f"ghcr.io/example/app@sha256:{digest}",
                    "--port",
                    "8789",
                    "--health-path",
                    "/ping",
                ],
                cwd=root,
                text=True,
                capture_output=True,
            )
            self.assertNotEqual(untagged.returncode, 0)
            self.assertIn("must preserve its tag", untagged.stderr)
            self.assertFalse((category / "example").exists())
            result = subprocess.run(
                [
                    str(self.repo / "scripts/new-app"),
                    "tools",
                    "example",
                    "--image",
                    f"ghcr.io/example/app:1.0.0@sha256:{digest}",
                    "--port",
                    "8789",
                    "--health-path",
                    "/ping",
                    "--internal",
                ],
                cwd=root,
                text=True,
                capture_output=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            app = category / "example/app"
            kustomization = (app / "kustomization.yaml").read_text()
            oci = (app / "ocirepository.yaml").read_text()
            helmrelease = (app / "helmrelease.yaml").read_text()
            route = (app / "httproute-envoy.yaml").read_text()
            self.assertIn("./ocirepository.yaml", kustomization)
            self.assertIn("./httproute-envoy.yaml", kustomization)
            self.assertIn("tag: 5.0.1", oci)
            self.assertIn("chartRef:", helmrelease)
            self.assertNotIn("kind: HelmRepository", helmrelease)
            self.assertIn("path: /ping", helmrelease)
            self.assertIn("tag: 1.0.0@sha256:" + digest, helmrelease)
            self.assertIn("name: envoy-internal", route)
            self.assertIn("name: example", route)
            self.assertIn("port: 8789", route)

    def test_pr_delivery_follows_superseded_head_and_uses_explicit_repo(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            temp = Path(raw)
            state = temp / "state"
            state.write_text("0")
            log = temp / "gh.log"
            gh = self.executable(
                temp / "gh",
                """#!/usr/bin/env python3
import os, sys
from pathlib import Path
state = Path(os.environ['FAKE_GH_STATE'])
log = Path(os.environ['FAKE_GH_LOG'])
args = sys.argv[1:]
with log.open('a') as stream:
    stream.write(' '.join(args) + '\\n')
if args[:2] == ['repo', 'view']:
    print('shamubernetes/home-k8s')
    raise SystemExit(0)
if args[:2] == ['pr', 'view']:
    if 'state,headRefOid' in args:
        print('MERGED ' + 'b' * 40)
        raise SystemExit(0)
    index = int(state.read_text())
    values = ['a' * 40, 'b' * 40, 'b' * 40, 'b' * 40]
    print(values[min(index, len(values) - 1)])
    state.write_text(str(index + 1))
    raise SystemExit(0)
if args[:2] == ['pr', 'checks']:
    raise SystemExit(1 if int(state.read_text()) == 1 else 0)
if args[:2] == ['pr', 'merge']:
    raise SystemExit(0)
raise SystemExit(2)
""",
            )
            result = subprocess.run(
                [str(self.repo / "scripts/pr-deliver"), "123", "--merge", "--interval", "1"],
                cwd=self.repo,
                env={
                    **os.environ,
                    "GH_BIN": str(gh),
                    "FAKE_GH_STATE": str(state),
                    "FAKE_GH_LOG": str(log),
                    "TMPDIR": str(temp),
                },
                text=True,
                capture_output=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("discarding stale check result", result.stderr)
            lines = log.read_text().splitlines()
            checks = [line for line in lines if line.startswith("pr checks")]
            self.assertEqual(len(checks), 2)
            merge = next(line for line in lines if line.startswith("pr merge"))
            self.assertIn("--repo shamubernetes/home-k8s", merge)
            self.assertIn("--match-head-commit " + "b" * 40, merge)
            self.assertIn("--auto", merge)
            self.assertIn("--delete-branch", merge)
            self.assertIn("merged shamubernetes/home-k8s#123", result.stdout)


if __name__ == "__main__":
    unittest.main()
