# Call leg costs runbook

> **For developers and coding agents:** this page says what the `call_legs` table holds, how each carrier's cost is read, and how to check that costs arrive.

## What this is

Each row of `call_legs` is one phone line a call used, and what the carrier charged for it. A call has one `call` leg (the caller, or the person Hail dialed) plus one `handover` leg per handover dial. Model: `CallLeg` in [`core/hailhq/core/models.py`](../../core/hailhq/core/models.py). Code: [`core/hailhq/core/call_legs.py`](../../core/hailhq/core/call_legs.py).

1. The voicebot notes every SIP participant of the call ([`voicebot/hailhq/voicebot/leg_tracker.py`](../../voicebot/hailhq/voicebot/leg_tracker.py)) and writes the legs when the call ends.
2. The api runs a pass every 5 minutes (`_leg_cost_loop` in [`api/hailhq/api/main.py`](../../api/hailhq/api/main.py)). It asks each carrier for the legs that ended at least 2 minutes ago, at most once every 15 minutes per leg. Carriers are asked at the same time, and Twilio and Telnyx up to 8 calls at once.
3. A leg gets `cost_status = 'found'` with `cost_usd`, or `missing` when it has no cost after 48 hours, for any reason (no record, carrier outage, missing key). Billing (a later step) will charge a fallback rate for it.

Nothing bills from these costs yet. Carrier costs are internal: no customer API returns them.

## Where each carrier's cost comes from

| Carrier | Found by                                         | Source                                                                                                                                                                 |
| ------- | ------------------------------------------------ | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Twilio  | CallSid (LiveKit attribute `sip.twilio.callSid`) | `price` on the Call resource. Twilio fills it in a few minutes after the call.                                                                                         |
| Telnyx  | SIP Call-ID (LiveKit attribute `sip.callIDFull`) | `cost`, `billed_sec` and `rate` in `GET /v2/detail_records?filter[sip_call_id]=…`                                                                                      |
| DIDWW   | SIP Call-ID                                      | One outbound CDR export (`POST /v3/exports`, `export_type: cdr_out`) per pass, matched on its "Call ID" column. Inbound DIDWW legs are not in it and end as `missing`. |

Each lookup is the carrier's `leg_costs` function in `core/hailhq/core/providers/voice/<carrier>.py`, registered in `CARRIERS` ([`carrier_routing.py`](../../core/hailhq/core/carrier_routing.py)). It uses the carrier keys the api already has. No new env key.

Carriers bill in steps: Twilio and Telnyx round US calls up to the minute. `billed_seconds` is the time the carrier billed, not the talk time. Twilio does not report it, so it stays empty there.

## Check that costs arrive

```sql
SELECT provider, cost_status, count(*), sum(cost_usd)
FROM call_legs
WHERE joined_at > now() - interval '1 day'
GROUP BY 1, 2
ORDER BY 1, 2;
```

- Many `pending` rows older than an hour: the pass is failing. Search the api logs for `leg costs:`.
- Many `missing` rows for one carrier: its ids are not reaching the voicebot. Check that the leg rows have `sip_call_id` (Telnyx, DIDWW) or `carrier_call_id` (Twilio).

## Build order

This is step 2 of per-destination call pricing. The steps after it ship in a fixed order: see "Order of work" in [CLAUDE.md](../../CLAUDE.md#order-of-work).
