"""Unit tests for ``TelnyxVerificationProvider``.

Telnyx is mocked at the HTTP boundary (``httpx.MockTransport``); the
requirement data is Telnyx's real ``/regulatory_requirements`` answer for
Sweden saved under ``fixtures/``.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

import httpx
import pytest
from hailhq.core.providers.verification import (
    Address,
    DocumentInput,
    UploadedFile,
    VerificationProviderError,
    get_verification_provider,
)
from hailhq.core.providers.verification.telnyx import TelnyxVerificationProvider

ORG = "11111111-2222-3333-4444-555555555555"
GROUP = "8a3c1e4e-0000-4000-8000-000000000001"
FIXTURES = Path(__file__).parent / "fixtures"


def _rules(name: str) -> dict:
    return json.loads((FIXTURES / f"telnyx-regulatory-SE-{name}.json").read_text())


def _ids(name: str, field_type: str) -> list[str]:
    return [
        r["id"]
        for r in _rules(name)["data"][0]["regulatory_requirements"]
        if r["field_type"] == field_type
    ]


def _group(name: str, status: str = "unapproved", values: dict | None = None) -> dict:
    values = values or {}
    return {
        "id": GROUP,
        "country_code": "SE",
        "phone_number_type": name,
        "action": "ordering",
        "status": status,
        "customer_reference": f"hail-{ORG}",
        "regulatory_requirements": [
            {
                "requirement_id": r["id"],
                "field_type": r["field_type"],
                "field_value": values.get(r["id"], ""),
                "status": status,
            }
            for r in _rules(name)["data"][0]["regulatory_requirements"]
        ],
    }


class Telnyx:
    """A scripted Telnyx: records every request, answers from ``answers``."""

    def __init__(self, answers):
        self.answers = answers
        self.calls: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        return self.answers(request)

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self))

    def bodies(self, method: str, path: str) -> list[dict]:
        return [
            json.loads(c.content)
            for c in self.calls
            if c.method == method and c.url.path.endswith(path)
        ]


def provider(fake: Telnyx) -> TelnyxVerificationProvider:
    return TelnyxVerificationProvider(api_key="key", client=fake.client())


def rules_answer(name: str):
    def answers(request):
        assert request.url.path.endswith("/regulatory_requirements")
        return httpx.Response(200, json=_rules(name))

    return answers


async def test_requirements_sweden_mobile() -> None:
    fake = Telnyx(rules_answer("mobile"))
    req = await provider(fake).requirements("SE", "mobile", "person")
    assert req.provider == "telnyx" and req.required is True
    assert req.subject_types == ("person", "business")
    # The field and slot names are Telnyx's requirement ids, so the draft can
    # send the values back under requirement_id without a second lookup.
    assert [f.name for f in req.fields] == _ids("mobile", "textual")
    assert [s.name for s in req.documents] == _ids("mobile", "document")
    assert req.address_required is True
    contact = req.fields[0]
    assert contact.label.startswith("Contact Information")
    assert contact.kind == "text" and contact.required
    assert "Example:" in contact.help
    for slot in req.documents:
        assert slot.needs_input is True
        assert [o.key for o in slot.options] == ["file"]
        assert slot.options[0].file_required is True
    params = fake.calls[0].url.params
    assert params["filter[country_code]"] == "SE"
    assert params["filter[phone_number_type]"] == "mobile"
    assert params["filter[action]"] == "ordering"


async def test_requirements_sweden_local_email_field() -> None:
    fake = Telnyx(rules_answer("local"))
    req = await provider(fake).requirements("SE", "local", "business")
    email = next(f for f in req.fields if "Email" in f.label)
    assert email.kind == "email"
    assert len(req.fields) == 4 and len(req.documents) == 3
    assert req.address_required is True


async def test_requirements_none_needed_when_telnyx_answers_empty() -> None:
    fake = Telnyx(lambda request: httpx.Response(200, json={"data": []}))
    req = await provider(fake).requirements("US", "local", "person")
    assert req.required is False and req.fields == () and req.documents == ()


async def test_requirements_carrier_outage_is_a_provider_error() -> None:
    fake = Telnyx(lambda request: httpx.Response(503, json={"errors": []}))
    with pytest.raises(VerificationProviderError):
        await provider(fake).requirements("SE", "mobile", "person")


def _inputs(name: str):
    textual, documents = _ids(name, "textual"), _ids(name, "document")
    fields = {tid: f"value for {tid[:8]}" for tid in textual}
    docs = {
        did: DocumentInput(
            option="file",
            file=UploadedFile(
                filename="my passport.pdf",
                content_type="application/pdf",
                data=b"%PDF-1.4 test",
            ),
        )
        for did in documents
    }
    address = Address(
        customer_name="Anna Svensson",
        street="Storgatan 1",
        city="Stockholm",
        region="Stockholm",
        postal_code="11122",
        country_code="SE",
    )
    return fields, address, docs


async def test_create_draft_reports_missing_input_without_calling_the_carrier() -> None:
    fake = Telnyx(rules_answer("mobile"))
    p = provider(fake)
    req = await p.requirements("SE", "mobile", "person")
    result = await p.create_draft(
        organization_id=ORG,
        contact_email="ops@hail.test",
        requirements=req,
        fields={},
        address=None,
        documents={},
    )
    fields = {pr.field for pr in result.problems}
    assert set(_ids("mobile", "textual")) <= fields
    assert set(_ids("mobile", "document")) <= fields
    assert "address" in fields
    assert result.refs == {}
    assert len(fake.calls) == 1  # only the requirements lookup


async def test_create_draft_happy_path() -> None:
    fields, address, docs = _inputs("mobile")
    uploads = iter(["doc-1", "doc-2"])
    stored: dict[str, str] = {}

    def answers(request):
        path = request.url.path
        if path.endswith("/regulatory_requirements"):
            return httpx.Response(200, json=_rules("mobile"))
        if request.method == "POST" and path.endswith("/requirement_groups"):
            # The create answer carries no requirement list; the draft must
            # read the group back before it fills anything in.
            return httpx.Response(200, json={"data": {"id": GROUP}})
        if request.method == "POST" and path.endswith("/addresses"):
            return httpx.Response(200, json={"data": {"id": "addr-1"}})
        if request.method == "POST" and path.endswith("/documents"):
            return httpx.Response(200, json={"data": {"id": next(uploads)}})
        if request.method == "PATCH":
            for v in json.loads(request.content)["regulatory_requirements"]:
                stored[v["requirement_id"]] = v["field_value"]
            return httpx.Response(200, json={"data": _group("mobile", values=stored)})
        if request.method == "GET" and GROUP in path:
            return httpx.Response(200, json={"data": _group("mobile", values=stored)})
        raise AssertionError(f"unexpected {request.method} {path}")

    fake = Telnyx(answers)
    p = provider(fake)
    req = await p.requirements("SE", "mobile", "person")
    result = await p.create_draft(
        organization_id=ORG,
        contact_email="ops@hail.test",
        requirements=req,
        fields=fields,
        address=address,
        documents=docs,
    )
    assert result.problems == []
    assert result.refs == {
        "group_id": GROUP,
        "address_id": "addr-1",
        "document_ids": ["doc-1", "doc-2"],
    }

    (group,) = fake.bodies("POST", "/requirement_groups")
    assert group == {
        "country_code": "SE",
        "phone_number_type": "mobile",
        "action": "ordering",
        "customer_reference": f"hail-{ORG}",
    }
    (addr,) = fake.bodies("POST", "/addresses")
    assert addr["first_name"] == "Anna" and addr["last_name"] == "Svensson"
    assert addr["country_code"] == "SE" and addr["customer_reference"] == f"hail-{ORG}"
    assert "business_name" not in addr
    uploaded = fake.bodies("POST", "/documents")
    # A fixed file name: the customer's own file name never reaches Telnyx.
    assert {u["filename"] for u in uploaded} == {"document.pdf"}
    assert base64.b64decode(uploaded[0]["file"]) == b"%PDF-1.4 test"
    (patch,) = fake.bodies("PATCH", GROUP)
    by_id = {
        v["requirement_id"]: v["field_value"] for v in patch["regulatory_requirements"]
    }
    for tid in _ids("mobile", "textual"):
        assert by_id[tid] == fields[tid]
    (address_id,) = _ids("mobile", "address")
    assert by_id[address_id] == "addr-1"
    assert sorted(by_id[d] for d in _ids("mobile", "document")) == ["doc-1", "doc-2"]


async def test_create_draft_business_address_uses_business_name() -> None:
    fields, address, docs = _inputs("local")

    def answers(request):
        path = request.url.path
        if path.endswith("/regulatory_requirements"):
            return httpx.Response(200, json=_rules("local"))
        if request.method == "POST" and path.endswith("/requirement_groups"):
            return httpx.Response(200, json={"data": {"id": GROUP}})
        if (
            request.method == "GET"
            and GROUP in path
            and not fake.bodies("PATCH", GROUP)
        ):
            return httpx.Response(200, json={"data": _group("local")})
        if request.method == "POST" and path.endswith("/addresses"):
            return httpx.Response(200, json={"data": {"id": "addr-b"}})
        if request.method == "POST" and path.endswith("/documents"):
            return httpx.Response(200, json={"data": {"id": "doc"}})
        if request.method == "PATCH":
            return httpx.Response(200, json={"data": {}})
        values = {
            r["id"]: "x" for r in _rules("local")["data"][0]["regulatory_requirements"]
        }
        return httpx.Response(200, json={"data": _group("local", values=values)})

    fake = Telnyx(answers)
    p = provider(fake)
    req = await p.requirements("SE", "local", "business")
    result = await p.create_draft(
        organization_id=ORG,
        contact_email="ops@hail.test",
        requirements=req,
        fields=fields,
        address=address.model_copy(update={"customer_name": "Svensson AB"}),
        documents=docs,
    )
    assert result.problems == []
    (addr,) = fake.bodies("POST", "/addresses")
    assert addr["business_name"] == "Svensson AB" and "first_name" not in addr


async def test_create_draft_carrier_400_becomes_a_problem_and_deletes_the_draft() -> (
    None
):
    fields, address, docs = _inputs("mobile")

    def answers(request):
        path = request.url.path
        if path.endswith("/regulatory_requirements"):
            return httpx.Response(200, json=_rules("mobile"))
        if request.method == "POST" and path.endswith("/requirement_groups"):
            return httpx.Response(200, json={"data": {"id": GROUP}})
        if request.method == "GET" and GROUP in path:
            return httpx.Response(200, json={"data": _group("mobile")})
        if request.method == "POST" and path.endswith("/addresses"):
            return httpx.Response(200, json={"data": {"id": "addr-1"}})
        if request.method == "POST" and path.endswith("/documents"):
            return httpx.Response(
                400,
                json={"errors": [{"title": "Bad Request", "detail": "File too large"}]},
            )
        if request.method == "DELETE":
            return httpx.Response(204)
        raise AssertionError(f"unexpected {request.method} {path}")

    fake = Telnyx(answers)
    p = provider(fake)
    req = await p.requirements("SE", "mobile", "person")
    result = await p.create_draft(
        organization_id=ORG,
        contact_email="ops@hail.test",
        requirements=req,
        fields=fields,
        address=address,
        documents=docs,
    )
    assert result.refs == {}
    assert [pr.message for pr in result.problems] == ["File too large"]
    # Telnyx lists the first document before the address, so the failed upload
    # happens before any address exists: only the group is deleted.
    deleted = [c.url.path for c in fake.calls if c.method == "DELETE"]
    assert deleted == [f"/v2/requirement_groups/{GROUP}"]


async def test_create_draft_carrier_outage_raises_and_deletes_the_draft() -> None:
    fields, address, docs = _inputs("mobile")

    def answers(request):
        path = request.url.path
        if path.endswith("/regulatory_requirements"):
            return httpx.Response(200, json=_rules("mobile"))
        if request.method == "POST" and path.endswith("/requirement_groups"):
            return httpx.Response(200, json={"data": {"id": GROUP}})
        if request.method == "GET" and GROUP in path:
            return httpx.Response(200, json={"data": _group("mobile")})
        if request.method == "DELETE":
            return httpx.Response(204)
        return httpx.Response(502, text="bad gateway")

    fake = Telnyx(answers)
    p = provider(fake)
    req = await p.requirements("SE", "mobile", "person")
    with pytest.raises(VerificationProviderError):
        await p.create_draft(
            organization_id=ORG,
            contact_email="ops@hail.test",
            requirements=req,
            fields=fields,
            address=address,
            documents=docs,
        )
    assert [c.url.path for c in fake.calls if c.method == "DELETE"] == [
        f"/v2/requirement_groups/{GROUP}"
    ]


async def test_create_draft_timeout_is_a_provider_error() -> None:
    fields, address, docs = _inputs("mobile")

    def answers(request):
        path = request.url.path
        if path.endswith("/regulatory_requirements"):
            return httpx.Response(200, json=_rules("mobile"))
        if request.method == "POST" and path.endswith("/requirement_groups"):
            return httpx.Response(200, json={"data": {"id": GROUP}})
        if request.method == "GET" and GROUP in path:
            return httpx.Response(200, json={"data": _group("mobile")})
        if request.method == "DELETE":
            return httpx.Response(204)
        raise httpx.ReadTimeout("slow upload", request=request)

    fake = Telnyx(answers)
    p = provider(fake)
    req = await p.requirements("SE", "mobile", "person")
    with pytest.raises(VerificationProviderError):
        await p.create_draft(
            organization_id=ORG,
            contact_email="ops@hail.test",
            requirements=req,
            fields=fields,
            address=address,
            documents=docs,
        )
    assert [c.url.path for c in fake.calls if c.method == "DELETE"] == [
        f"/v2/requirement_groups/{GROUP}"
    ]
    upload = next(c for c in fake.calls if c.url.path.endswith("/documents"))
    assert upload.extensions["timeout"]["read"] == 60


async def test_check_reports_missing_and_declined_values() -> None:
    textual = _ids("mobile", "textual")
    group = _group("mobile", values={textual[0]: "ok"})
    for req in group["regulatory_requirements"]:
        if req["requirement_id"] == textual[0]:
            req["status"] = "declined"
    fake = Telnyx(lambda request: httpx.Response(200, json={"data": group}))
    problems = await provider(fake).check({"group_id": GROUP})
    assert {p.field for p in problems} == {
        r["requirement_id"] for r in group["regulatory_requirements"]
    }
    assert any("declined" in p.message for p in problems)


async def test_submit_posts_for_approval() -> None:
    fake = Telnyx(lambda request: httpx.Response(200, json={"data": _group("mobile")}))
    await provider(fake).submit({"group_id": GROUP})
    (call,) = fake.calls
    assert call.method == "POST"
    assert call.url.path == f"/v2/requirement_groups/{GROUP}/submit_for_approval"


@pytest.mark.parametrize(
    ("telnyx_status", "state"),
    [
        ("unapproved", "draft"),
        ("pending-approval", "pending"),
        ("approved", "approved"),
        ("declined", "rejected"),
        ("expired", "rejected"),
    ],
)
async def test_status_mapping(telnyx_status, state) -> None:
    fake = Telnyx(
        lambda request: httpx.Response(
            200, json={"data": _group("mobile", status=telnyx_status)}
        )
    )
    status = await provider(fake).status({"group_id": GROUP})
    assert status.state == state
    assert (status.reason is not None) == (state == "rejected")


async def test_purchase_handle_is_the_group_id() -> None:
    fake = Telnyx(lambda request: httpx.Response(200, json={"data": {}}))
    assert await provider(fake).purchase_handle({"group_id": GROUP}) == {
        "requirement_group_id": GROUP
    }


async def test_discard_never_raises() -> None:
    fake = Telnyx(lambda request: httpx.Response(500, text="boom"))
    await provider(fake).discard(
        {"group_id": GROUP, "document_ids": ["doc-1"], "address_id": "addr-1"}
    )
    assert [c.method for c in fake.calls] == ["DELETE"] * 3


def test_registry_returns_plugin_when_configured(monkeypatch) -> None:
    from hailhq.core.config import settings
    from hailhq.core.providers import verification as registry

    monkeypatch.setattr(registry, "_INSTANCES", {})
    monkeypatch.setattr(settings, "telnyx_api_key", "key")
    first = get_verification_provider("telnyx")
    assert isinstance(first, TelnyxVerificationProvider)
    assert get_verification_provider("telnyx") is first


def test_registry_none_without_api_key(monkeypatch) -> None:
    from hailhq.core.config import settings
    from hailhq.core.providers import verification as registry

    monkeypatch.setattr(registry, "_INSTANCES", {})
    monkeypatch.setattr(settings, "telnyx_api_key", "")
    assert get_verification_provider("telnyx") is None


def test_default_provider_stays_twilio_when_both_are_configured(monkeypatch) -> None:
    from hailhq.core.config import settings
    from hailhq.core.providers import verification as registry

    monkeypatch.setattr(registry, "_INSTANCES", {})
    monkeypatch.setattr(settings, "twilio_account_sid", "ACtest")
    monkeypatch.setattr(settings, "twilio_auth_token", "tok")
    monkeypatch.setattr(settings, "telnyx_api_key", "key")
    assert registry.default_verification_provider_name() == "twilio"
