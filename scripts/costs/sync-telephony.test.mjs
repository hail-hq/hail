import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mapCsvToNumbers } from './sync-telephony.mjs';

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
