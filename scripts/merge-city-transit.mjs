#!/usr/bin/env node
// Merge per-extract OSM transit results into one file per city plus a manifest.
//
//   node scripts/merge-city-transit.mjs
//   node scripts/merge-city-transit.mjs --extracts ./extracts --out ./out
//
// City list: data/cities-pop2m.json (pop >= 2,000,000).
//
// A city can appear in more than one country extract (e.g. gcc-states holds
// Dubai, Riyadh, Jeddah, Mecca and Kuwait), so counts are summed per city and
// routes are deduped on OSM relation id — the same relation can be clipped to
// two different boxes if their bboxes overlap.

import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const REPO = path.resolve(HERE, '..');

const argv = process.argv.slice(2);
const FLAGS = new Set(['out', 'extracts', 'cities', 'cap']);
const flag = {};
const positional = [];
for (let i = 0; i < argv.length; i++) {
  const a = argv[i];
  if (!a.startsWith('--')) {
    positional.push(a);
    continue;
  }
  const n = a.slice(2);
  if (n === 'help') {
    console.log('node scripts/merge-city-transit.mjs [--extracts <dir>] [--out <dir>] [--cities <file>]');
    process.exit(0);
  }
  flag[n] = FLAGS.has(n) ? argv[++i] : true;
}

const OUT_DIR = path.resolve(flag.out || process.env.OUT_DIR || path.join(REPO, 'out'));
const EXTRACT_DIR = flag.extracts || process.env.EXTRACT_DIR || null;
const CITIES_FILE = path.resolve(flag.cities || path.join(REPO, 'data/cities-pop2m.json'));
const R2_PREFIX = process.env.R2_PREFIX || 'data/osm-city-transit/';
const R2_BUCKET = process.env.R2_BUCKET || 'geo-datalake';
const MAX_ROUTES = Number(process.env.MAX_ROUTES || 400);

const log = (m) => console.log(`[${new Date().toISOString().slice(11, 19)}] ${m}`);

async function readExtracts() {
  const byCity = new Map();
  let files = 0;

  if (EXTRACT_DIR) {
    for (const f of fs.readdirSync(path.resolve(EXTRACT_DIR)).filter((f) => f.endsWith('.json'))) {
      let d;
      try {
        d = JSON.parse(fs.readFileSync(path.join(path.resolve(EXTRACT_DIR), f), 'utf8'));
      } catch {
        continue;
      }
      files++;
      for (const [slug, e] of Object.entries(d.cities || {})) {
        if (!byCity.has(slug)) byCity.set(slug, []);
        byCity.get(slug).push(e);
      }
    }
    log(`loaded ${files} extract file(s) from ${EXTRACT_DIR}`);
    return byCity;
  }

  const { S3Client, ListObjectsV2Command, GetObjectCommand } = await import('@aws-sdk/client-s3');
  if (!process.env.R2_ENDPOINT || !process.env.R2_ACCESS_KEY_ID || !process.env.R2_SECRET_ACCESS_KEY) {
    throw new Error('no --extracts dir and no R2 credentials');
  }
  const s3 = new S3Client({
    region: 'auto',
    endpoint: process.env.R2_ENDPOINT,
    credentials: { accessKeyId: process.env.R2_ACCESS_KEY_ID, secretAccessKey: process.env.R2_SECRET_ACCESS_KEY },
  });
  let token;
  do {
    const res = await s3.send(new ListObjectsV2Command({ Bucket: R2_BUCKET, Prefix: R2_PREFIX, ContinuationToken: token }));
    for (const obj of res.Contents || []) {
      if (!obj.Key.endsWith('.json')) continue;
      const body = await s3.send(new GetObjectCommand({ Bucket: R2_BUCKET, Key: obj.Key }));
      let d;
      try {
        d = JSON.parse(await body.Body.transformToString());
      } catch {
        continue;
      }
      files++;
      for (const [slug, e] of Object.entries(d.cities || {})) {
        if (!byCity.has(slug)) byCity.set(slug, []);
        byCity.get(slug).push(e);
      }
    }
    token = res.IsTruncated ? res.NextContinuationToken : undefined;
  } while (token);
  log(`loaded ${files} extract result file(s) from s3://${R2_BUCKET}/${R2_PREFIX}`);
  return byCity;
}

const { cities } = JSON.parse(fs.readFileSync(CITIES_FILE, 'utf8'));
log(`${cities.length} cities in ${path.relative(REPO, CITIES_FILE)}`);

const byCity = await readExtracts();
fs.mkdirSync(OUT_DIR, { recursive: true });

const manifest = {};
const rows = [];
for (const c of cities) {
  const parts = byCity.get(c.slug) || [];
  const seenRoute = new Set();
  const routes = [];
  const operators = new Set();
  const networks = new Set();
  const byMode = {};
  let stops = 0;
  for (const p of parts) {
    stops += p.counts?.stops || 0;
    for (const op of p.operators || []) operators.add(op);
    for (const n of p.networks || []) networks.add(n);
    for (const r of p.routes || []) {
      if (seenRoute.has(r.id)) continue;
      seenRoute.add(r.id);
      routes.push(r);
      byMode[r.mode] = (byMode[r.mode] || 0) + 1;
    }
  }
  routes.sort((a, b) => String(a.mode).localeCompare(String(b.mode)) || String(a.name || '').localeCompare(String(b.name || '')));
  const trimmed = routes.slice(0, MAX_ROUTES);
  const withGeom = trimmed.filter((r) => r.spans && r.spans.length).length;

  const out = {
    slug: c.slug,
    name: c.name,
    center: { lat: c.lat, lng: c.lng },
    pop: c.pop,
    counts: {
      routes: trimmed.length,
      routesBeforeCap: routes.length,
      stops,
      byMode,
      operators: operators.size,
      networks: networks.size,
      withGeometry: withGeom,
    },
    routes: trimmed,
  };
  fs.writeFileSync(path.join(OUT_DIR, `${c.slug}.json`), JSON.stringify(out));
  manifest[c.slug] = out.counts;
  rows.push({ slug: c.slug, pop: c.pop, ...out.counts });
}

fs.writeFileSync(path.join(OUT_DIR, 'city-transit-manifest.json'), JSON.stringify(manifest, null, 1));

rows.sort((a, b) => b.routes - a.routes);
const withRoutes = rows.filter((r) => r.routes > 0);
log(`merged ${rows.length} cities | with routes: ${withRoutes.length} | total routes: ${rows.reduce((s, r) => s + r.routes, 0)} | total stops: ${rows.reduce((s, r) => s + r.stops, 0)}`);
log(`capped by MAX_ROUTES=${MAX_ROUTES}: ${rows.filter((r) => r.routesBeforeCap > r.routes).length} cities`);
const modeTotals = {};
for (const r of rows) for (const [m, n] of Object.entries(r.byMode || {})) modeTotals[m] = (modeTotals[m] || 0) + n;
log(`by mode: ${JSON.stringify(modeTotals)}`);
log(`cities with zero routes: ${rows.filter((r) => r.routes === 0).length}`);
log(`top: ${rows.slice(0, 8).map((r) => `${r.slug}=${r.routes}`).join(' ')}`);
log(`wrote ${OUT_DIR}`);
