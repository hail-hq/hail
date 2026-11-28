# Connect your phone system

Keep your phone number, your phone provider and your phone system (PBX).
Your phone system sends the calls you choose to your Hail agent: every call,
calls after hours, calls nobody picks up, or one option in your phone menu.

Using 3CX? Follow the [3CX setup guide](https://hail.so/blog/3cx-ai-receptionist),
with screenshots for each step.

## Add a SIP trunk to your phone system

Create a connection under **Phone system** in the console (or
`POST /v1/sip-connections`). Then add a generic SIP trunk to your phone
system with these settings:

- **SIP server**: shown on the connection. If you see only its first characters, contact support for the full address.
- **Username and password**: shown when you create the connection. If you lose the password, make a new one (`POST /v1/sip-connections/{id}/rotate-password`).
- **Audio**: G.711 (PCMU or PCMA).
- **Key presses**: RFC 2833 / RFC 4733. Hail needs them to check your number.
- **Number format**: send the dialed number with `+` and the country code, like `+14155550100`. Most phone systems do this with an outbound rule.
- **Caller number**: send the number of the person calling, not your own number. Your agent knows returning callers by their number. If your phone system sends one of your own numbers instead, your agent treats the caller as unknown, and the call's `caller_hidden_reason` is `phone_system_sent_own_number`.
- **Routing**: send calls straight to the trunk. A queue or a menu that answers first keeps the call from reaching your agent.

Each call to your agent uses 2 call channels on your phone system. A handover
to a person uses 1 more.

Calls from Hail come from shared addresses, so an address allowlist does not
protect the trunk. Let calls on this trunk reach only your own extensions,
never an outside line.

## Check your number

Add the number to the connection, then press **Check** (or
`POST /v1/sip-connections/{id}/numbers/{number_id}/verify`). Hail calls your
number over the phone network. Your phone system sends the call back to Hail
over the trunk, and Hail listens for a code it sends as key presses. This
proves the number is yours.

Check the number soon after you add it. Until it passes, another workspace
can buy or connect the same number, and Hail removes numbers that stay
unchecked.

If the check fails, the result says why:

- `no_call_arrived`: the call never reached Hail. Check the trunk's SIP server, username and password, and that a rule sends this number to Hail with `+` and the country code.
- `not_authenticated`: the call came in on another connection's trunk. Use this connection's username and password.
- `code_not_received`: the key presses did not arrive. Turn on RFC 2833 key presses.

## Calls out and handover

Add your phone system's address (its public hostname or IP address) to the
connection, and your agent can also:

- Call out from your number: `POST /v1/calls` with `from` set to the connected number.
- Hand a call over to a person on an extension: save the person as a contact with an `extension` (1 to 6 digits, `*` or `#`), then add the contact to the agent's `handover_contacts` ([Agents](agents.md#hand-over-to-a-person)). The person sees the caller's number.

Without that address, your agent only answers calls.

## Good to know

- A connected number handles calls only. To send texts, your agent uses a number bought in Hail.
- Removing a number from Hail leaves it as it is in your phone system and at your provider.
