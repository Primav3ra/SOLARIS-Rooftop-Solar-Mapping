/**
 * Bundle budget, asserted rather than trusted.
 *
 * One rule matters: **the content routes must not download three.js.** It is
 * about 700 kB and the Validation, Method, Data and Limitations pages have no
 * use for it. Route-splitting makes that true; nothing except a check keeps it
 * true, because a stray import in a shared component would silently pull the
 * WebGL chunk into the main bundle and nobody would notice from looking at the
 * site.
 *
 * Run after `npm run build`. Exits non-zero in CI on a regression.
 */

import { readdirSync, statSync, readFileSync } from 'node:fs';
import { join, dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const here = dirname(fileURLToPath(import.meta.url));
const assets = resolve(here, '../../../src/solaris/api/static/assets');

// Bytes. Generous enough not to be noise, tight enough to catch a real
// regression -- an accidental three.js import would blow the entry budget by
// an order of magnitude, not a few per cent.
const BUDGETS = {
  entry: 320 * 1024,
  'intro-webgl': 1200 * 1024,
  map: 1100 * 1024,
  charts: 320 * 1024,
  vendor: 420 * 1024,
};

// Markers that mean three.js ended up somewhere it should not have. Checked by
// content rather than by filename, because the filename is a hash.
const WEBGL_MARKERS = ['WebGLRenderer', 'THREE.'];

let files;
try {
  files = readdirSync(assets).filter((f) => f.endsWith('.js'));
} catch {
  console.error(`No build output at ${assets}. Run \`npm run build\` first.`);
  process.exit(1);
}

let failed = false;
const report = [];

for (const file of files) {
  const path = join(assets, file);
  const size = statSync(path).size;
  const chunk = Object.keys(BUDGETS).find((name) => file.includes(name)) ?? 'entry';
  const budget = BUDGETS[chunk];
  const over = size > budget;
  if (over) failed = true;
  report.push({ file, chunk, size, budget, over });

  // The load-bearing assertion.
  if (!file.includes('intro-webgl')) {
    const source = readFileSync(path, 'utf8');
    const marker = WEBGL_MARKERS.find((m) => source.includes(m));
    if (marker) {
      console.error(
        `FAIL  ${file} contains "${marker}" -- three.js has leaked out of the ` +
          `intro chunk. Check for a static import of the intro or a three.js ` +
          `symbol in a shared component.`,
      );
      failed = true;
    }
  }
}

const kb = (n) => `${(n / 1024).toFixed(0)} kB`;
report.sort((a, b) => b.size - a.size);
for (const row of report) {
  const status = row.over ? 'OVER' : 'ok  ';
  console.log(`${status}  ${row.file.padEnd(34)} ${kb(row.size).padStart(9)} / ${kb(row.budget)}`);
}

if (failed) {
  console.error('\nBundle budget exceeded.');
  process.exit(1);
}
console.log('\nBundle budget ok.');
