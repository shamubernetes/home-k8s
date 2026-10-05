#!/usr/bin/env node
// Keep Ceph CSI operator updates within the installed Rook CRD API.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { execFileSync } = require("node:child_process");
const { createRequire } = require("node:module");
const { pathToFileURL } = require("node:url");

async function main() {
  const install = execFileSync("mise", ["where", "npm:renovate"], { encoding: "utf8" }).trim();
  const root = fs.realpathSync(path.join(install, "node_modules", "renovate"));
  const load = (name) => import(pathToFileURL(path.join(root, "dist", name)).href);
  await (await load("logger/index.js")).init();
  const repo = path.resolve(__dirname, "../..");
  (await load("config/global.js")).GlobalConfig.set({ localDir: repo });
  const json5 = createRequire(path.join(root, "package.json"))("json5");
  const config = json5.parse(fs.readFileSync(path.join(repo, ".github/renovate.json5"), "utf8"));
  const { extractPackageFile } = await load("modules/manager/helm-values/index.js");
  const filename = "kubernetes/apps/rook-ceph/rook-ceph/app/helmrelease.yaml";
  const extracted = await extractPackageFile(fs.readFileSync(path.join(repo, filename), "utf8"), filename, {});
  const operator = extracted.deps.find((dep) => dep.depName === "quay.io/cephcsi/ceph-csi-operator");
  assert.ok(operator, "real Helm values extraction finds the operator image");
  assert.equal(operator.datasource, "docker");
  const rules = config.packageRules.filter((rule) => rule.matchPackageNames?.includes(operator.depName));
  assert.equal(rules.length, 1);
  const rule = rules[0];
  assert.deepEqual(rule.matchDatasources, ["docker"]);
  assert.deepEqual(rule.matchFileNames, [filename]);
  assert.deepEqual(rule.matchPackageNames, [operator.depName]);
  assert.equal(rule.allowedVersions, "<1.1.0");
  assert.equal(rule.enabled, undefined, "supported operator patches remain enabled");
  const { filterVersions } = await load("workers/repository/process/lookup/filter.js");
  const releases = ["v1.0.5", "v1.0.6", "v1.1.0", "v1.1.1", "v2.0.0"].map((version) => ({ version }));
  for (const scheme of ["docker", "semver"]) {
    const { api } = await load(`modules/versioning/${scheme}/index.js`);
    const filtered = filterVersions({ ...rule, depName: operator.depName, ignoreUnstable: false, respectLatest: false }, "v1.0.5", "v2.0.0", releases, api);
    assert.deepEqual(filtered.map((release) => release.version), ["v1.0.6"]);
  }
  console.log("PASS real Helm image extraction and Renovate filters retain supported patches and reject the unavailable bundled v1.1 CRD API");
}
main().catch((error) => { console.error(error); process.exitCode = 1; });
