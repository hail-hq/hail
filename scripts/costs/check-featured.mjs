import { readFile, writeFile } from 'node:fs/promises';
import { join, dirname } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const __dirname = dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = join(__dirname, '..', '..');
const DATA_DIR = join(REPO_ROOT, 'costs');
const LOCK_PATH = join(DATA_DIR, 'featured.lock.json');

const CATEGORIES = ['llm', 'stt', 'tts'];
const MIN_FEATURED_PER_CATEGORY = 2;

// Sorted with bare .sort() (code-unit order) rather than localeCompare, which is
// locale-dependent and would yield different route sets on different machines.
export function pairSlug(a, b) {
  return [a, b].sort().join('-vs-');
}

export function featuredIds(data) {
  return data.models.filter((m) => m.featured === true).map((m) => m.model_id);
}

export function pairSlugs(ids) {
  const sorted = [...ids].sort();
  const out = [];
  for (let i = 0; i < sorted.length; i++) {
    for (let j = i + 1; j < sorted.length; j++) {
      out.push(pairSlug(sorted[i], sorted[j]));
    }
  }
  return out.sort();
}

export function computeSlugs({ llm, stt, tts }) {
  return [
    ...pairSlugs(featuredIds(llm)),
    ...pairSlugs(featuredIds(stt)),
    ...pairSlugs(featuredIds(tts)),
  ].sort();
}

export function checkInvariants({ llm, stt, tts, lock }) {
  const data = { llm, stt, tts };
  const errors = [];

  for (const category of CATEGORIES) {
    const ids = featuredIds(data[category]);
    if (ids.length < MIN_FEATURED_PER_CATEGORY) {
      errors.push(
        `${category} has ${ids.length} featured model(s); at least ${MIN_FEATURED_PER_CATEGORY} are required or its comparison pages vanish`,
      );
    }
    const allIds = new Set(data[category].models.map((m) => m.model_id));
    for (const row of data[category].models) {
      if (row.featured !== true) continue;
      if (row.replaced_by_model_id && !allIds.has(row.replaced_by_model_id)) {
        errors.push(
          `${category}: featured model ${row.model_id} has replaced_by_model_id "${row.replaced_by_model_id}" which does not resolve in-file`,
        );
      }
    }
  }

  const computed = computeSlugs(data);
  const locked = [...(lock?.slugs ?? [])].sort();

  const removed = locked.filter((s) => !computed.includes(s));
  if (removed.length > 0) {
    errors.push(
      `these slugs are in costs/featured.lock.json but are no longer generated, so previously indexed pages would 404: ${removed.join(', ')} — do not run \`pnpm costs:featured --write\` to silence this; restore \`featured: true\`, or hand-edit costs/featured.lock.json so the removal is reviewable.`,
    );
  }

  const added = computed.filter((s) => !locked.includes(s));
  if (added.length > 0) {
    errors.push(
      `${added.length} new slug(s) are not in the lockfile; run \`pnpm costs:featured --write\` and commit costs/featured.lock.json: ${added.join(', ')}`,
    );
  }

  return errors;
}

async function readCategory(name) {
  return JSON.parse(await readFile(join(DATA_DIR, `${name}.json`), 'utf-8'));
}

async function main() {
  const write = process.argv.includes('--write');
  const [llm, stt, tts] = await Promise.all(CATEGORIES.map(readCategory));

  if (write) {
    const slugs = computeSlugs({ llm, stt, tts });
    await writeFile(LOCK_PATH, JSON.stringify({ slugs }, null, 2) + '\n', 'utf-8');
    console.log(`Wrote ${slugs.length} slug(s) to costs/featured.lock.json`);
    process.exit(0);
  }

  let lock;
  try {
    lock = JSON.parse(await readFile(LOCK_PATH, 'utf-8'));
  } catch {
    console.error('costs/featured.lock.json is missing. Run `pnpm costs:featured --write`.');
    process.exit(1);
  }

  const errors = checkInvariants({ llm, stt, tts, lock });
  if (errors.length === 0) {
    console.log(`Featured set OK (${lock.slugs.length} comparison pages).`);
    process.exit(0);
  }

  for (const err of errors) console.error(`- ${err}`);
  process.exit(1);
}

if (import.meta.url === pathToFileURL(process.argv[1]).href) {
  main().catch((err) => {
    console.error(err);
    process.exit(2);
  });
}
