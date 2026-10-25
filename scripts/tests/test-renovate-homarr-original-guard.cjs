#!/usr/bin/env node
// Preserve accepted original image bytes without freezing the running app.
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
  const rules = json5.parse(fs.readFileSync(path.join(repo, ".github/renovate/packageRules.json5"), "utf8")).packageRules.filter((r) => r.description === "Preserve the qualified Homarr original-recovery image");
  assert.equal(rules.length, 1);
  const { applyPackageRules } = await load("util/package-rules/index.js");
  const file = "kubernetes/apps/services/homarr/app/kopiur-native-originals.yaml";
  const activeFile = "kubernetes/apps/services/homarr/app/helmrelease.yaml";
  let cases = 0;
  for (const manager of ["kubernetes", "helm-values"]) {
    const { extractPackageFile } = await load(`modules/manager/${manager}/extract.js`);
    const result = await extractPackageFile(fs.readFileSync(path.join(repo, file), "utf8"), file, {});
    const dep = result.deps.find((d) => d.depName === "ghcr.io/homarr-labs/homarr");
    assert.equal(dep.currentValue, "v2.4.1");
    assert.equal(dep.currentDigest, "sha256:ee67a5e856c051c4e114418a54b89a3fc3cb5507d0689e5cb89c790305c549cd");
    const apply = (d, packageFile = file) => applyPackageRules({ ...d, manager, packageFile, packageRules: rules });
    assert.equal((await apply(dep)).enabled, false);
    for (const changed of [{ ...dep, currentValue: "v2.9.0" }, { ...dep, currentDigest: "sha256:" + "a".repeat(64) }, { ...dep, depName: "example/other", packageName: "example/other" }]) assert.notEqual((await apply(changed)).enabled, false);
    assert.notEqual((await apply(dep, activeFile)).enabled, false);
    assert.notEqual((await apply({ ...dep, currentValue: "v2.10.0" }, activeFile)).enabled, false);
    cases += 7;
  }
  console.log(`ok: ${cases} real extraction and package-rule cases`);
}
main().catch((error) => { console.error(error); process.exit(1); });
