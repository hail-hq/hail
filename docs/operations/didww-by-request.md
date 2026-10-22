# DIDWW numbers by request

A customer clicked **Ask support for a number** in the console: an email to
hi@hail.so with the subject `Number request: <CC> <type>` (for example
`Number request: PT national`). These are rows with `by_request: true` in
[`costs/didww.json`](../../costs/didww.json): DIDWW picks the number when the
order is placed, so Hail cannot quote and sell it
([didww.md](../public/self-host/didww.md#numbers-hail-cannot-sell)).

1. Reply with the price from the catalog row (`usd_per_month`, `setup_usd`)
   and ask for the organization id (console → Settings). `usd_per_month` is
   the cheapest area; when `notes` says the price varies, quote the area the
   customer wants.
2. If the row has `verification_required: true`, the customer first files
   their papers. The console's **Verify** button needs an offer, and these
   rows have none, so they file through the API: `GET
/v1/verifications/requirements` then `POST /v1/verifications` with
   `provider=didww` ([OpenAPI](../../openapi/openapi.yaml)). Wait for the Hail
   verification to be `approved`.
3. Order in the DIDWW panel: **my.didww.com → Buy Numbers → \<country\> →
   \<type\>**, pick the area, quantity 1, metered plan (0 channels). Link the
   customer's approved address when DIDWW asks. Note the DID id and the
   number DIDWW assigned.
4. Add the number to the organization and charge setup + first month, in
   one transaction (same ledger rows as an API purchase,
   [`finish_order`](../../api/hailhq/api/number_orders.py)). Fill the seven
   values at the top. `kind` is the catalog `number_type` (`toll_free`, not
   `toll-free`). Prices are in cents, from the DIDWW price of the area you
   ordered, not the catalog floor.

   ```bash
   psql "$DATABASE_URL" -v ON_ERROR_STOP=1 <<'SQL'
   \set org '<org id>'
   \set e164 '+<number>'
   \set cc '<CC>'
   \set kind '<type>'
   \set did '<DID id>'
   \set monthly <area monthly price x 100>
   \set setup <area setup price x 100, or 0>
   BEGIN;
   -- Same lock and balance rule as an API purchase (POST /v1/numbers answers 402).
   SELECT pg_advisory_xact_lock(hashtextextended(:'org', 0));
   SELECT COALESCE(SUM(amount_cents), 0) >= :monthly + :setup AS funded
   FROM account_credits WHERE organization_id = :'org' \gset
   \if :funded
   \else
     \echo 'Balance too low for setup + first month: nothing added, nothing charged. Ask the customer to top up.'
     ROLLBACK;
     \q
   \endif
   INSERT INTO phone_numbers (id, organization_id, e164, country_code, number_type, capabilities, provider, provider_resource_id, provisioning_state, provisioning_metadata, is_pool, acquired_at)
   VALUES (gen_random_uuid(), :'org', :'e164', :'cc', :'kind', ARRAY['voice'], 'didww', :'did', 'active', jsonb_build_object('monthly_cents', :monthly, 'currency', 'USD', 'order_state', 'complete'), FALSE, now())
   RETURNING id \gset num_
   INSERT INTO account_credits (organization_id, kind, channel, amount_cents, qty, ref, source)
   VALUES (:'org', 'debit', 'voice', -:monthly, 1, 'monthly_fee:' || :'org' || ':' || :'num_id' || ':dedicated_number:' || to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM'), 'monthly_fee');
   INSERT INTO account_credits (organization_id, kind, channel, amount_cents, qty, ref, source)
   SELECT :'org', 'debit', 'voice', -:setup, 1, 'number_setup:' || :'num_id', 'number_setup' WHERE :setup > 0;
   COMMIT;
   SQL
   ```

   The monthly-fee ref has the format of `monthly_fee_ref`
   ([`billing.py`](../../core/hailhq/core/billing.py)), so the website's
   renewal rater does not charge the first month again. `monthly_cents` is
   the price the rater falls back to when the catalog row's price varies by
   area, as for an API purchase.

5. Tell the customer the number. It shows in **Console → Numbers**. The
   monthly fee renews like any other number; releasing
   it (`DELETE /v1/numbers/{id}`) terminates the DID.
