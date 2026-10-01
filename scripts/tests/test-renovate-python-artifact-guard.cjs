#!/usr/bin/env node
// Keep an unavailable standalone release out of locked Mise installations.
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
  const python = extracted.deps.find((dep) => dep.depName === "python");
  assert.equal(python.packageName, "python/cpython");
  const rules = config.packageRules.filter((rule) => rule.matchManagers?.includes("mise") && rule.matchPackageNames?.includes(python.packageName));
  assert.equal(rules.length, 1, "one exact Python availability guard");
  const rule = rules[0];
  assert.deepEqual(rule.matchManagers, ["mise"]);
  assert.deepEqual(rule.matchFileNames, [".mise.toml"]);
  assert.deepEqual(rule.matchPackageNames, ["python/cpython"]);
  assert.equal(rule.enabled, undefined, "supported Python upgrades stay enabled");
  const { filterVersions } = await load("workers/repository/process/lookup/filter.js");
  const releases = ["3.14.6", "3.14.7", "3.14.8", "3.14.9", "3.15.0"].map((version) => ({ version }));
  for (const scheme of ["semver", "pep440"]) {
    const { api } = await load(`modules/versioning/${scheme}/index.js`);
    const filtered = filterVersions({ ...rule, depName: "python", ignoreUnstable: false, respectLatest: false }, "3.14.6", "3.15.0", releases, api);
    assert.deepEqual(filtered.map((release) => release.version), ["3.14.7", "3.14.9", "3.15.0"]);
  }
  console.log("PASS exact Python extraction and real Renovate filters reject only unavailable 3.14.8; later releases remain eligible");
}
main().catch((error) => { console.error(error); process.exitCode = 1; });
