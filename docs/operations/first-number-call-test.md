# First number call test

Place one real call to a customer's number and check that it was answered.
A number can be sold and routed to an agent and still refuse every call:
for example, a carrier account with no inbound call capacity.

```bash
export HAIL_API_KEY='<API key from your own Hail workspace>'
uv run python scripts/ops/place_test_call.py \
  --number +<customer number> --i-will-call-a-customer
```

Run it from a repo checkout. The script is
[`scripts/ops/place_test_call.py`](../../scripts/ops/place_test_call.py).

## When to run it

- The first number Hail sells for a new carrier, a new country, or a new
  number type (local, mobile, national, toll-free).
- After any change to inbound routing for a carrier: the carrier's SIP or
  trunk settings, its call capacity, the LiveKit inbound trunk, or the code
  in [`inbound_routing.py`](../../core/hailhq/core/inbound_routing.py).

## Before

- The number has a voice agent: `GET /v1/numbers/{id}` as the customer shows
  `voice_agent_id` set and `inbound_registered: true`. If not, see
  [Routing a number by hand](didww-by-request.md#routing-a-number-by-hand).
- The call reaches the customer's agent and shows in their call log. Tell
  the customer before you call when you can.
- Your own workspace pays for the outbound call. It needs credit.

## Run

1. Set `HAIL_API_KEY` in your shell to a key from your own workspace
   (**Console → API keys**). Never commit it. `HAIL_API_URL` changes the API
   address (default `https://api.hail.so`).
2. Run the command above. The script refuses to run without `--number` and
   `--i-will-call-a-customer`.
3. It places the call (`POST /v1/calls`), polls `GET /v1/calls/{id}` until
   the call ends, and prints one line:
   - `ANSWERED: the number took the call, ended after 42 s. status=completed end_reason=normal_hangup`
   - `NOT ANSWERED: status=failed end_reason=sip_trunk_failure. <message>`

   Exit code: 0 answered, 1 not answered, 2 the call could not be placed or
   did not end within 7 minutes (`--timeout` changes it).

## Read the result

Your outbound call only shows what your side saw. Check the customer's side
too: it is the row you delete afterwards.

```bash
psql "$DATABASE_URL" <<'SQL'
\set org '<org id>'
\set e164 '+<customer number>'
SELECT id, status, end_reason, answered_at, ended_at
FROM calls
WHERE organization_id = :'org' AND direction = 'inbound' AND to_e164 = :'e164'
ORDER BY requested_at DESC LIMIT 3;
SQL
```

- An inbound row that was answered: the number works.
- An inbound row with `end_reason` `no_agent` or `insufficient_funds`: Hail
  got the call and refused it. Fix the agent or the balance.
- No inbound row at all: the call never reached Hail. The carrier or the
  LiveKit inbound trunk refused it. Check the carrier panel (call capacity,
  where the number points) and the number's `inbound_registered`.

## After

Remove the test call from the customer's workspace:
[call-record-deletion.md](call-record-deletion.md), with the inbound call
`id` from the query above. Your own outbound call stays in your workspace;
delete it the same way if you want.
