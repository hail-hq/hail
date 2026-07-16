# Agent outbound — runbook

Agent-origin orgs (organizations.origin = 'agent') self-signup via
POST hail.so/api/agent/signup and send on free credit, gated by velocity
caps (core/hailhq/core/agent_caps.py — the enforcement gate; defaults live
in core/hailhq/core/config.py). Spec: hail-website
docs/superpowers/specs/2026-07-14-agent-self-signup-design.md.

## Kill switch (all agent outbound, instantly; humans unaffected)

ON:
INSERT INTO platform_flags (key, value) VALUES ('agent_outbound_disabled', 'true')
ON CONFLICT (key) DO UPDATE SET value = 'true', updated_at = now();
OFF:
UPDATE platform_flags SET value = 'false', updated_at = now()
WHERE key = 'agent_outbound_disabled';

## One abusive org (targeted, not the kill switch)

Use the existing per-org channel suspension. `reason` is NOT NULL — always
record why (ticket/link):
INSERT INTO channel_suspensions (organization_id, channel, reason)
VALUES ('<org>', '<email|sms|voice>', '<why — ticket/link>');

To lift the suspension once resolved:
DELETE FROM channel_suspensions
WHERE organization_id = '<org>' AND channel = '<email|sms|voice>';

## Cap tuning

Env vars override the defaults in core/hailhq/core/config.py
(AGENT_EMAIL_PER_HOUR, AGENT_SMS_PER_HOUR, AGENT_VOICE_PER_HOUR,
AGENT_EMAIL_PER_DAY, AGENT_SMS_PER_DAY, AGENT_VOICE_PER_DAY,
AGENT_EMAIL_RECIPIENTS_PER_DAY, AGENT_SMS_RECIPIENTS_PER_DAY,
AGENT_VOICE_RECIPIENTS_PER_DAY, AGENT_GLOBAL_EMAIL_PER_HOUR,
AGENT_GLOBAL_SMS_PER_HOUR, AGENT_GLOBAL_VOICE_PER_HOUR — pydantic-settings naming,
no prefix). Restart the API to pick up changes.

## Monitoring queries

-- volume by channel, last 24h
SELECT channel, count(\*) FROM agent_send_log
WHERE created_at > now() - interval '24 hours' GROUP BY channel;

-- top agent orgs by sends, last 24h
SELECT organization_id, count(\*) FROM agent_send_log
WHERE created_at > now() - interval '24 hours'
GROUP BY organization_id ORDER BY 2 DESC LIMIT 20;

-- signup funnel by source
SELECT ref, count(\*) FROM agent_signups
WHERE created_at > now() - interval '7 days' GROUP BY ref;

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

## Accepted risk (from the spec)

Agent traffic shares sender domains with paying customers — velocity caps +
this kill switch are the mitigation. If deliverability dips
(bounce/complaint rates on shared domains), flip the kill switch first,
investigate second. Reputation isolation (dedicated subdomain/IP pool) is
the designated fast-follow.

---

## Launch checklist (operator — after deploy)

**SEQUENCING WARNING:** Deploy order is critical. Each step must complete before the next:

1. hail-website DB migration applies first (Global Constraint)
2. hail-website deploy
3. hail API deploy (alembic 0034 auto-applies per the repo's deploy flow — verify in GHA log)
4. Only THEN publish SKILL.md to ClawHub and Moltbook — the doc promises 429 caps and Retry-After headers that don't exist until the API deploys

- [ ] **Step 1: Verify API deployment**
  - Check GHA logs for successful hail-api deploy
  - Confirm alembic 0034 migration ran (search logs for "agent_send_log" or "agent_signups")

- [ ] **Step 2: Publish to ClawHub**

  ```bash
  mkdir -p /tmp/hail-skill
  curl -s "https://hail.so/skill.md?ref=clawhub" -o /tmp/hail-skill/SKILL.md
  clawhub skill publish /tmp/hail-skill
  ```

  Expected: listing visible on clawhub. If `clawhub` CLI needs auth/setup, follow its login flow first (operator account).

- [ ] **Step 3: Moltbook presence**
  1. Register a Hail-owned agent on moltbook.com per their flow (agent signs up, provides claim link, owner verifies via X).
  2. First post: the skill doc — link `https://hail.so/skill.md?ref=moltbook`, framed as "you can sign up yourself, here's how."
  3. Ongoing cadence via the `~/playground/hail-content` pipeline (manual posting first, per current content-engine practice): product notes and replies to comms-related threads, each carrying the `?ref=moltbook` link.

- [ ] **Step 4: Verify the funnel end-to-end in prod**
      After all above steps complete: run the Task 4 Step 3 curl against `https://hail.so` with a real owner email you control; confirm 201 + key; send one email via the key; confirm the send succeeds, `agent_send_log` has the row, and the attribution query in this runbook shows the `ref`. Then delete/close the test workspace.
