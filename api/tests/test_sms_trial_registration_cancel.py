"""A workspace's toll-free registration whose number now sends trial texts
cannot be cancelled from the console: the cancel would delete the number's
toll-free verification at the carrier (the account close case is in
test_org_close.py)."""

import pytest
from hailhq.api.main import app
from hailhq.api.routes.verifications import get_verification_registry
from hailhq.core.models import CarrierVerification, PhoneNumber


class _Carrier:
    def __init__(self):
        self.discarded = []

    async def discard(self, refs):
        self.discarded.append(dict(refs))


@pytest.fixture
def carrier():
    fake = _Carrier()
    app.dependency_overrides[get_verification_registry] = lambda: (
        lambda name, purpose="number": fake
    )
    yield fake
    app.dependency_overrides.pop(get_verification_registry, None)


@pytest.mark.parametrize("in_trial_pool", [True, False])
async def test_console_cancel_keeps_a_trial_numbers_registration(
    client, async_session, org_and_key, carrier, in_trial_pool
):
    org, _, plain = org_and_key
    number = PhoneNumber(
        organization_id=None if in_trial_pool else org,
        is_pool=in_trial_pool,
        pool_purpose="sms_trial" if in_trial_pool else None,
        e164="+18005550199",
        country_code="US",
        number_type="toll_free",
        capabilities=["sms"],
        provider="twilio",
        provider_resource_id="PN_trial",
        provisioning_state="active",
    )
    async_session.add(number)
    await async_session.commit()
    row = CarrierVerification(
        organization_id=org,
        number_id=number.id,
        provider="twilio",
        country_code="US",
        number_type="toll_free",
        subject_type="business",
        purpose="sms",
        state="approved",
        provider_refs={
            "registration_kind": "toll_free",
            "tollfree_verification_sid": "HH1",
        },
        requirements_version="test",
    )
    async_session.add(row)
    await async_session.commit()

    resp = await client.delete(
        f"/verifications/{row.id}", headers={"Authorization": f"Bearer {plain}"}
    )

    await async_session.refresh(row)
    if in_trial_pool:
        assert resp.status_code == 409, resp.text
        assert "trial" in resp.json()["detail"]
        assert "twilio" not in resp.text.lower()
        assert row.state == "approved" and carrier.discarded == []
    else:
        assert resp.status_code == 200, resp.text
        assert row.state == "cancelled" and len(carrier.discarded) == 1
