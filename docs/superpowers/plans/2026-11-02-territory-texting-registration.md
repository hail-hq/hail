# Territory texting registration implementation plan

> **For agentic workers:** Use superpowers:executing-plans. Execute each test cycle before implementation.

**Goal:** Include US Virgin Islands and other US NANP territories in US texting registration and its send gate.

**Architecture:** Keep campaign country US. Share the set US/AS/GU/MP/PR/VI between Python gate and database selection. Mirror that fixed set in the console grouping helper. Canada and foreign NANP destinations retain their existing treatment. Approved campaigns attach territory local numbers through existing provider adapters.

**Tech Stack:** Python/FastAPI/SQLAlchemy/pytest; TypeScript/Vitest.

**Spec:** The user's account audit and instruction to systematically fix all findings.

## Global constraints

- No merging, production changes, customer charges, or fabricated acceptance records.
- Use isolated task branches based on fetched remote defaults.
- A person's campaign still holds one number; business campaigns group by carrier and organization.
- Existing registration country remains US to avoid duplicate campaigns and fees.
- Tests use only the named local Postgres container.

## Review focus

- VI sender to mainland must not bypass registration.
- Mainland sender to a US territory must not bypass registration.
- Canada and British Virgin Islands must remain outside this domestic gate.
- Territory-only organizations must be able to file and attach a campaign.
- A late territory purchase must respect a person's single-number limit.

## Task 1: Gate and campaign selection

Files: core/hailhq/core/sms_registration_gate.py; api/hailhq/api/sms_registration.py; api/hailhq/api/routes/verifications.py; core/tests/test_sms_registration_gate.py; api/tests/test_sms_api.py; api/tests/test_verifications_api.py.

- [ ] Add parameterized territory regression cases for classification, denied sending, registration requirements, approval attachment, and late attachment.
- [ ] Run the new cases and observe failures on the current US-only checks.
- [ ] Define US_SMS_COUNTRY_CODES and replace origin and selection checks with membership. Include territory destinations in is_us.
- [ ] Run core and API tests with the local database; regenerate OpenAPI when route output changes.

## Task 2: Console registration discovery

Files: hail-website/app/console/numbers/texting-groups.ts and its tests.

- [ ] Add territory grouping cases and foreign-country controls; observe failure.
- [ ] Include the same domestic territory set in number grouping. Keep the campaign's country US.
- [ ] Run grouping and numbers component tests, typecheck, lint, and production build.

## Task 3: Verification and delivery

- [ ] Run related and full suites. Identify pre-existing failures separately.
- [ ] Review committed changes with codex review; resolve substantive findings with regression tests.
- [ ] Push only task branches; create linked PRs with exact reviewed SHA and commit counts.
