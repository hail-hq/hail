import { test } from 'node:test';
import assert from 'node:assert/strict';
import { findStale, rowsForFile } from './check-stale.mjs';

const today = new Date('2026-05-05T00:00:00Z');

test('returns empty when all rows are fresh', () => {
  const rows = [
    { model_id: 'a', last_verified: '2026-05-01' },
    { model_id: 'b', last_verified: '2026-04-20' },
  ];
  assert.deepEqual(findStale(rows, 30, today), []);
});

test('returns rows older than max age', () => {
  const rows = [
    { model_id: 'fresh', last_verified: '2026-05-01' },
    { model_id: 'stale', last_verified: '2026-03-01' },
  ];
  const stale = findStale(rows, 30, today);
  assert.equal(stale.length, 1);
  assert.equal(stale[0].model_id, 'stale');
});

test('boundary: exactly maxAge days old is not stale', () => {
  const rows = [{ model_id: 'edge', last_verified: '2026-04-05' }]; // 30 days
  assert.deepEqual(findStale(rows, 30, today), []);
});

test('boundary: maxAge + 1 days old is stale', () => {
  const rows = [{ model_id: 'edge', last_verified: '2026-04-04' }]; // 31 days
  const stale = findStale(rows, 30, today);
  assert.equal(stale.length, 1);
});

test('rowsForFile flattens a model-shaped file', () => {
  const rows = rowsForFile('llm.json', { models: [{ model_id: 'a', last_verified: '2026-01-01' }] });
  assert.equal(rows.length, 1);
  assert.equal(rows[0].model_id, 'a');
});

test('rowsForFile flattens telephony across both arrays', () => {
  const rows = rowsForFile('telephony.json', {
    numbers: [{ display_name: 'US local', last_verified: '2026-01-01' }],
    a2p_10dlc: [{ carrier: 'AT&T', last_verified: '2026-01-02' }],
  });
  assert.equal(rows.length, 2);
});

test('rowsForFile throws on an unknown data file', () => {
  assert.throws(() => rowsForFile('mystery.json', { foo: [] }), /unknown costs file/);
});
