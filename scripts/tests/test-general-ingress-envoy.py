#!/usr/bin/env python3
"""Transition contract for the general NGINX-to-Envoy route estate."""
from __future__ import annotations

import json
from pathlib import Path
import re
import subprocess
import sys

REPO_ROOT = Path(__file__).resolve().parents[2]
EXPECTED_ROUTE_NAMES = set(['adguard-home-origin-envoy',
 'adguard-home-secondary-envoy',
 'adguard-home-tertiary-envoy',
 'alertmanager-envoy',
 'atuin-envoy',
 'audiobookshelf-envoy',
 'calibre-web-automated-envoy',
 'ceph-objectstore-envoy',
 'changedetection-envoy',
 'convex-actions-envoy',
 'convex-api-envoy',
 'convex-dashboard-envoy',
 'cowbell-cowbell-envoy',
 'cowbell-envoy',
 'cowbell-issues-envoy',
 'cowtail-envoy',
 'cwa-bdl-envoy',
 'documenso-envoy',
 'firecrawl-envoy',
 'flux-webhook-envoy',
 'gatus-envoy',
 'gitmirror-envoy',
 'grafana-envoy',
 'grimmory-envoy',
 'hermes-webhooks-envoy',
 'home-assistant-code-server-envoy',
 'home-assistant-envoy',
 'hubble-ui-envoy',
 'konflate-envoy',
 'konflate-webhook-envoy',
 'n8n-envoy',
 'onepassword-connect-envoy',
 'pasta-envoy',
 'plane-envoy',
 'plane-github-bridge-envoy',
 'plane-mcp-envoy',
 'plane-uploads-envoy',
 'plex-envoy',
 'pocket-id-envoy',
 'prometheus-envoy',
 'renovate-envoy',
 'romm-envoy',
 'rook-ceph-dashboard-envoy',
 'searxng-envoy',
 'seerr-envoy',
 'siren-envoy',
 'stash-envoy',
 'tautulli-envoy',
 'tracearr-envoy',
 'tubearchivist-envoy',
 'web-static-envoy',
 'wizarr-envoy',
 'zoo-fleet-envoy'])
EXPECTED = [('adguard-home-origin-envoy', 'envoy-internal', 'adguard.${HOME_DOMAIN}', 'PathPrefix', '/', 'adguard-home-origin', 3000, ''),
 ('adguard-home-secondary-envoy', 'envoy-internal', 'adguard-secondary.${HOME_DOMAIN}', 'PathPrefix', '/', 'adguard-home-secondary', 3000, ''),
 ('adguard-home-tertiary-envoy', 'envoy-internal', 'adguard-tertiary.${HOME_DOMAIN}', 'PathPrefix', '/', 'adguard-home-tertiary', 3000, ''),
 ('alertmanager-envoy', 'envoy-internal', 'alertmanager.${HOME_DOMAIN}', 'PathPrefix', '/', 'kube-prometheus-stack-alertmanager', 9093, ''),
 ('atuin-envoy', 'envoy-external', 'atuin.${HOME_DOMAIN}', 'PathPrefix', '/', 'atuin', 80, ''),
 ('audiobookshelf-envoy', 'envoy-internal', 'audiobookshelf.${HOME_DOMAIN}', 'PathPrefix', '/', 'audiobookshelf', 80, ''),
 ('calibre-web-automated-envoy', 'envoy-internal', 'calibre.${HOME_DOMAIN}', 'PathPrefix', '/', 'calibre-web-automated', 8083, ''),
 ('ceph-objectstore-envoy', 'envoy-internal', 'rgw.${HOME_DOMAIN}', 'PathPrefix', '/', 'rook-ceph-rgw-ceph-objectstore', 80, ''),
 ('changedetection-envoy', 'envoy-internal', 'changedetection.${HOME_DOMAIN}', 'PathPrefix', '/', 'changedetection-app', 5000, ''),
 ('convex-actions-envoy', 'envoy-internal', 'convex-actions.${HOME_DOMAIN}', 'PathPrefix', '/', 'convex-backend', 3211, ''),
 ('convex-api-envoy', 'envoy-internal', 'convex-api.${HOME_DOMAIN}', 'PathPrefix', '/', 'convex-backend', 3210, ''),
 ('convex-dashboard-envoy', 'envoy-internal', 'convex.${HOME_DOMAIN}', 'PathPrefix', '/', 'convex-dashboard', 6791, ''),
 ('cowbell-cowbell-envoy', 'envoy-external', 'cowbell.${HOME_DOMAIN}', 'PathPrefix', '/', 'cowbell-cowbell', 80, ''),
 ('cowbell-envoy', 'envoy-external', 'issue.${HOME_DOMAIN}', 'PathPrefix', '/', 'cowbell-cowbell', 80, ''),
 ('cowbell-issues-envoy', 'envoy-external', 'issues.${HOME_DOMAIN}', 'PathPrefix', '/', 'cowbell-cowbell', 80, ''),
 ('cowtail-envoy', 'envoy-external', 'cowtail.${HOME_DOMAIN}', 'PathPrefix', '/', 'cowtail', 80, '86400s'),
 ('cwa-bdl-envoy', 'envoy-internal', 'books.${HOME_DOMAIN}', 'PathPrefix', '/', 'cwa-bdl', 8084, ''),
 ('documenso-envoy', 'envoy-internal', 'documenso.${HOME_DOMAIN}', 'PathPrefix', '/', 'documenso', 3000, ''),
 ('firecrawl-envoy', 'envoy-internal', 'firecrawl.${HOME_DOMAIN}', 'PathPrefix', '/', 'firecrawl-api', 3002, ''),
 ('flux-webhook-envoy', 'envoy-external', 'flux-webhook.${HOME_DOMAIN}', 'PathPrefix', '/hook/', 'webhook-receiver', 80, ''),
 ('gatus-envoy', 'envoy-internal', 'gatus.${HOME_DOMAIN}', 'PathPrefix', '/', 'gatus', 80, ''),
 ('gitmirror-envoy', 'envoy-external', 'gitmirror.${HOME_DOMAIN}', 'PathPrefix', '/webhooks/github', 'git-mirror-operator-github-webhook-service', 8082, ''),
 ('grafana-envoy', 'envoy-internal', 'grafana.${HOME_DOMAIN}', 'PathPrefix', '/', 'grafana-v5-service', 3000, ''),
 ('grimmory-envoy', 'envoy-internal', 'grimmory.${HOME_DOMAIN}', 'PathPrefix', '/', 'grimmory', 6060, ''),
 ('hermes-webhooks-envoy', 'envoy-external', 'hermes-webhooks.${HOME_DOMAIN}', 'Exact', '/webhooks/firecrawl-automation', 'hermes-webhooks', 80, ''),
 ('home-assistant-code-server-envoy', 'envoy-internal', 'code.ha.${HOME_DOMAIN}', 'PathPrefix', '/', 'home-assistant', 12321, ''),
 ('home-assistant-envoy', 'envoy-internal', 'ha.${HOME_DOMAIN}', 'PathPrefix', '/', 'home-assistant', 8123, ''),
 ('hubble-ui-envoy', 'envoy-internal', 'hubble.${HOME_DOMAIN}', 'PathPrefix', '/', 'hubble-ui', 80, ''),
 ('konflate-envoy', 'envoy-internal', 'konflate.${HOME_DOMAIN}', 'PathPrefix', '/', 'konflate', 8080, ''),
 ('konflate-webhook-envoy', 'envoy-external', 'konflate-hooks.${HOME_DOMAIN}', 'Exact', '/hooks', 'konflate', 8080, ''),
 ('n8n-envoy', 'envoy-internal', 'n8n.${HOME_DOMAIN}', 'PathPrefix', '/', 'n8n', 5678, ''),
 ('onepassword-connect-envoy', 'envoy-internal', 'op-connect.${HOME_DOMAIN}', 'PathPrefix', '/', 'onepassword-connect', 8080, ''),
 ('pasta-envoy', 'envoy-internal', 'pasta.${HOME_DOMAIN}', 'PathPrefix', '/', 'pasta', 80, ''),
 ('plane-envoy', 'envoy-internal', 'plane.${HOME_DOMAIN}', 'PathPrefix', '/', 'plane-web', 3000, ''),
 ('plane-envoy', 'envoy-internal', 'plane.${HOME_DOMAIN}', 'PathPrefix', '/api/', 'plane-api', 8000, ''),
 ('plane-envoy', 'envoy-internal', 'plane.${HOME_DOMAIN}', 'PathPrefix', '/auth/', 'plane-api', 8000, ''),
 ('plane-envoy', 'envoy-internal', 'plane.${HOME_DOMAIN}', 'PathPrefix', '/god-mode/', 'plane-admin', 3000, ''),
 ('plane-envoy', 'envoy-internal', 'plane.${HOME_DOMAIN}', 'PathPrefix', '/graphql/', 'plane-api', 8000, ''),
 ('plane-envoy', 'envoy-internal', 'plane.${HOME_DOMAIN}', 'PathPrefix', '/live/', 'plane-live', 3000, ''),
 ('plane-envoy', 'envoy-internal', 'plane.${HOME_DOMAIN}', 'PathPrefix', '/marketplace/', 'plane-api', 8000, ''),
 ('plane-envoy', 'envoy-internal', 'plane.${HOME_DOMAIN}', 'PathPrefix', '/silo/', 'plane-silo', 3000, ''),
 ('plane-envoy', 'envoy-internal', 'plane.${HOME_DOMAIN}', 'PathPrefix', '/spaces/', 'plane-space', 3000, ''),
 ('plane-github-bridge-envoy', 'envoy-external', 'plane-github.${HOME_DOMAIN}', 'Exact', '/github/webhook', 'plane-github-bridge', 80, ''),
 ('plane-github-bridge-envoy', 'envoy-external', 'plane-github.${HOME_DOMAIN}', 'Exact', '/plane/callback', 'plane-github-bridge', 80, ''),
 ('plane-github-bridge-envoy', 'envoy-external', 'plane-github.${HOME_DOMAIN}', 'Exact', '/plane/setup', 'plane-github-bridge', 80, ''),
 ('plane-github-bridge-envoy', 'envoy-external', 'plane-github.${HOME_DOMAIN}', 'Exact', '/plane/webhook', 'plane-github-bridge', 80, ''),
 ('plane-mcp-envoy', 'envoy-internal', 'plane-mcp.${HOME_DOMAIN}', 'PathPrefix', '/mcp', 'plane-mcp', 3000, ''),
 ('plane-uploads-envoy', 'envoy-internal', 'plane.${HOME_DOMAIN}', 'PathPrefix', '/uploads', 'rook-ceph-rgw-ceph-objectstore', 80, ''),
 ('plex-envoy', 'envoy-external,envoy-internal', 'plex.${HOME_DOMAIN}', 'PathPrefix', '/', 'plex', 32400, ''),
 ('pocket-id-envoy', 'envoy-internal', 'sso.${HOME_DOMAIN}', 'PathPrefix', '/', 'pocket-id', 1411, ''),
 ('prometheus-envoy', 'envoy-internal', 'prometheus.${HOME_DOMAIN}', 'PathPrefix', '/', 'kube-prometheus-stack-prometheus', 9090, ''),
 ('renovate-envoy', 'envoy-external', 'renovate.${HOME_DOMAIN}', 'PathPrefix', '/', 'renovate', 8080, ''),
 ('romm-envoy', 'envoy-internal', 'romm.${HOME_DOMAIN}', 'PathPrefix', '/', 'romm', 8080, ''),
 ('rook-ceph-dashboard-envoy', 'envoy-internal', 'rook.${HOME_DOMAIN}', 'PathPrefix', '/', 'rook-ceph-mgr-dashboard', 7000, ''),
 ('searxng-envoy', 'envoy-internal', 'searxng.${HOME_DOMAIN}', 'PathPrefix', '/', 'searxng', 8080, ''),
 ('seerr-envoy', 'envoy-external', 'requests.${HOME_DOMAIN}', 'PathPrefix', '/', 'seerr', 80, ''),
 ('siren-envoy', 'envoy-internal', 'siren.${HOME_DOMAIN}', 'PathPrefix', '/', 'siren', 3000, ''),
 ('stash-envoy', 'envoy-internal', 'stash.${HOME_DOMAIN}', 'PathPrefix', '/', 'stash', 80, ''),
 ('tautulli-envoy', 'envoy-internal', 'tautulli.${HOME_DOMAIN}', 'PathPrefix', '/', 'tautulli', 80, ''),
 ('tracearr-envoy', 'envoy-internal', 'tracearr.${HOME_DOMAIN}', 'PathPrefix', '/', 'tracearr', 3000, ''),
 ('tubearchivist-envoy', 'envoy-internal', 'tube.${HOME_DOMAIN}', 'PathPrefix', '/', 'tubearchivist', 8000, ''),
 ('web-static-envoy', 'envoy-external', 'static.${HOME_DOMAIN}', 'PathPrefix', '/', 'web-static', 80, ''),
 ('wizarr-envoy', 'envoy-external', 'join.${HOME_DOMAIN}', 'PathPrefix', '/', 'wizarr', 5690, ''),
 ('zoo-fleet-envoy', 'envoy-external', 'fleet.${HOME_DOMAIN}', 'PathPrefix', '/', 'zoo-fleet', 3000, '300s')]
GRAFANA_ROUTE_TRANSITION = {
    ('grafana-envoy', 'envoy-internal', 'grafana.${HOME_DOMAIN}', 'PathPrefix', '/', 'grafana', 80, ''),
    ('grafana-envoy', 'envoy-internal', 'grafana.${HOME_DOMAIN}', 'PathPrefix', '/', 'grafana-v5-service', 3000, ''),
}

def load_documents(path: Path) -> list[dict]:
    result = subprocess.run(
        ["yq", "ea", "-o=json", "-I=0", ".", str(path)],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return [json.loads(line) for line in result.stdout.splitlines() if line.strip()]


def route_rows(document: dict) -> list[tuple]:
    name = document["metadata"]["name"]
    parent_names = ",".join(sorted(ref["name"] for ref in document["spec"]["parentRefs"]))
    hostnames = document["spec"]["hostnames"]
    if len(hostnames) != 1:
        raise AssertionError(f"{name} must declare exactly one hostname")
    host = hostnames[0]
    rows = []
    for rule in document["spec"]["rules"]:
        matches = rule.get("matches") or [{"path": {"type": "PathPrefix", "value": "/"}}]
        timeout = str((rule.get("timeouts") or {}).get("request", ""))
        for backend in rule["backendRefs"]:
            for match in matches:
                path = match.get("path") or {"type": "PathPrefix", "value": "/"}
                rows.append((name, parent_names, host, path.get("type", "PathPrefix"), path.get("value", "/"), backend["name"], int(backend["port"]), timeout))
    return rows


def main() -> int:
    observed = []
    observed_names = set()
    for path in sorted(REPO_ROOT.glob("kubernetes/apps/**/*httproute*.yaml")):
        selected = []
        for document in load_documents(path):
            if document.get("kind") != "HTTPRoute":
                continue
            name = document["metadata"]["name"]
            if name not in EXPECTED_ROUTE_NAMES:
                continue
            if document.get("apiVersion") != "gateway.networking.k8s.io/v1":
                raise AssertionError(f"{name} must use gateway.networking.k8s.io/v1")
            selected.append(name)
            observed_names.add(name)
            observed.extend(route_rows(document))
        if selected:
            kustomization = path.parent / "kustomization.yaml"
            if not kustomization.is_file():
                raise AssertionError(f"missing kustomization next to {path.relative_to(REPO_ROOT)}")
            pattern = rf"(?m)^\s*-\s+(?:\./)?{re.escape(path.name)}$"
            if not re.search(pattern, kustomization.read_text()):
                raise AssertionError(f"{path.name} is not registered in {kustomization.relative_to(REPO_ROOT)}")

    missing = sorted(EXPECTED_ROUTE_NAMES - observed_names)
    unexpected = sorted(observed_names - EXPECTED_ROUTE_NAMES)
    if missing or unexpected:
        raise AssertionError(f"route name mismatch: missing={missing} unexpected={unexpected}")
    grafana_rows = [row for row in observed if row[0] == "grafana-envoy"]
    if len(grafana_rows) != 1 or grafana_rows[0] not in GRAFANA_ROUTE_TRANSITION:
        raise AssertionError(f"Grafana route must use one approved transition backend: {grafana_rows}")
    expected = list(EXPECTED)
    expected[expected.index(next(row for row in expected if row[0] == "grafana-envoy"))] = grafana_rows[0]
    if sorted(observed) != sorted(expected):
        expected_only = sorted(set(expected) - set(observed))
        observed_only = sorted(set(observed) - set(expected))
        raise AssertionError(f"route contract changed: expected_only={expected_only} observed_only={observed_only}")
    print(f"ok: general Envoy route contract ({len(observed_names)} routes, {len(observed)} path/backend bindings)")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (AssertionError, subprocess.CalledProcessError) as error:
        print(f"general Envoy route contract failed: {error}", file=sys.stderr)
        raise SystemExit(1)
