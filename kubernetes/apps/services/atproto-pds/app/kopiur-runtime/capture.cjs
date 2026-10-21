// Runtime escrow for an ONLINE CSI whole-volume snapshot, not a SQLite file copy.
'use strict';
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const root = path.resolve(process.env.PDS_DATA_DIRECTORY || '/pds');
const bundle = path.join(root, '.kopiur-pds');
const contract = 'online-csi-crash-consistent-pds-volume';
const hash = bytes => crypto.createHash('sha256').update(bytes).digest('hex');
function exists(p) {
  try { fs.lstatSync(p); return true; } catch (e) {
    if (e.code === 'ENOENT') return false;
    throw e;
  }
}
function regular(p, directory = false, owned = false) {
  const s = fs.lstatSync(p);
  if (s.isSymbolicLink() || (directory ? !s.isDirectory() : !s.isFile()) ||
      (!directory && s.nlink !== 1) ||
      (owned && (s.uid !== process.getuid() || s.dev !== fs.lstatSync(root).dev))) {
    throw Error('unsafe filesystem object');
  }
  return s;
}
function syncDirectory(p) {
  const fd = fs.openSync(p, 'r');
  try { fs.fsyncSync(fd); } finally { fs.closeSync(fd); }
}
function durable(p, bytes) {
  const fd = fs.openSync(p, fs.constants.O_WRONLY | fs.constants.O_CREAT |
    fs.constants.O_EXCL | fs.constants.O_NOFOLLOW, 0o600);
  try { fs.writeFileSync(fd, bytes); fs.fsyncSync(fd); } finally { fs.closeSync(fd); }
}
function privateTree(p, enforcePrivate = true) {
  const s = fs.lstatSync(p);
  regular(p, s.isDirectory(), true);
  // Legacy tar output can be 0644 inside its private 0700 generation directory.
  // Permit retaining that generation, but require new escrow files to be 0600.
  const forbiddenBits = s.isDirectory() ? 0o5000 : 0o7000;
  if ((s.mode & forbiddenBits) || ((enforcePrivate || s.isDirectory()) && (s.mode & 0o077))) {
    throw Error('escrow permissions are not private');
  }
  if (s.isDirectory()) for (const name of fs.readdirSync(p)) privateTree(path.join(p, name), enforcePrivate);
}
function runtimeAndSource() {
  const runtime = Object.fromEntries(Object.entries(process.env).filter(([key]) =>
    key.startsWith('PDS_') || ['TZ', 'LOG_ENABLED', 'LOG_LEVEL'].includes(key)));
  for (const key of [
    'PDS_JWT_SECRET',
    'PDS_ADMIN_PASSWORD',
    'PDS_PLC_ROTATION_KEY_K256_PRIVATE_KEY_HEX',
  ]) if (!runtime[key]) throw Error('runtime escrow input missing');
  if (runtime.PDS_BLOBSTORE_S3_BUCKET) throw Error('external blobs are not on this volume');
  const locations = [
    [runtime.PDS_ACCOUNT_DB_LOCATION || path.join(root, 'account.sqlite'), false],
    [runtime.PDS_SEQUENCER_DB_LOCATION || path.join(root, 'sequencer.sqlite'), false],
    [runtime.PDS_DID_CACHE_DB_LOCATION || path.join(root, 'did_cache.sqlite'), false],
    [runtime.PDS_ACTOR_STORE_DIRECTORY || path.join(root, 'actors'), true],
    [runtime.PDS_BLOBSTORE_DISK_LOCATION, true],
  ];
  if (runtime.PDS_BLOBSTORE_DISK_TMP_LOCATION) locations.push([runtime.PDS_BLOBSTORE_DISK_TMP_LOCATION, true]);
  for (const [location, directory] of locations) {
    if (!location || !path.isAbsolute(location)) throw Error('invalid native location');
    const p = path.resolve(location);
    if (!p.startsWith(root + path.sep) || p === bundle || p.startsWith(bundle + path.sep) ||
        fs.realpathSync(p) !== p || regular(p, directory).dev !== regular(root, true).dev) {
      throw Error('native state is outside the captured volume');
    }
    fs.accessSync(p, fs.constants.R_OK | (directory ? fs.constants.X_OK : 0));
  }
  // Fail closed on an incumbent hold; never release or extend another owner.
  const coordination = path.join(bundle, 'coordination');
  if (exists(coordination)) regular(coordination, true, true);
  for (const name of ['request', 'active']) {
    if (exists(path.join(bundle, 'coordination', name))) throw Error('incumbent hold exists');
  }
  // Audit with the producer UID, never root or best-effort traversal. Native
  // changes during this audit may fail it; the CSI snapshot, not this walk,
  // supplies the point-in-time bytes. lost+found is the policy's exact exclusion.
  const device = regular(root, true).dev;
  function walk(p) {
    const s = fs.lstatSync(p);
    regular(p, s.isDirectory());
    if (s.dev !== device) throw Error('native submount is outside the snapshot');
    fs.accessSync(p, fs.constants.R_OK | (s.isDirectory() ? fs.constants.X_OK : 0));
    if (s.isDirectory()) for (const name of fs.readdirSync(p)) {
      const child = path.join(p, name);
      if (child !== bundle && child !== path.join(root, 'lost+found')) walk(child);
    }
  }
  walk(root);
  return runtime;
}
function prepare() {
  if (fs.realpathSync(root) !== root) throw Error('unsafe source root');
  regular(root, true);
  if (!exists(bundle)) fs.mkdirSync(bundle, {mode: 0o700});
  regular(bundle, true, true);
  if (fs.statSync(bundle).mode & 0o077) throw Error('unsafe escrow directory permissions');
  const lock = path.join(bundle, 'capture.lock');
  if (!exists(lock)) durable(lock, '');
  regular(lock, false, true);
  fs.chmodSync(lock, 0o600);
}
function capture() {
  prepare();
  const complete = path.join(bundle, 'COMPLETE');
  // Remove stale success durably before every fallible capture preflight.
  for (const name of ['COMPLETE', 'COMPLETE.tmp']) {
    const p = path.join(bundle, name);
    if (exists(p)) { regular(p, false, true); fs.unlinkSync(p); }
  }
  syncDirectory(bundle);
  const pending = path.join(bundle, 'pending');
  try {
    for (const name of ['pending', 'previous', 'current']) {
      const p = path.join(bundle, name);
      if (exists(p)) { regular(p, true, true); privateTree(p, false); }
    }
    for (const name of ['pending', 'previous']) fs.rmSync(path.join(bundle, name), {recursive: true, force: true});
    const runtime = runtimeAndSource();
    fs.mkdirSync(pending, {mode: 0o700});
    const generation = crypto.randomBytes(16).toString('hex');
    durable(path.join(pending, 'runtime.json'), JSON.stringify(runtime));
    durable(path.join(pending, 'metadata.json'), JSON.stringify({format: 2, contract, generation,
      completedAt: new Date().toISOString(), consistency: 'crash-consistent',
      dataPoint: 'same-volume-csi-snapshot', applicationConsistent: false}));
    const files = ['runtime.json', 'metadata.json'];
    const sums = files.map(name => hash(fs.readFileSync(path.join(pending, name))) + '  ' + name).join('\n') + '\n';
    durable(path.join(pending, 'SHA256SUMS'), sums);
    syncDirectory(pending);
    const current = path.join(bundle, 'current');
    if (exists(current)) fs.renameSync(current, path.join(bundle, 'previous'));
    fs.renameSync(pending, current);
    syncDirectory(bundle);
    durable(path.join(bundle, 'COMPLETE.tmp'), JSON.stringify({format: 2, contract, generation,
      manifestSHA256: hash(sums)}) + '\n');
    fs.renameSync(path.join(bundle, 'COMPLETE.tmp'), complete);
    syncDirectory(bundle);
    verify();
  } catch (error) {
    if (exists(complete)) fs.unlinkSync(complete);
    syncDirectory(bundle);
    throw error;
  }
  console.log(JSON.stringify({contract, escrowPrepared: true, snapshotCompleted: false}));
}
function verify() {
  // On a CSI clone/restored native tree this pairs escrow, not database integrity.
  if (fs.realpathSync(bundle) !== bundle) throw Error('unsafe escrow root');
  regular(bundle, true, true);
  const current = path.join(bundle, 'current');
  regular(current, true, true);
  privateTree(current);
  const markerPath = path.join(bundle, 'COMPLETE');
  regular(markerPath, false, true);
  const marker = JSON.parse(fs.readFileSync(markerPath));
  const sums = fs.readFileSync(path.join(current, 'SHA256SUMS'), 'utf8');
  if (marker.format !== 2 || marker.contract !== contract || !/^[a-f0-9]{32}$/.test(marker.generation) ||
      hash(sums) !== marker.manifestSHA256) throw Error('unpaired escrow marker');
  const expected = ['runtime.json', 'metadata.json'].map(name =>
    hash(fs.readFileSync(path.join(current, name))) + '  ' + name).join('\n') + '\n';
  if (sums !== expected) throw Error('escrow hash mismatch');
  const metadata = JSON.parse(fs.readFileSync(path.join(current, 'metadata.json')));
  if (metadata.format !== 2 || metadata.contract !== contract || metadata.generation !== marker.generation ||
      metadata.applicationConsistent !== false || metadata.dataPoint !== 'same-volume-csi-snapshot') {
    throw Error('unpaired escrow metadata');
  }
  return marker.generation;
}
async function main() {
  if (process.argv[2] === 'liveness') {
    const response = await fetch('http://127.0.0.1:3000/xrpc/_health', {signal: AbortSignal.timeout(2000)});
    if (!response.ok) throw Error('native health failed');
  } else if (process.argv[2] === 'prepare') prepare();
  else if (process.argv[2] === 'capture') capture();
  else if (process.argv[2] === 'verify') console.log(JSON.stringify({contract, generation: verify(), escrowVerified: true}));
  else throw Error('unknown capture command');
}
main().catch(() => { console.error('PDS escrow or native health failed; private output withheld'); process.exitCode = 1; });
