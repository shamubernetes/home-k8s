# bazarr recovery

## Accepted replacement

Trusted-main independent original recovery [run 39472233737](https://github.com/shamubernetes/home-k8s/actions/runs/39472233737) completed on October 20. It restored retained NAS `9a623d4c77684a42b970984ce9df7076` and R2 `c889e51bdf7ce94ee2f6b555e546842d`, validated native database fingerprints and paired configuration/file bytes, removed networked readers before boot, and exercised the isolated native API. 17 tables, 410279 rows, 5324 paired files; native API 665 series and 3783 movies. These are actual restored original points, not synthetic fixtures or a claim that the older point contains later writes.

The enabled supported daily capture completed again on October 20 and the enabled offsite replication copied NAS `a11564601af013cf22a91ccb2025b73e` to R2 `03fbeba4158c2428203de980e3ee24f2` with exact copiedFrom lineage. The policy verified all selected files without errors. The production effective-key authenticated API returned HTTP 200 immediately before retirement preparation. Runtime credentials retain their existing GitOps and 1Password sources. No new escrow is required or introduced. The existing no-stop single-database stable-filetree capture remains unchanged. Shared NAS media trees are outside the replaced config-PVC scope.

## Data and history preservation

The production PVC UID `b23179bc-9772-4230-ba21-ac295cff9c83` must have live `kustomize.toolkit.fluxcd.io/prune: disabled` from the protection change before this retirement merges. Retirement intentionally removes its bootstrap declaration from Flux inventory while retaining the bound PVC, PV and the unchanged application's existingClaim. Its immutable historical dataSourceRef is provenance, not a live restore dependency. Never recreate, edit its immutable spec, or delete that bound claim to clear the reference.

Both legacy ExternalSecrets are retained locally with unchanged names, targets and 1Password references, keeping NAS/R2 Restic history credentials available. No repository or historical point is deleted, no remote pruning is introduced, and the accepted originals and current copied R2 point remain retained. The VolSync ReplicationSources and restore destination can now be retired without removing their production PVC or history access.

## Independent recovery

Use the supported trusted-main recovery workflow and the existing dedicated per-app NAS/R2 identity to restore into disposable storage, never over production. The accepted workflow run records the exact original-point/native database and API checks. For disaster recovery provision a new empty claim separately and recover the chosen Kopiur point before deliberately changing existingClaim through GitOps. Do not restore the retired automatic VolSync bootstrap.
