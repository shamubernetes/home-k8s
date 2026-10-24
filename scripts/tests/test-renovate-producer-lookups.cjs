#!/usr/bin/env node
// Exercise real Renovate extraction and protect only the accepted original pins.
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
  const { extractPackageFile: fluxExtract } = await load("modules/manager/flux/extract.js");
  const zooFile = "kubernetes/apps/services/zoo-operator/app/ocirepository.yaml";
  const zoo = fluxExtract(fs.readFileSync(path.join(repo, zooFile), "utf8"), zooFile, {}).deps;
  assert.equal(zoo.length, 1);
  assert.equal(zoo[0].currentValue, "0.6.6");
  assert.equal(zoo[0].currentDigest, "sha256:7a082428fb208217616fc677dcc0c93bf75575294ed385546aea039170e70f21");
  assert.equal(zoo[0].skipReason, undefined);
  assert.ok(zoo[0].autoReplaceStringTemplate.includes("{{newValue}}"));
  assert.ok(zoo[0].autoReplaceStringTemplate.includes("{{newDigest}}"));

  const { extractPackageFile } = await load("modules/manager/kubernetes/extract.js");
  const { applyPackageRules } = await load("util/package-rules/index.js");
  const name = "ghcr.io/volcengine/openviking";
  const digest = "sha256:23bfcc83f941e92d5bbf71dd5626d4a46e2173043c5ae68317778da47d42fb6d";
  const rules = config.packageRules.filter((r) => r.description === "Preserve the accepted OpenViking original-proof image");
  assert.equal(rules.length, 1);
  const extract = (file) => extractPackageFile(fs.readFileSync(path.join(repo, file), "utf8"), file, {}).deps.find((d) => d.depName === name);
  const apply = (dep, file) => applyPackageRules({ ...dep, manager: "kubernetes", packageFile: file, packageRules: rules });
  const policy = "kubernetes/apps/tools/openviking/backup-native/policy-data.yaml";
  const active = extract(policy);
  assert.equal(active.currentValue, "v0.4.23.1");
  assert.equal(active.currentDigest, digest);
  assert.notEqual((await apply(active, policy)).enabled, false);
  let cases = 2;
  for (const file of rules[0].matchFileNames) {
    const dep = extract(file);
    assert.equal(dep.currentValue, "0.2.12");
    assert.equal(dep.currentDigest, digest);
    assert.equal((await apply(dep, file)).enabled, false);
    assert.notEqual((await apply({ ...dep, currentDigest: "sha256:" + "a".repeat(64) }, file)).enabled, false);
    assert.notEqual((await apply({ ...dep, currentValue: "v0.4.23.1" }, file)).enabled, false);
    assert.notEqual((await apply(dep, policy)).enabled, false);
    assert.notEqual((await apply({ ...dep, depName: "example/other", packageName: "example/other" }, file)).enabled, false);
    cases += 5;
  }
  console.log(`PASS ${cases} real extractor/matcher cases: valid Zoo tag+digest, active capture enabled, only exact original proofs frozen`);
}
main().catch((error) => { console.error(error); process.exitCode = 1; });
