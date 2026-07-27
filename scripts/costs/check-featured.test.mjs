import { test } from 'node:test';
import assert from 'node:assert/strict';
import { pairSlug, featuredIds, pairSlugs, checkInvariants } from './check-featured.mjs';

test('pairSlug sorts ids deterministically', () => {
  assert.equal(pairSlug('gpt-5.5', 'claude-opus-5'), 'claude-opus-5-vs-gpt-5.5');
  assert.equal(pairSlug('claude-opus-5', 'gpt-5.5'), 'claude-opus-5-vs-gpt-5.5');
});

test('featuredIds selects only flagged rows', () => {
  const data = {
    models: [
      { model_id: 'a', featured: true },
      { model_id: 'b' },
      { model_id: 'c', featured: false },
      { model_id: 'd', featured: true },
    ],
  };
  assert.deepEqual(featuredIds(data), ['a', 'd']);
});

test('featuredIds keeps deprecated rows', () => {
  const data = { models: [{ model_id: 'old', featured: true, deprecated_at: '2026-01-01' }] };
  assert.deepEqual(featuredIds(data), ['old']);
});

test('pairSlugs produces every unordered pair, sorted', () => {
  assert.deepEqual(pairSlugs(['b', 'a', 'c']), ['a-vs-b', 'a-vs-c', 'b-vs-c']);
});

test('pairSlugs of two ids is one pair', () => {
  assert.deepEqual(pairSlugs(['x', 'y']), ['x-vs-y']);
});

test('passes when lock matches and invariants hold', () => {
  const cat = (ids) => ({ models: ids.map((id) => ({ model_id: id, featured: true })) });
  const errors = checkInvariants({
    llm: cat(['a', 'b']),
    stt: cat(['c', 'd']),
    tts: cat(['e', 'f']),
    lock: { slugs: ['a-vs-b', 'c-vs-d', 'e-vs-f'] },
  });
  assert.deepEqual(errors, []);
});

test('fails when a category has fewer than two featured models', () => {
  const cat = (ids) => ({ models: ids.map((id) => ({ model_id: id, featured: true })) });
  const errors = checkInvariants({
    llm: cat(['a']),
    stt: cat(['c', 'd']),
    tts: cat(['e', 'f']),
    lock: { slugs: ['c-vs-d', 'e-vs-f'] },
  });
  assert.equal(errors.length, 1);
  assert.match(errors[0], /llm has 1 featured model/);
});

test('fails when a locked slug disappears', () => {
  const cat = (ids) => ({ models: ids.map((id) => ({ model_id: id, featured: true })) });
  const errors = checkInvariants({
    llm: cat(['a', 'b']),
    stt: cat(['c', 'd']),
    tts: cat(['e', 'f']),
    lock: { slugs: ['a-vs-b', 'a-vs-z', 'c-vs-d', 'e-vs-f'] },
  });
  assert.equal(errors.length, 1);
  assert.match(errors[0], /a-vs-z/);
  assert.match(errors[0], /no longer generated/);
});

test('fails when a featured replaced_by_model_id does not resolve', () => {
  const errors = checkInvariants({
    llm: {
      models: [
        { model_id: 'a', featured: true, replaced_by_model_id: 'ghost' },
        { model_id: 'b', featured: true },
      ],
    },
    stt: { models: [{ model_id: 'c', featured: true }, { model_id: 'd', featured: true }] },
    tts: { models: [{ model_id: 'e', featured: true }, { model_id: 'f', featured: true }] },
    lock: { slugs: ['a-vs-b', 'c-vs-d', 'e-vs-f'] },
  });
  assert.equal(errors.length, 1);
  assert.match(errors[0], /ghost/);
});

test('reports new slugs missing from the lock', () => {
  const cat = (ids) => ({ models: ids.map((id) => ({ model_id: id, featured: true })) });
  const errors = checkInvariants({
    llm: cat(['a', 'b', 'c']),
    stt: cat(['d', 'e']),
    tts: cat(['f', 'g']),
    lock: { slugs: ['a-vs-b', 'd-vs-e', 'f-vs-g'] },
  });
  assert.equal(errors.length, 1);
  assert.match(errors[0], /--write/);
});
