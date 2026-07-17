import { readFile, writeFile } from 'node:fs/promises';
import { join, dirname } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const __dirname = dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = join(__dirname, '..', '..');
const TELEPHONY_JSON = join(REPO_ROOT, 'costs', 'telephony.json');

// Twilio's machine-readable numbers dataset (the feed behind the pricing pages).
// The hashed path can rotate; override with SYNC_CSV_URL if Twilio moves it.
const CSV_URL =
  process.env.SYNC_CSV_URL ||
  'https://www.twilio.com/content/dam/twilio-com/pricing-data/en/csv/PMded94a0dae30eaaec0f115f22859bd38_SiteNumbersPricing.csv';

const TYPE_MAP = { Local: 'local', Mobile: 'mobile', 'Toll Free': 'toll_free', National: 'national' };
const COUNTRY_FLOOR = 40; // never shrink the allow-list below this — see plan's data-safety invariant

// Parse a single CSV line, respecting double-quoted fields (which may contain commas).
// Escaped quotes within a field are represented as "" (two consecutive quotes).
function parseCSVLine(line) {
  const cells = [];
  let current = '';
  let inQuotes = false;

  for (let i = 0; i < line.length; i++) {
    const char = line[i];
    const nextChar = line[i + 1];

    if (char === '"') {
      if (inQuotes && nextChar === '"') {
        // Escaped quote: add one quote and skip the next character
        current += '"';
        i++;
      } else {
        // Toggle quote state
        inQuotes = !inQuotes;
      }
    } else if (char === ',' && !inQuotes) {
      // Field delimiter (only outside quotes)
      cells.push(current.trim());
      current = '';
    } else {
      current += char;
    }
  }

  cells.push(current.trim());
  return cells;
}

// Minimal CSV parser that respects quoted fields per RFC 4180.
function parseCsv(text) {
  const lines = text.trim().split(/\r?\n/);
  const header = parseCSVLine(lines[0]);
  return lines.slice(1).map((line) => {
    const cells = parseCSVLine(line);
    return Object.fromEntries(header.map((h, i) => [h, (cells[i] ?? '').trim()]));
  });
}

export function mapCsvToNumbers(csvText) {
  const rows = [];
  const countries = new Set();
  for (const r of parseCsv(csvText)) {
    const number_type = TYPE_MAP[r['Phone Number Type']];
    if (!number_type) continue;
    const voice = r['Voice Enabled'] === 'Yes';
    const sms = r['SMS Enabled'] === 'Yes';
    const mms = r['MMS Enabled'] === 'Yes';
    if (!voice && !sms) continue; // a number must do something
    const priceRaw = r['Phone Number Price / month'];
    if (!/^(0|[1-9][0-9]*)(\.[0-9]{1,8})?$/.test(priceRaw)) continue;
    const iso = r['ISO'];
    countries.add(iso);
    rows.push({
      country_code: iso,
      number_type,
      display_name: `${r['Country']} ${r['Phone Number Type'].toLowerCase()}`,
      dial_code: r['Country Code'],
      usd_per_month: priceRaw,
      voice, sms, mms,
    });
  }
  rows.sort((a, b) =>
    a.country_code.localeCompare(b.country_code) || a.number_type.localeCompare(b.number_type));
  return { rows, countryCount: countries.size };
}

async function main() {
  const res = await fetch(CSV_URL);
  if (!res.ok) throw new Error(`Twilio CSV fetch failed: ${res.status} (${CSV_URL})`);
  const csv = await res.text();
  const { rows, countryCount } = mapCsvToNumbers(csv);
  if (countryCount < COUNTRY_FLOOR) {
    throw new Error(`sync aborted: only ${countryCount} countries (< ${COUNTRY_FLOOR}); refusing to shrink the allow-list`);
  }
  const existing = JSON.parse(await readFile(TELEPHONY_JSON, 'utf-8'));
  const today = new Date().toISOString().slice(0, 10);
  const prevByKey = new Map(
    (existing.numbers || []).map((n) => [`${n.country_code}:${n.number_type}`, n]));
  const numbers = rows.map((n) => {
    const prev = prevByKey.get(`${n.country_code}:${n.number_type}`);
    const changed = !prev || prev.usd_per_month !== n.usd_per_month
      || prev.voice !== n.voice || prev.sms !== n.sms || prev.mms !== n.mms;
    return {
      ...n,
      last_verified: today,
      last_changed_at: changed ? today : (prev.last_changed_at || today),
      verification_method: 'carrier-sync',
      verified_by: 'twilio-sync',
      source_url: CSV_URL,
    };
  });
  const out = { ...existing, numbers };
  await writeFile(TELEPHONY_JSON, JSON.stringify(out, null, 2) + '\n');
  console.log(`wrote ${numbers.length} rows across ${countryCount} countries`);
}

if (import.meta.url === pathToFileURL(process.argv[1]).href) {
  main().catch((err) => { console.error(err); process.exit(1); });
}
