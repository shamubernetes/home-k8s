alias hra := helm-repo-add

helm-repo-add repo_name repo_url:
  #!/usr/bin/env bash
  echo "Adding {{repo_name}}: {{repo_url}}"
  just template_helm_repo {{repo_name}} {{repo_url}}
  just update_kustomization_helm {{repo_name}}

[private]
template_helm_repo repo_name repo_url:
  makejinja -D name={{repo_name}} -D url={{repo_url}} -i {{justfile_directory()}}/templates/helmrepo.yaml.j2 -o {{justfile_directory()}}/kubernetes/flux/repositories/helm/{{repo_name}}.yaml

[private]
update_kustomization_helm repo_name:
  yq '.resources += "./{{repo_name}}.yaml"' -i {{justfile_directory()}}/kubernetes/flux/repositories/helm/kustomization.yaml
