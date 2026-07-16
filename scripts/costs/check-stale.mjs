import { readFile, readdir } from 'node:fs/promises';
import { join, dirname } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const __dirname = dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = join(__dirname, '..', '..');
const DATA_DIR = join(REPO_ROOT, 'costs');

const MS_PER_DAY = 1000 * 60 * 60 * 24;

// Which array(s) hold staleable rows in each costs file. An unrecognized
// file is a hard error, not a silent skip: silently skipping is exactly how a
// price file could rot unnoticed while the invoice keeps citing it.
const ROW_ARRAYS = {
  llm: ['models'],
  stt: ['models'],
  tts: ['models'],
  telephony: ['numbers', 'a2p_10dlc'],
};

export function rowsForFile(filename, data) {
  const category = filename.replace(/\.json$/, '');
  const keys = ROW_ARRAYS[category];
  if (!keys) {
    throw new Error(`unknown costs file: ${filename} (add it to ROW_ARRAYS)`);
  }
  const rows = [];
  for (const key of keys) {
    const arr = data?.[key];
    if (!Array.isArray(arr)) {
      throw new Error(`${filename}: expected array at \`${key}\``);
    }
    for (const row of arr) rows.push({ category, ...row });
  }
  return rows;
}

export function findStale(rows, maxAgeDays, now = new Date()) {
  const cutoffMs = now.getTime() - maxAgeDays * MS_PER_DAY;
  return rows.filter((row) => {
    const verifiedMs = new Date(row.last_verified + 'T00:00:00Z').getTime();
    return verifiedMs < cutoffMs;
  });
}

async function main() {
  const args = process.argv.slice(2);
  const maxAgeIdx = args.indexOf('--max-age');
  const maxAge = maxAgeIdx >= 0 ? Number(args[maxAgeIdx + 1]) : 30;

  if (!Number.isFinite(maxAge) || maxAge <= 0) {
    console.error('--max-age requires a positive number');
    process.exit(2);
  }

  const entries = await readdir(DATA_DIR, { withFileTypes: true });
  const files = entries
    .filter((e) => e.isFile() && e.name.endsWith('.json'))
    .map((e) => e.name);

  const perFile = await Promise.all(
    files.map(async (file) => {
      const data = JSON.parse(await readFile(join(DATA_DIR, file), 'utf-8'));
      const rows = rowsForFile(file, data);
      return findStale(rows, maxAge);
    }),
  );
  const results = perFile.flat();

  if (results.length === 0) {
    console.log(`No stale rows (max age: ${maxAge} days).`);
    process.exit(0);
  }

  console.log(`Found ${results.length} stale row(s) (max age: ${maxAge} days):\n`);
  for (const row of results) {
    const days = Math.floor(
      (Date.now() - new Date(row.last_verified + 'T00:00:00Z').getTime()) / MS_PER_DAY,
    );
    const label = row.model_id || row.display_name || row.carrier || '(row)';
    console.log(`- [${row.category}] ${row.provider ?? ''} ${label} — last verified ${row.last_verified} (${days} days ago)`.replace(/\s+/g, ' '));
  }
  process.exit(1);
}

if (import.meta.url === pathToFileURL(process.argv[1]).href) {
  main().catch((err) => {
    console.error(err);
    process.exit(2);
  });
}
