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
  recording: ['recording'],
  llm: ['models'],
  stt: ['models'],
  tts: ['models'],
  twilio: ['numbers', 'a2p_10dlc'],
  telnyx: ['numbers'],
  didww: ['numbers'],
  sms: ['providers'],
  'sms-registration': ['providers'],
  voice: ['rates'],
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

export function findEndedPromos(rows, now = new Date()) {
  const today = now.toISOString().slice(0, 10);
  return rows.filter((row) => row.promo_ends_at && row.promo_ends_at < today);
}

function rowLabel(row) {
  const label =
    row.model_id ||
    row.display_name ||
    row.carrier ||
    row.name ||
    [row.country_code, row.direction, row.number_type].filter(Boolean).join(' ') ||
    '(row)';
  return [row.provider, label].filter(Boolean).join(' / ');
}

export function buildReport(rows, maxAge, now = new Date()) {
  const stale = findStale(rows, maxAge, now);
  const endedPromos = findEndedPromos(rows, now);
  const lines = [];

  if (stale.length > 0) {
    lines.push(`Found ${stale.length} stale row(s) (max age: ${maxAge} days):`, '');
    for (const row of stale) {
      const days = Math.floor(
        (now.getTime() - new Date(row.last_verified + 'T00:00:00Z').getTime()) / MS_PER_DAY,
      );
      lines.push(`- [${row.category}] ${rowLabel(row)} — last verified ${row.last_verified} (${days} days ago)`);
    }
  }

  if (endedPromos.length > 0) {
    if (lines.length > 0) lines.push('');
    lines.push(`Found ${endedPromos.length} row(s) with an ended promotion:`, '');
    for (const row of endedPromos) {
      lines.push(`- [${row.category}] ${rowLabel(row)} — promotion ended ${row.promo_ends_at}`);
    }
  }

  return lines;
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
      return rowsForFile(file, data);
    }),
  );
  const lines = buildReport(perFile.flat(), maxAge);

  if (lines.length === 0) {
    console.log(`No stale rows or ended promotions (max age: ${maxAge} days).`);
    process.exit(0);
  }

  console.log(lines.join('\n'));
  process.exit(1);
}

if (import.meta.url === pathToFileURL(process.argv[1]).href) {
  main().catch((err) => {
    console.error(err);
    process.exit(2);
  });
}
