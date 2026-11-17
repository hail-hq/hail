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

- The number has a live voice agent and is registered for inbound calls.
  `registered` and `agent_ready` must be `t`. Note the `organization_id`
  for the queries below. If either is `f`, see
  [Routing a number by hand](didww-by-request.md#routing-a-number-by-hand).
  No row: the number is not active in any workspace (still pending,
  released, or a typo). Do not call: Hail would drop the call.

  ```bash
  psql "$DATABASE_URL" <<'SQL'
  \set e164 '+<customer number>'
  SELECT n.organization_id, n.inbound_registered_at IS NOT NULL AS registered,
         COALESCE(a.status = 'live' AND a.voice_enabled, false) AS agent_ready
  FROM phone_numbers n LEFT JOIN agents a ON a.id = n.voice_agent_id
  WHERE n.e164 = :'e164' AND n.provisioning_state = 'active';
  SQL
  ```
- The call reaches the customer's agent and shows in their call log. Get
  the customer's OK before you call: the script tells Hail the recipient
  agreed to the call (`recipient_consent: true`).
- Your own workspace pays for the outbound call. It needs credit.

## Run

1. Set `HAIL_API_KEY` in your shell to a key from your own workspace
   (**Console → API keys**). Never commit it. `HAIL_API_URL` changes the API
   address (default `https://api.hail.so`).
2. Run the command above. The script refuses to run without `--number` and
   `--i-will-call-a-customer`.
3. It places the call (`POST /v1/calls`), polls `GET /v1/calls/{id}` until
   the call ends, and prints one line:
   - `ANSWERED: the call was picked up, ended after 42 s. status=completed end_reason=normal_hangup`
     (a call with status `completed` counts as answered)
   - `NOT ANSWERED: status=failed end_reason=sip_trunk_failure. <message>`

   Exit code: 0 answered, 1 not answered, 2 the call could not be placed or
   did not end within 7 minutes (`--timeout` changes it).

## Read the result

Your outbound call only shows what your side saw. A carrier announcement or
voicemail also picks up. Check the customer's side too: it is the row you
delete afterwards. Match it by the caller ID the script printed, so you
never pick a real caller's call.

```bash
psql "$DATABASE_URL" <<'SQL'
\set org '<org id>'
\set e164 '+<customer number>'
\set caller '+<caller ID the script printed>'
SELECT id, status, end_reason, answered_at, ended_at
FROM calls
WHERE organization_id = :'org' AND direction = 'inbound'
  AND to_e164 = :'e164' AND from_e164 = :'caller'
  AND requested_at > now() - interval '1 hour'
ORDER BY requested_at DESC;
SQL
```

- An inbound row that was answered: the number works.
- An inbound row that Hail refused (`status` `failed`). The `end_reason`
  says why ([`inbound_calls.py`](../../core/hailhq/core/inbound_calls.py)):
  - `no_agent`: no live voice agent on the number.
  - `insufficient_funds`: the workspace has no credit.
  - `user_rejected`: voice is suspended for the workspace.
  - `carrier_route_failed`: the call came in on another carrier's LiveKit
    trunk, or the number has no voice capability.
- No inbound row at all: the carrier or the LiveKit inbound trunk refused
  the call, or Hail dropped it because the number is not an active number
  of a workspace. Check the carrier panel (call capacity, where the number
  points) and the Before query above.

## After

Remove the test call from the customer's workspace:
[call-record-deletion.md](call-record-deletion.md), with the inbound call
`id` from the query above. Your own outbound call stays in your workspace;
delete it the same way if you want.
