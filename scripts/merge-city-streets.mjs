#!/usr/bin/env node
// Merge per-extract street results into one file per city, plus a manifest.
//
//   node scripts/merge-city-streets.mjs                       # read from R2, write ./out
//   node scripts/merge-city-streets.mjs --extracts ./extracts # read a local dir of extract JSONs
//   node scripts/merge-city-streets.mjs --out ./out --cap 320
//
// Extract inputs are the JSON files written by city-streets-pbf.py, one per
// Geofabrik extract. They are read from, in order of preference:
//   1. --extracts <dir>            local directory of *.json
//   2. $EXTRACT_DIR                local directory of *.json
//   3. s3://<bucket>/data/osm-city-streets/   via R2_* env vars
//
// Output: <out>/<city-slug>.json and <out>/city-streets-manifest.json
//
// City list: data/cities-pop2m.json (pop >= 2,000,000). Pass --cities to override.

import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const REPO = path.resolve(HERE, '..');

const args = process.argv.slice(2);
const flag = (name, dflt) => {
  const i = args.indexOf('--' + name);
  return i === -1 ? dflt : args[i + 1];
};

const OUT_DIR = path.resolve(flag('out', process.env.OUT_DIR || path.join(REPO, 'out')));
const EXTRACT_DIR = flag('extracts', process.env.EXTRACT_DIR || null);
const CITIES_FILE = path.resolve(flag('cities', path.join(REPO, 'data/cities-pop2m.json')));
const R2_PREFIX = process.env.R2_PREFIX || 'data/osm-city-streets/';
const CAP = Number(flag('cap', process.env.STREET_CAP || 320));

const log = (m) => console.log(`[${new Date().toISOString().slice(11, 19)}] ${m}`);

// Canonical dedupe key: accent-folded, case-folded name + 2-decimal coords.
// Two OSM ways carrying the same name at the same spot collapse to one row.
const canon = (n) =>
  !n
    ? ''
    : String(n)
        .normalize('NFD')
        .replace(/[\u0300-\u036f]/g, '')
        .toLowerCase()
        .replace(/['’.]/g, '')
        .replace(/\s+/g, ' ')
        .trim();

const dedupeKey = (s) => `${canon(s.n) || canon(s.raw)}@${Number(s.la).toFixed(2)},${Number(s.lo).toFixed(2)}`;

function readExtractsFromDir(dir) {
  const byCity = new Map();
  let files = 0;
  for (const f of fs.readdirSync(dir).filter((f) => f.endsWith('.json'))) {
    let d;
    try {
      d = JSON.parse(fs.readFileSync(path.join(dir, f), 'utf8'));
    } catch {
      log(`  skip unreadable ${f}`);
      continue;
    }
    files++;
    for (const [slug, entry] of Object.entries(d.cities || {})) {
      if (!byCity.has(slug)) byCity.set(slug, []);
      byCity.get(slug).push(...(entry.streets || []));
    }
  }
  log(`loaded ${files} extract file(s) from ${dir}`);
  return byCity;
}

async function readExtractsFromR2() {
  const { S3Client, ListObjectsV2Command, GetObjectCommand } = await import('@aws-sdk/client-s3');
  const bucket = process.env.R2_BUCKET || 'geo-datalake';
  const endpoint =
    process.env.R2_ENDPOINT ||
    (process.env.R2_ACCOUNT_ID ? `https://${process.env.R2_ACCOUNT_ID}.r2.cloudflarestorage.com` : null);
  if (!endpoint || !process.env.R2_ACCESS_KEY_ID || !process.env.R2_SECRET_ACCESS_KEY) {
    throw new Error(
      'no --extracts dir and no R2 credentials. Set R2_ENDPOINT (or R2_ACCOUNT_ID), R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY.'
    );
  }
  const s3 = new S3Client({
    region: 'auto',
    endpoint,
    credentials: {
      accessKeyId: process.env.R2_ACCESS_KEY_ID,
      secretAccessKey: process.env.R2_SECRET_ACCESS_KEY,
    },
  });
  const byCity = new Map();
  let files = 0;
  let token;
  do {
    const res = await s3.send(new ListObjectsV2Command({ Bucket: bucket, Prefix: R2_PREFIX, ContinuationToken: token }));
    for (const obj of res.Contents || []) {
      if (!obj.Key.endsWith('.json')) continue;
      const body = await s3.send(new GetObjectCommand({ Bucket: bucket, Key: obj.Key }));
      let d;
      try {
        d = JSON.parse(await body.Body.transformToString());
      } catch {
        continue;
      }
      files++;
      for (const [slug, entry] of Object.entries(d.cities || {})) {
        if (!byCity.has(slug)) byCity.set(slug, []);
        byCity.get(slug).push(...(entry.streets || []));
      }
    }
    token = res.IsTruncated ? res.NextContinuationToken : undefined;
  } while (token);
  log(`loaded ${files} extract result file(s) from s3://${bucket}/${R2_PREFIX}`);
  return byCity;
}

const { cities } = JSON.parse(fs.readFileSync(CITIES_FILE, 'utf8'));
log(`${cities.length} cities in ${path.relative(REPO, CITIES_FILE)}`);

const byCity = EXTRACT_DIR ? readExtractsFromDir(path.resolve(EXTRACT_DIR)) : await readExtractsFromR2();

fs.mkdirSync(OUT_DIR, { recursive: true });

const manifest = {};
const rows = [];
for (const c of cities) {
  const raw = byCity.get(c.slug) || [];
  const byKey = new Map();
  for (const s of raw) byKey.set(dedupeKey(s), s);
  const merged = [...byKey.values()].slice(0, CAP);
  merged.forEach((s, i) => {
    s.rank = i + 1;
  });
  const out = {
    slug: c.slug,
    name: c.name,
    center: { lat: c.lat, lng: c.lng },
    pop: c.pop,
    count: merged.length,
    streets: merged,
  };
  fs.writeFileSync(path.join(OUT_DIR, `${c.slug}.json`), JSON.stringify(out));
  manifest[c.slug] = {
    name: c.name,
    pop: c.pop,
    count: merged.length,
    sample: merged.slice(0, 3).map((s) => s.n),
  };
  rows.push({ slug: c.slug, pop: c.pop, raw: raw.length, uniq: byKey.size, after: merged.length });
}

fs.writeFileSync(path.join(OUT_DIR, 'city-streets-manifest.json'), JSON.stringify(manifest, null, 1));

rows.sort((a, b) => a.after - b.after);
const ge = rows.filter((r) => r.after >= 300).length;
log(`merged ${rows.length} cities | >=${Math.min(300, CAP)} streets: ${ge} | capped at ${CAP}`);
log(`cities with 0 streets: ${rows.filter((r) => r.after === 0).length}`);
log(`smallest: ${rows.slice(0, 5).map((r) => `${r.slug}=${r.after}`).join(', ')}`);
log(`wrote ${OUT_DIR}`);
