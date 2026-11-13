# Investigate a failed number purchase

Read the order's state and safe carrier references. Replace the UUID with the
affected Hail number ID:

```sql
BEGIN READ ONLY;
SELECT id, organization_id, provisioning_state,
       provisioning_metadata->>'failure_code' AS failure_code,
       provisioning_metadata->>'failure_reason' AS failure_reason,
       provisioning_metadata->'carrier_error' AS carrier_error
FROM phone_numbers
WHERE id = '00000000-0000-0000-0000-000000000000';
ROLLBACK;
```

- `carrier_error` retains the HTTP status, numeric error codes, and a UUID
  request ID when Telnyx supplies them. Raw carrier text is not retained.
- `carrier_account_restricted` means Telnyx rejected the order with code
  `20003` (account/API-key permission). It is an operator issue, not a request
  for the customer's number-verification paperwork.
- Check the account in [Telnyx Mission Control](https://portal.telnyx.com/).
  The owner can follow [Telnyx's account-verification guide](https://support.telnyx.com/en/articles/1130595-account-verification).
  Give carrier support the error code and request ID to identify the exact
  restriction. Do not assume changing an email address alone will resolve it.
- A failed order returns its credit hold once. A timeout, HTTP 408/409, or
  server error can leave an unknown outcome: keep the order pending for
  reconciliation. Never submit another order before resolving that outcome.
- A live, ready quote confirms inventory and number-level readiness. It does
  not guarantee the carrier account has permission to buy.

The implementation is in [number_orders.py](../../api/hailhq/api/number_orders.py).
Telnyx's [number adapter](../../core/hailhq/core/providers/voice/telnyx.py)
separates the US/Canadian destination code from subscriber digits during
search and checks every returned number against the requested prefix.
