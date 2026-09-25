import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mapCsvToNumbers, mergeRows } from './sync-telephony.mjs';

const CSV =
  'ISO,Country,Country Code,Phone Number Type,Voice Enabled,Trunking Enabled,SMS Enabled,MMS Enabled,Domestic Voice Only,Domestic SMS Only,Phone Number Price / month\n' +
  'SE,Sweden,46,Mobile,No,No,Yes,No,N/A,No,3.00\n' +
  'US,United States,1,Local,Yes,Yes,Yes,Yes,No,N/A,1.15\n' +
  'GB,United Kingdom,44,Toll Free,Yes,Yes,No,No,Yes,N/A,2.15\n' +
  'XX,Nowhere,999,Local,No,No,No,No,N/A,N/A,5.00\n' + // neither voice nor sms — dropped
  'JP,Japan,81,National,Yes,Yes,No,No,Yes,N/A,4.50\n';

test('maps capabilities and price, dropping no-capability rows', () => {
  const { rows, countryCount } = mapCsvToNumbers(CSV);
  const se = rows.find((r) => r.country_code === 'SE');
  assert.equal(se.number_type, 'mobile');
  assert.equal(se.voice, false);
  assert.equal(se.sms, true);
  assert.equal(se.mms, false);
  assert.equal(se.usd_per_month, '3.00');
  assert.equal(se.dial_code, '46');
  const us = rows.find((r) => r.country_code === 'US');
  assert.deepEqual([us.voice, us.sms, us.mms], [true, true, true]);
  const jp = rows.find((r) => r.country_code === 'JP');
  assert.equal(jp.number_type, 'national');
  // XX row (no voice, no sms) is dropped
  assert.equal(rows.some((r) => r.country_code === 'XX'), false);
  assert.equal(countryCount, 4); // SE, US, GB, JP
});

test('prices are decimal strings, not numbers', () => {
  const { rows } = mapCsvToNumbers(CSV);
  assert.equal(typeof rows[0].usd_per_month, 'string');
});

test('parses quoted CSV fields with commas', () => {
  const csvWithQuotedComma =
    'ISO,Country,Country Code,Phone Number Type,Voice Enabled,Trunking Enabled,SMS Enabled,MMS Enabled,Domestic Voice Only,Domestic SMS Only,Phone Number Price / month\n' +
    'US,United States,1,Local,Yes,Yes,Yes,Yes,No,N/A,1.15\n' +
    'VI,"Virgin Islands, U.S.",1340,Local,Yes,Yes,Yes,No,No,N/A,1.15\n';
  const { rows } = mapCsvToNumbers(csvWithQuotedComma);
  const vi = rows.find((r) => r.country_code === 'VI');
  assert.ok(vi, 'VI row should be present');
  assert.equal(vi.country_code, 'VI');
  assert.equal(vi.dial_code, '1340');
  assert.equal(vi.number_type, 'local');
  assert.equal(vi.usd_per_month, '1.15');
  assert.equal(vi.voice, true);
  assert.equal(vi.sms, true);
  assert.equal(vi.mms, false);
});

test('a hand-verified row is kept when the feed disagrees, and reported', () => {
  const existing = [
    {
      country_code: 'GB', number_type: 'mobile', display_name: 'United Kingdom mobile', dial_code: '44',
      usd_per_month: '2.50', voice: true, sms: true, mms: false,
      last_verified: '2026-09-24', last_changed_at: '2026-09-24',
      verification_method: 'manual-confirmed', verified_by: 'twilio-api',
      source_url: 'https://www.twilio.com/docs/phone-numbers/pricing',
    },
    {
      country_code: 'US', number_type: 'local', display_name: 'United States local', dial_code: '1',
      usd_per_month: '1.15', voice: true, sms: true, mms: true,
      last_verified: '2026-07-17', last_changed_at: '2026-07-17',
      verification_method: 'carrier-sync', verified_by: 'twilio-sync', source_url: 'https://example.test/csv',
    },
  ];
  const feed = [
    { country_code: 'GB', number_type: 'mobile', display_name: 'United Kingdom mobile', dial_code: '44', usd_per_month: '1.15', voice: true, sms: true, mms: false },
    { country_code: 'US', number_type: 'local', display_name: 'United States local', dial_code: '1', usd_per_month: '1.20', voice: true, sms: true, mms: true },
  ];
  const { numbers, kept } = mergeRows(existing, feed, '2026-09-28');
  const gb = numbers.find((r) => r.country_code === 'GB');
  assert.equal(gb.usd_per_month, '2.50');
  assert.equal(gb.verification_method, 'manual-confirmed');
  assert.equal(gb.last_verified, '2026-09-24');
  const us = numbers.find((r) => r.country_code === 'US');
  assert.equal(us.usd_per_month, '1.20');
  assert.equal(us.last_changed_at, '2026-09-28');
  assert.equal(us.verification_method, 'carrier-sync');
  assert.equal(kept.length, 1);
  assert.equal(kept[0].key, 'GB:mobile');
});

test('a hand-verified row that agrees with the feed is kept silently', () => {
  const existing = [{
    country_code: 'GB', number_type: 'local', display_name: 'United Kingdom local', dial_code: '44',
    usd_per_month: '1.15', voice: true, sms: false, mms: false,
    last_verified: '2026-09-24', last_changed_at: '2026-09-24',
    verification_method: 'manual-confirmed', verified_by: 'twilio-api', source_url: 'https://example.test',
  }];
  const feed = [{ country_code: 'GB', number_type: 'local', display_name: 'United Kingdom local', dial_code: '44', usd_per_month: '1.15', voice: true, sms: false, mms: false }];
  const { kept } = mergeRows(existing, feed, '2026-09-28');
  assert.equal(kept.length, 0);
});
