# 3CX V20 field notes

Part of the [connected numbers runbook](connected-numbers.md).

Menu names are from 3CX V20 Update 10. Lines marked "(forum)" come from 3CX forum posts or release notes, not 3CX's manual.

**Before you start**:

- The 3CX edition must allow a generic SIP trunk: PRO or Enterprise, on-premise or self-hosted. Hosted SMB and free editions offer only "3CX Tested" providers (forum).
- Firewall: outgoing SIP on 5060 to the SIP server, RTP (UDP) both ways, SIP ALG off on the router. A blocked INVITE never reaches Hail; 3CX may log only a timeout (408 or 503), or nothing (seen on one install).
- Capacity: each agent call holds 2 of 3CX's simultaneous calls (the forwarded inbound leg stays open). 8 simultaneous calls carry about 4 agent calls. The runbook's 6-call test needs 12.

**Trunk** (Admin > Voice & Chat > Add Trunk > the template "Generic SIP Trunk (IP Based)"). Whatever the template's name, enter the connection's username and password: Hail refuses a call without them.

- Use the generic template, never a carrier's (Twilio or a local carrier): those register with that carrier.
- Registrar/Server: the SIP server's hostname from the connection, port 5060, Outbound Proxy empty. Enter the hostname only, never its IP addresses: they change, and Hail matches calls by username and password, not by address.
- Authentication ID and password: the connection's username and password. Seen working on one install with type of authentication `Do not require – IP based` (3CX still answered LiveKit's digest challenge); not checked in 3CX's docs. If calls never reach Hail, try a type that authenticates.
- Main Trunk Number is required. 3CX sends it as the caller on every call through the trunk (see caller ID below).
- DTMF: RFC 2833.
- DIDs on the trunk: none. The business's number already belongs to its carrier trunk, and the same DID on 2 trunks confuses inbound routing.
- Default route: End Call. Calls never come in on this trunk.

**Outbound rule** (Admin > Outbound Rules, move it to the top):

- Do not match the number itself. 3CX's E.164 setting ("remove the country code if same country") rewrites `+<cc>…` to the national form before rules run, so a `+<cc>` prefix never matches, and each number would need its own rule.
- Match a dial-prefix tag no other rule uses (for example `7799`). Route 1: the Hail trunk, strip the tag's digits, prepend `+<country code>`. One rule serves every number.
- Countries that drop the national trunk prefix in E.164 (the leading `0` in GB, DE and most others): strip the tag and that `0` too (5 digits for a 4-digit tag). Countries that keep it (IT): strip only the tag.
- "Calls from extension(s)", "Calls to numbers with a length of" and "Outbound Caller ID" stay empty. "Length of" means a count of digits, not an extension: a value makes the rule never match. An Outbound Caller ID replaces the real caller.
- Never a broad prefix (`+`, `0-9`, "Any"): every outgoing call of the business would go to Hail.
- The first matching rule wins. If the Hail rule sits below a general rule, the general rule wins: the call leaves on the carrier trunk and the carrier answers `403`. Keep the Hail rule on top.

**Sending calls to the trunk**: forward to outside number `<tag><national number>`. Example in GB: the national `020 7946 0018` is forwarded to `779902079460018`; the rule strips 5 digits and prepends `+44`, so Hail receives `+442079460018`, the number on the connection.

- Testing: Admin > Advanced > CID Rules, one rule per caller (a staff mobile), on the carrier trunk that receives the number (never the Hail trunk), forward to `<tag><national number>`. Other callers keep reaching the business.
- A Digital Receptionist (menu) with no greeting waits silently for a key press: keep it out of the path, or forward from it with a 1-second timeout.
- A user (extension) can forward too: Call Forwarding > Available, "Busy" and "Not registered and unanswered" > External number `<tag><national number>`. Left on Voicemail, a user with no phone sends every call to voicemail.
- Test from an extension with no DID of its own. Since Update 10 an extension's assigned DID can skip outbound rules and use its own trunk (forum).
- The ownership check calls from a Hail pool line, often a different number on each check. Read the check call's number in 3CX's call history and match its start in the form 3CX shows it (with `+`, with `00`, or with neither). If no rule matches, 3CX answers the check itself (reception or menu) and the check ends `no_call_arrived`.
- Go-live: the number's inbound rule (office hours, out of office, or one menu option) forwards to `<tag><national number>`. Remove the test CID rules.

**Caller ID**: by default 3CX sends the Main Trunk Number as the caller, so every caller looks like the business's own number. Agents find earlier calls and texts by the caller's number, so callers' histories would mix. Fix it before go-live: V20 has no Outbound Parameters tab, so set `FromUserPart` to `$OriginatorCallerId` in a copy of the trunk's provider template (Advanced > Templates > SIP Trunk Providers) or in an exported trunk file, then import it (forum). A test call from a mobile must show the mobile's number in Hail. Not yet checked in 3CX's own docs: the P-Asserted-Identity and Remote-Party-ID fields.

**3CX Event Log** (Admin > Event Log, errors and warnings only):

- `403` from another trunk's address: the call left on the business's normal line, so the Hail rule did not match (prefix, an empty field not empty, or rule order).
- `401` or `407` from the SIP server: wrong username or password.
- `486 Busy Here` from the SIP server: Hail refused the call (seen for a number with no agent routed). Read the failed call's end reason in Hail: `no_agent`, `insufficient_funds` and the like.
- No line at all and Hail says `no_call_arrived`: the CID or inbound rule did not catch the call. A call routed to the wrong place logs nothing: the Event Log shows failures only.
- Read the IP address in each line to tell which side answered: the carrier's or Hail's SIP server. Admin > Voice & Chat > the trunk > Check SIP Trunk tests the trunk itself.

**Agent voice**: set a voice native to the call's language; with no voice set, the default voice speaks with a foreign accent. Write brand names in the instructions and greeting as words ("Northgate"), not capitals ("NORTHGATE"): text-to-speech spells capitals letter by letter.
