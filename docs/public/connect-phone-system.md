# Connect your phone system

Keep your number, your phone provider and your phone system (PBX). Your
phone system sends the calls you choose to a Hail agent over a SIP trunk:
all calls, after hours, overflow, or one menu option.

Hail's team sets up the connection with you: email hi@hail.so. The steps
below show what the setup does.

```bash
# 1. Create a connection. The password is shown once.
curl -X POST "$HAIL_API_URL/v1/sip-connections" -H "Authorization: Bearer $HAIL_API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"label": "Main office", "default_region": "US", "pbx_host": "pbx.example.com"}'
# → {"id": "c1...", "sip_host": "<project>.sip.livekit.cloud", "username": "hail-...", "password": "..."}

# 2. Add your number. It starts "unverified".
curl -X POST "$HAIL_API_URL/v1/sip-connections/c1.../numbers" -H "Authorization: Bearer $HAIL_API_KEY" \
  -H 'Content-Type: application/json' -d '{"e164": "+14155550100"}'
# → {"id": "n1...", "e164": "+14155550100", "provisioning_state": "unverified", "source": "connected"}

# 3. Send that number's calls to Hail (below), then run the ownership check.
curl -X POST "$HAIL_API_URL/v1/sip-connections/c1.../numbers/n1.../verify" \
  -H "Authorization: Bearer $HAIL_API_KEY"
# → {"state": "active"}  or  {"state": "unverified", "reason": "no_call_arrived"}

# 4. Route it to an agent, as any number.
curl -X PATCH "$HAIL_API_URL/v1/numbers/n1..." -H "Authorization: Bearer $HAIL_API_KEY" \
  -H 'Content-Type: application/json' -d '{"voice_agent_id": "a1b2..."}'
```

CLI: `hail sip-connections create --label "Main office" --region US --pbx-host pbx.example.com`,
then `add-number`, `verify`, and `hail numbers route <number-id> --calls <agent-id>` ([CLI](cli.md#connect-your-phone-system)).
Console: **Numbers → Connect your phone system**.
Schemas: `SipConnectionCreate`, `SipConnectionCreated`, `ConnectedNumberCheck` in
[`openapi/openapi.yaml`](../../openapi/openapi.yaml).

## Set up the trunk on your phone system

Add a generic SIP trunk with:

- **SIP server**: `sip_host` from step 1.
- **Username and password**: from step 1. Lost the password? `POST /v1/sip-connections/{id}/rotate-password` makes a new one.
- **Audio**: G.711 (PCMU or PCMA).
- **Key presses**: RFC 2833 / RFC 4733 (telephone events). The ownership check needs them.
- **Dialed number**: `+` and the country code, like `+14155550100`, on every call to Hail (an outbound rule that adds them).
- Send calls straight to the trunk, not through a queue or menu that answers first.

A call to Hail uses 2 of your phone system's call channels; a handover uses 1 more.

Hail dials your phone system from shared cloud addresses. Let calls from the
trunk reach only your own extensions, never an outside line.

## The ownership check

Hail calls your number over the phone network. The call must come back to
Hail through your connection and carry a code that Hail sends as key
presses. A call you send yourself cannot pass.

- Billed as a normal outbound call.
- `no_call_arrived`: check the trunk, the routing rule for this number, and the address.
- `not_authenticated`: the call came without this connection's username and password, or not in `+` format.
- `code_not_received`: turn on RFC 2833 key presses.
- Run the check soon after you add a number. Until it passes, someone else can buy or connect the number, and Hail removes numbers left unchecked.

## Calls out and handover

With `pbx_host`, agents call out from your number through your phone system
(`POST /v1/calls` with `from` set to it), and hand calls over to a person on
an extension: save the person as a contact with `extension` (1 to 6 digits,
`*` or `#`), then add it to the agent's `handover_contacts`
([Agents](agents.md#hand-over-to-a-person)). The person sees the caller's own number.

Without `pbx_host`, Hail only answers.

## Good to know

- Price: 10¢ a minute of call, and 10¢ a minute more while a handover is answered. No monthly fee.
- Voice only: a connected number cannot send texts. An agent can still text from a Hail number.
- `DELETE /v1/sip-connections/{id}/numbers/{number_id}` removes the number from Hail. It stays with your phone system and provider.
