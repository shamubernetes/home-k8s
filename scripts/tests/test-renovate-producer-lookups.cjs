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

  const extractors = {
    kubernetes: (await load("modules/manager/kubernetes/extract.js")).extractPackageFile,
    "helm-values": (await load("modules/manager/helm-values/extract.js")).extractPackageFile,
  };
  const { applyPackageRules } = await load("util/package-rules/index.js");
  const name = "ghcr.io/volcengine/openviking";
  const digest = "sha256:23bfcc83f941e92d5bbf71dd5626d4a46e2173043c5ae68317778da47d42fb6d";
  const rules = config.packageRules.filter((r) => r.description === "Preserve the accepted OpenViking original-proof image");
  assert.equal(rules.length, 1);
  assert.deepEqual(rules[0].matchManagers, Object.keys(extractors));
  const extract = async (file, manager) => (await extractors[manager](fs.readFileSync(path.join(repo, file), "utf8"), file, {})).deps.find((d) => d.depName === name);
  const apply = (dep, file, manager) => applyPackageRules({ ...dep, manager, packageFile: file, packageRules: rules });
  const policy = "kubernetes/apps/tools/openviking/backup-native/policy-data.yaml";
  let cases = 1;
  for (const manager of Object.keys(extractors)) {
    const active = await extract(policy, manager);
    assert.equal(active.currentValue, "v0.4.23.1");
    assert.equal(active.currentDigest, digest);
    assert.notEqual((await apply(active, policy, manager)).enabled, false);
    cases++;
    for (const file of rules[0].matchFileNames) {
      const dep = await extract(file, manager);
      assert.equal(dep.currentValue, "0.2.12");
      assert.equal(dep.currentDigest, digest);
      assert.equal((await apply(dep, file, manager)).enabled, false);
      assert.notEqual((await apply({ ...dep, currentDigest: "sha256:" + "a".repeat(64) }, file, manager)).enabled, false);
      assert.notEqual((await apply({ ...dep, currentValue: "v0.4.23.1" }, file, manager)).enabled, false);
      assert.notEqual((await apply(dep, policy, manager)).enabled, false);
      assert.notEqual((await apply({ ...dep, depName: "example/other", packageName: "example/other" }, file, manager)).enabled, false);
      cases += 5;
    }
  }
  const convexFile = "kubernetes/apps/services/convex/app/helmrelease.yaml";
  const convex = await extractors["helm-values"](fs.readFileSync(path.join(repo, convexFile), "utf8"), convexFile, {});
  const convexRules = json5.parse(fs.readFileSync(path.join(repo, ".github/renovate/packageRules.json5"), "utf8")).packageRules.filter((r) => r.description === "Convex runtime image digests");
  assert.equal(convexRules.length, 1);
  for (const [image, pin] of [
    ["backend", "sha256:1f2044e3eac463ac78973b136c0baf72d4ada602611d853d6f99f280e29e0a98"],
    ["dashboard", "sha256:284a2638e0c1a4ec0c2327d8219776f3a426ca5824b81686ae4d9454dc0ce8ed"],
  ]) {
    const dep = convex.deps.find((d) => d.depName === `ghcr.io/convex/convex-${image}`);
    assert.ok(dep);
    assert.equal(dep.currentValue, "latest");
    assert.equal(dep.currentDigest, pin);
    const effective = await applyPackageRules({ ...dep, datasource: "docker", updateType: "digest", packageRules: convexRules });
    assert.equal(effective.groupName, "convex-runtime");
    assert.equal(effective.minimumGroupSize, 2);
    assert.equal(effective.minimumReleaseAge, "7 days");
    cases++;
  }
  console.log(`PASS ${cases} real extractor/matcher cases: valid pins, exact original-proof guards, canonical Convex namespace and unchanged digest-group policy`);
}
main().catch((error) => { console.error(error); process.exitCode = 1; });
