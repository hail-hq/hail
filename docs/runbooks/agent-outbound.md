# Agent outbound — runbook

Agent-origin orgs (organizations.origin = 'agent') do self-signup via
POST hail.so/api/agent/signup and send on free credit. Velocity caps gate
them (core/hailhq/core/agent_caps.py is the enforcement gate; the defaults
are in core/hailhq/core/config.py). Refer to the spec: hail-website
docs/superpowers/specs/2026-07-14-agent-self-signup-design.md.

## Kill switch (all agent outbound, instantly; humans unaffected)

ON:
INSERT INTO platform_flags (key, value) VALUES ('agent_outbound_disabled', 'true')
ON CONFLICT (key) DO UPDATE SET value = 'true', updated_at = now();
OFF:
UPDATE platform_flags SET value = 'false', updated_at = now()
WHERE key = 'agent_outbound_disabled';

The switch covers direct sends (POST /emails, /sms, /calls) AND the
inbound-forward relay for agent-origin orgs. Both check the flag before they
call a provider. While the switch is ON, an agent-origin org's queued forwards
defer: they stay queued and resume when you flip the switch OFF. A queued agent
forward at the head of the shared forward queue can delay other orgs' forwards
for a short time until the switch clears. This is expected during a short
emergency stop.

## One abusive org (targeted, not the kill switch)

Use the existing per-org channel suspension. `reason` is NOT NULL — always
record the reason (ticket/link):
INSERT INTO channel_suspensions (organization_id, channel, reason)
VALUES ('<org>', '<email|sms|voice>', '<why — ticket/link>');

To lift the suspension after you resolve the issue:
DELETE FROM channel_suspensions
WHERE organization_id = '<org>' AND channel = '<email|sms|voice>';

## Cap tuning

Env vars override the defaults in core/hailhq/core/config.py
(AGENT_EMAIL_PER_HOUR, AGENT_SMS_PER_HOUR, AGENT_VOICE_PER_HOUR,
AGENT_EMAIL_PER_DAY, AGENT_SMS_PER_DAY, AGENT_VOICE_PER_DAY,
AGENT_EMAIL_RECIPIENTS_PER_DAY, AGENT_SMS_RECIPIENTS_PER_DAY,
AGENT_VOICE_RECIPIENTS_PER_DAY, AGENT_GLOBAL_EMAIL_PER_HOUR,
AGENT_GLOBAL_SMS_PER_HOUR, AGENT_GLOBAL_VOICE_PER_HOUR — pydantic-settings naming,
no prefix). Restart the API to apply the changes.

## Monitoring queries

-- volume by channel, last 24h
SELECT channel, count(\*) FROM agent_send_log
WHERE created_at > now() - interval '24 hours' GROUP BY channel;

-- top agent orgs by sends, last 24h
SELECT organization_id, count(\*) FROM agent_send_log
WHERE created_at > now() - interval '24 hours'
GROUP BY organization_id ORDER BY 2 DESC LIMIT 20;

-- signup funnel by source (completed signups only — rows with a NULL user_id
-- are throttle-accounting entries for rejected/duplicate attempts, not signups)
SELECT ref, count(\*) FROM agent_signups
WHERE user_id IS NOT NULL AND created_at > now() - interval '7 days' GROUP BY ref;

-- signup grants to agent-origin orgs, last 7 days
-- (grant_signup fires for every signup, human and agent alike, with
-- kind='credit'/channel='credit' in both cases — join on organizations.origin
-- to isolate the agent-origin ones; source identifies the grant itself)
SELECT ac.organization_id, sum(ac.amount_cents) AS grant_cents
FROM account_credits ac
JOIN organizations o ON o.id = ac.organization_id
WHERE o.origin = 'agent' AND ac.source = 'grant_signup'
AND ac.created_at > now() - interval '7 days'
GROUP BY ac.organization_id ORDER BY 2 DESC;

## Retention (agent_send_log grows unbounded)

The gate queries only the last hour/day. Thus rows older than approximately
24h have no runtime value. The monitoring queries above cap at 7 days. Prune
periodically to bound the table + autovacuum cost (for example, a daily job):
DELETE FROM agent_send_log WHERE created_at < now() - interval '30 days';

## Accepted risk (from the spec)

Agent traffic shares sender domains with paying customers. Velocity caps +
this kill switch are the mitigation. If deliverability decreases
(bounce/complaint rates on shared domains), flip the kill switch first.
Investigate second. Reputation isolation (dedicated subdomain/IP pool) is
the designated fast-follow.

---

## Launch checklist (operator — after deploy)

**SEQUENCING WARNING:** The deploy order is critical. Complete each step before you start the next:

1. Apply the hail-website DB migration first (Global Constraint)
2. Deploy hail-website
3. Deploy the hail API (alembic 0034 auto-applies per the repo's deploy flow — verify in the GHA log)
4. Only THEN publish SKILL.md to ClawHub and Moltbook. The doc promises 429 caps and Retry-After headers that do not exist until the API deploys

- [ ] **Step 1: Verify API deployment**
  - Check the GHA logs for a successful hail-api deploy
  - Confirm that the alembic 0034 migration ran (search the logs for "agent_send_log" or "agent_signups")

- [ ] **Step 2: Publish to ClawHub**

  ```bash
  mkdir -p /tmp/hail-skill
  curl -s "https://hail.so/skill.md?ref=clawhub" -o /tmp/hail-skill/SKILL.md
  clawhub skill publish /tmp/hail-skill
  ```

  Expected result: the listing is visible on clawhub. If the `clawhub` CLI needs auth/setup, follow its login flow first (operator account).

- [ ] **Step 3: Moltbook presence**
  1. Register a Hail-owned agent on moltbook.com per their flow (the agent signs up and provides a claim link, then the owner verifies via X).
  2. Make the first post the skill doc — link `https://hail.so/skill.md?ref=moltbook`, framed as "you can sign up yourself, here's how."
  3. Keep an ongoing cadence via the `~/playground/hail-content` pipeline (manual posting first, per current content-engine practice). Post product notes and replies to comms-related threads. Each post carries the `?ref=moltbook` link.

- [ ] **Step 4: Verify the funnel end-to-end in prod**
      After all the steps above are complete, run the Task 4 Step 3 curl against `https://hail.so` with a real owner email that you control. Confirm 201 + key. Send one email via the key. Confirm that the send succeeds, that `agent_send_log` has the row, and that the attribution query in this runbook shows the `ref`. Then delete/close the test workspace.
