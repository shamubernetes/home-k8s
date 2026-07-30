#!/usr/bin/env bash
set -euo pipefail

repo_root=$(git rev-parse --show-toplevel)
cd "$repo_root"

protected_helmreleases=(
  kubernetes/apps/arrs/sonarr/app/helmrelease.yaml
  kubernetes/apps/arrs/radarr/app/helmrelease.yaml
  kubernetes/apps/arrs/radarr-3d/app/helmrelease.yaml
  kubernetes/apps/arrs/prowlarr/app/helmrelease.yaml
  kubernetes/apps/arrs/sabnzbd/app/helmrelease.yaml
  kubernetes/apps/arrs/bazarr/app/helmrelease.yaml
  kubernetes/apps/arrs/whisparr/app/helmrelease.yaml
  kubernetes/apps/arrs/listenarr/app/helmrelease.yaml
  kubernetes/apps/arrs/profilarr/app/helmrelease.yaml
  kubernetes/apps/services/homarr/app/helmrelease.yaml
)

oauth2_dir=kubernetes/apps/arrs/oauth2-proxy
arrs_kustomization=kubernetes/apps/arrs/kustomization.yaml
rollback_count=0

for manifest in "${protected_helmreleases[@]}"; do
  if yq -e '.spec.values.ingress != null' "$manifest" >/dev/null 2>&1; then
    ((rollback_count += 1))
    yq -e '
      .spec.values.ingress.app.annotations."external-dns.alpha.kubernetes.io/controller" == "ignore" and
      .spec.values.ingress.app.annotations."nginx.ingress.kubernetes.io/auth-url" == "http://oauth2-proxy.arrs.svc.cluster.local:4180/oauth2/auth"
    ' "$manifest" >/dev/null
  fi
done

case $rollback_count in
  10)
    [[ -d $oauth2_dir ]]
    grep -Fqx -- '- ./oauth2-proxy/ks.yaml' "$arrs_kustomization"
    state=rollback-retained
    ;;
  0)
    [[ ! -e $oauth2_dir ]]
    if grep -Fqx -- '- ./oauth2-proxy/ks.yaml' "$arrs_kustomization"; then
      printf 'oauth2-proxy remains listed after all protected Ingresses were retired\n' >&2
      exit 1
    fi
    if git grep -n -E 'nginx\.ingress\.kubernetes\.io/auth-(url|signin|response-headers)|oauth2-proxy\.arrs\.svc\.cluster\.local' -- "${protected_helmreleases[@]}" 2>/dev/null; then
      printf 'legacy protected-auth references remain after retirement\n' >&2
      exit 1
    fi
    state=oauth2-proxy-retired
    ;;
  *)
    printf 'partial protected-auth retirement is not allowed (%d/10 rollback Ingresses remain)\n' "$rollback_count" >&2
    exit 1
    ;;
esac

printf 'ok: protected auth retirement contract (%s)\n' "$state"
