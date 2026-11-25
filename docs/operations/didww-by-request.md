# DIDWW numbers by request

A customer clicked **Ask support for a number** in the console: an email to
hi@hail.so with the subject `Number request: <CC> <type>` (for example
`Number request: PT national`). These are rows with `by_request: true` in
[`costs/didww.json`](../../costs/didww.json): DIDWW picks the number when the
order is placed, so Hail cannot sell it through the API
([didww.md](../public/self-host/didww.md#numbers-hail-cannot-sell)).
`POST /numbers/quotes` still returns it as an offer with `by_request: true`,
no `e164` and a `quote_id`; `POST /numbers` refuses that offer.

1. Reply with the price from the catalog row (`usd_per_month`, `setup_usd`)
   and ask for the organization id (console → Settings). `usd_per_month` is
   the cheapest area; when `notes` says the price varies, quote the area the
   customer wants.
2. If the row has `verification_required: true`, the customer first files
   their papers. In the console: **Verify** on the by-request offer. Through
   the API: get the offer's `quote_id` from `POST /v1/numbers/quotes` (the
   offer with `by_request: true`), then `GET /v1/verifications/requirements`
   and `POST /v1/verifications` with that `quote_id`
   ([OpenAPI](../../openapi/openapi.yaml)). Hail files it with DIDWW. Wait for
   the Hail verification to be `approved`.
3. Order in the DIDWW panel: **my.didww.com → Buy Numbers → \<country\> →
   \<type\>**, pick the area, quantity 1, metered plan (0 channels). Link the
   customer's approved address when DIDWW asks. Note the DID id and the
   number DIDWW assigned.
4. Free the number if a connected-number claim that is not checked yet
   holds it (exits 1 when none does; go on either way):
   `docker compose exec api python -m hailhq.api.connected_numbers_ops release-claim --number +<number>`
   ([connected-numbers.md](connected-numbers.md#5-limits)).
5. Add the number to the organization and charge setup + first month, in
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

6. Calls reach the number once it routes to an agent: Hail then assigns
   the DID to the voice IN trunk and to the pay-per-minute capacity group
   ([didww.md](../public/self-host/didww.md#inbound-calls)). Until then the
   DIDWW panel shows it as not configured (Trunk "Voice: none", capacity 0).
   Unrouting clears the trunk only; the number stays in the capacity group,
   which costs nothing without calls.
7. Tell the customer the number. It shows in **Console → Numbers**. The
   monthly fee renews like any other number; releasing
   it (`DELETE /v1/numbers/{id}`) terminates the DID.
8. Run the first call test when this is the first number of its carrier,
   country or type ([first-number-call-test.md](first-number-call-test.md)).

## Routing a number by hand

The customer normally routes the number to an agent: **Console → Numbers**,
or `PATCH /v1/numbers/{id}` with `voice_agent_id`. That call registers the
number for inbound calls: the carrier points it at LiveKit, and LiveKit's
inbound trunk for that carrier accepts it
([`inbound_routing.register`](../../core/hailhq/core/inbound_routing.py)).
When you cannot call the API as the customer, run the same step inside the
api container on the server, from the repo checkout there. Fill the three
values at the top.

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml \
  exec -T api python - <<'PY'
import asyncio
from datetime import datetime, timezone
from uuid import UUID

from hailhq.core import inbound_routing
from hailhq.core.db import org_lock, session_scope
from hailhq.core.livekit import LiveKitClient
from hailhq.core.models import Agent, PhoneNumber
from sqlalchemy import select

ORG = UUID("<org id>")
E164 = "+<number>"
AGENT = UUID("<agent id>")


async def main():
    lk = LiveKitClient()
    try:
        async with session_scope() as db:
            await org_lock(db, ORG)
            number = (
                await db.execute(
                    select(PhoneNumber).where(
                        PhoneNumber.organization_id == ORG,
                        PhoneNumber.e164 == E164,
                        PhoneNumber.provisioning_state == "active",
                    )
                )
            ).scalar_one()
            agent = await db.get(Agent, AGENT)
            if not agent or agent.organization_id != ORG:
                raise SystemExit("agent not found in this organization")
            if agent.status != "live" or not agent.voice_enabled:
                raise SystemExit("agent is not live or does not answer calls")
            if "voice" not in number.capabilities:
                raise SystemExit("number has no voice capability")
            await inbound_routing.register(db, lk, number)
            number.voice_agent_id = AGENT
            number.updated_at = datetime.now(timezone.utc)
            await db.commit()
            print(E164, "registered at", number.inbound_registered_at)
    finally:
        await lk.aclose()


asyncio.run(main())
PY
```

- `register` only flushes, and `session_scope()` never commits: it closes
  the session, which rolls back
  ([`db.py`](../../core/hailhq/core/db.py)). Without `await db.commit()`,
  the carrier and LiveKit are set up but `inbound_registered_at` and
  `voice_agent_id` are not saved.
- `register` does not set `voice_agent_id`; the API route sets it after.
  A registered number with no agent refuses calls (`end_reason` `no_agent`).
- The snippet makes the API route's checks (same organization, voice
  capability, an agent that answers calls) and also needs a `live` agent,
  as inbound calls do. It writes no `audit_log` row.
- `GET /v1/numbers/{id}` then shows `inbound_registered: true`.
