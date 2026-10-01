#!/usr/bin/env node
// Keep the malformed Renovate npm artifact out of locked Mise installations.
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
  const { extractPackageFile } = await load("modules/manager/mise/index.js");
  const extracted = await extractPackageFile(fs.readFileSync(path.join(repo, ".mise.toml"), "utf8"), ".mise.toml", {});
  const renovate = extracted.deps.find((dep) => dep.packageName === "renovate");
  assert.ok(renovate, "actual Mise extraction identifies Renovate npm package");
  const rules = config.packageRules.filter((rule) => rule.matchManagers?.includes("mise") && rule.matchPackageNames?.includes(renovate.packageName));
  assert.equal(rules.length, 1, "one exact Renovate artifact guard");
  const rule = rules[0];
  assert.deepEqual(rule.matchManagers, ["mise"]);
  assert.deepEqual(rule.matchFileNames, [".mise.toml"]);
  assert.deepEqual(rule.matchPackageNames, ["renovate"]);
  assert.equal(rule.enabled, undefined, "supported Renovate upgrades stay enabled");
  const { filterVersions } = await load("workers/repository/process/lookup/filter.js");
  const { api } = await load("modules/versioning/semver/index.js");
  const releases = ["44.130.0", "44.131.0", "44.131.1", "44.131.2", "44.132.0"].map((version) => ({ version }));
  const filtered = filterVersions({ ...rule, depName: renovate.depName, ignoreUnstable: false, respectLatest: false }, "44.130.0", "44.132.0", releases, api);
  assert.deepEqual(filtered.map((release) => release.version), ["44.131.0", "44.131.2", "44.132.0"]);
  console.log("PASS actual Mise extraction and Renovate filter reject only malformed 44.131.1; supported and later releases stay eligible");
}
main().catch((error) => { console.error(error); process.exitCode = 1; });
