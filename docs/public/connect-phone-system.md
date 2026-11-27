# Connect your phone system

Keep your number, your phone provider and your phone system (PBX). Your
phone system sends the calls you choose to a Hail agent over a SIP trunk:
all calls, after hours, overflow, or one menu option.

Hail's team sets up the connection with you: email hi@hail.so. Below is
what your phone system needs and what the setup does.

Using 3CX? Follow the [3CX setup guide](https://hail.so/blog/3cx-ai-receptionist),
step by step with screenshots.

## Set up the trunk on your phone system

Add a generic SIP trunk with:

- **SIP server**: Hail's team sends you the full address. The console and the API show its first characters.
- **Username and password**: from Hail's setup. Lost the password? Hail makes a new one.
- **Audio**: G.711 (PCMU or PCMA).
- **Key presses**: RFC 2833 / RFC 4733 (telephone events). The ownership check needs them.
- **Dialed number**: `+` and the country code, like `+14155550100`, on every call to Hail (an outbound rule that adds them).
- **Caller number**: pass the original caller's number, not your own. Your agent knows returning callers by their number. A call that shows one of your own numbers as the caller reaches your agent as a hidden caller, and the call's `caller_hidden_reason` is `phone_system_sent_own_number`.
- Send calls straight to the trunk, not through a queue or menu that answers first.

A call to Hail uses 2 of your phone system's call channels; a handover uses 1 more.

Hail dials your phone system from shared cloud addresses. Let calls from the
trunk reach only your own extensions, never an outside line.

## The ownership check

Hail calls your number over the phone network. The call must come back to
Hail through your connection and carry a code that Hail sends as key
presses. A call you send yourself cannot pass.

- Billed as a normal outbound call.
- `no_call_arrived`: check the trunk and its username and password, the address, and that a routing rule sends this number to Hail with `+` and the country code. A number without them never reaches Hail.
- `not_authenticated`: the call came in on another connection's trunk, for example with another connection's username and password.
- `code_not_received`: turn on RFC 2833 key presses.
- Run the check soon after you add a number. Until it passes, someone else can buy or connect the number, and Hail removes numbers left unchecked.

## Calls out and handover

With your phone system's address, agents call out from your number through your phone system
(`POST /v1/calls` with `from` set to it), and hand calls over to a person on
an extension: save the person as a contact with `extension` (1 to 6 digits,
`*` or `#`), then add it to the agent's `handover_contacts`
([Agents](agents.md#hand-over-to-a-person)). The person sees the caller's own number.

Without it, Hail only answers.

## Good to know

- Price: 10¢ a minute of call, and 10¢ a minute more while a handover is answered. No monthly fee.
- Voice only: a connected number cannot send texts. An agent can still text from a Hail number.
- Removing a number from Hail leaves it with your phone system and provider.
