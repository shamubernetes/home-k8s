// Capture the native PDS store only while its existing bounded supervisor holds the writer.
'use strict';
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const {execFileSync} = require('node:child_process');
const root = process.env.PDS_DATA_DIRECTORY || '/pds';
const bundle = path.join(root, '.kopiur-pds');
const supervisor = process.env.PDS_CAPTURE_SUPERVISOR || '/kopiur/recurring.sh';
function control(...args) {
  execFileSync('sh', [supervisor, ...args], {stdio: 'ignore', timeout: 125000});
}
function regular(p, directory = false) {
  const s = fs.lstatSync(p);
  if (s.isSymbolicLink() || (directory ? !s.isDirectory() : !s.isFile())) throw Error('unsupported capture object');
  return s;
}
function exists(p) {
  try { fs.lstatSync(p); return true; } catch (e) {
    if (e.code === 'ENOENT') return false;
    throw e;
  }
}
function inventory() {
  const device = regular(root, true).dev;
  const rows = [];
  function walk(p) {
    const s = fs.lstatSync(p, {bigint: true});
    if ((!s.isFile() && !s.isDirectory()) || Number(s.dev) !== device || (s.isFile() && s.nlink !== 1n)) {
      throw Error('store contains a link, special object or submount');
    }
    rows.push([path.relative(root, p), String(s.mode), String(s.uid), String(s.gid),
      String(s.ino), String(s.size), String(s.mtimeNs), String(s.ctimeNs)]);
    if (s.isDirectory()) for (const name of fs.readdirSync(p).sort()) {
      const child = path.join(p, name);
      if (child !== bundle) walk(child);
    }
  }
  // The root's timestamps change when the helper first creates its own directory.
  walk(root);
  return JSON.stringify(rows);
}
function digest(p) { return crypto.createHash('sha256').update(fs.readFileSync(p)).digest('hex'); }
function durable(p, bytes) {
  fs.writeFileSync(p, bytes, {mode: 0o600, flag: 'wx'});
  const fd = fs.openSync(p, 'r');
  try { fs.fsyncSync(fd); } finally { fs.closeSync(fd); }
}
function syncDirectory(p) {
  const fd = fs.openSync(p, 'r');
  try { fs.fsyncSync(fd); } finally { fs.closeSync(fd); }
}
function capture() {
  fs.mkdirSync(bundle, {mode: 0o700, recursive: true});
  regular(bundle, true);
  fs.chmodSync(bundle, 0o700);
  const complete = path.join(bundle, 'COMPLETE');
  for (const name of ['COMPLETE', 'COMPLETE.tmp']) {
    const p = path.join(bundle, name);
    if (exists(p)) regular(p);
    fs.rmSync(p, {force: true});
  }
  // Old success is invalid before any directory preflight can fail.
  for (const name of ['pending', 'previous', 'current']) {
    const p = path.join(bundle, name);
    if (exists(p)) regular(p, true);
  }
  fs.rmSync(path.join(bundle, 'pending'), {recursive: true, force: true});
  fs.rmSync(path.join(bundle, 'previous'), {recursive: true, force: true});
  const pending = path.join(bundle, 'pending');
  fs.mkdirSync(pending, {mode: 0o700});
  const token = crypto.randomBytes(16).toString('hex');
  try {
    control('acquire', token, '1800', '1320');
    const before = inventory();
    execFileSync('tar', ['-c', '--numeric-owner', '--exclude=./.kopiur-pds', '-f',
      path.join(pending, 'application-state.tar'), '-C', root, '.'], {stdio: 'ignore', timeout: 600000});
    control('check', '120');
    if (inventory() !== before) throw Error('native store changed while held');
    const runtime = Object.fromEntries(Object.entries(process.env).filter(([key]) =>
      key.startsWith('PDS_') || ['TZ', 'LOG_ENABLED', 'LOG_LEVEL'].includes(key)));
    const escrowKeys = [
      'PDS_JWT_SECRET',
      'PDS_ADMIN_PASSWORD',
      'PDS_PLC_ROTATION_KEY_K256_PRIVATE_KEY_HEX',
    ];
    for (const key of escrowKeys) {
      if (!runtime[key]) throw Error('original runtime escrow input missing');
    }
    durable(path.join(pending, 'runtime.json'), JSON.stringify(runtime));
    durable(path.join(pending, 'metadata.json'), JSON.stringify({format: 1, contract: 'stopped-pds-whole-store',
      inventorySHA256: crypto.createHash('sha256').update(before).digest('hex'), completedAt: new Date().toISOString()}));
    const archive = path.join(pending, 'application-state.tar');
    const fd = fs.openSync(archive, 'r');
    try { fs.fsyncSync(fd); } finally { fs.closeSync(fd); }
    const files = ['application-state.tar', 'runtime.json', 'metadata.json'];
    durable(path.join(pending, 'SHA256SUMS'), files.map(name => digest(path.join(pending, name)) + '  ' + name).join('\n') + '\n');
    control('check', '30');
    syncDirectory(pending);
    const current = path.join(bundle, 'current');
    if (fs.existsSync(current)) fs.renameSync(current, path.join(bundle, 'previous'));
    fs.renameSync(pending, current);
    syncDirectory(bundle);
    durable(path.join(bundle, 'COMPLETE.tmp'), 'complete\n');
    fs.renameSync(path.join(bundle, 'COMPLETE.tmp'), complete);
    syncDirectory(bundle);
  } catch (error) {
    fs.rmSync(complete, {force: true});
    throw error;
  } finally {
    try { control('release', token); } catch (error) {
      fs.rmSync(complete, {force: true});
      throw error;
    } finally {
      fs.rmSync(pending, {recursive: true, force: true});
    }
  }
  console.log(JSON.stringify({contract: 'stopped-pds-whole-store', complete: true}));
}
async function main() {
  if (process.argv[2] === 'liveness') {
    try { control('check', '1'); return; } catch (_) { /* Require the real native health path. */ }
    const response = await fetch('http://127.0.0.1:3000/xrpc/_health', {signal: AbortSignal.timeout(2000)});
    if (!response.ok) throw Error('native health failed');
  } else if (process.argv[2] === 'capture') {
    capture();
  } else throw Error('unknown capture command');
}
main().catch(() => { console.error('PDS capture or native health failed; private output withheld'); process.exitCode = 1; });
