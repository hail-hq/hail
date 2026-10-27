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
from hailhq.core.providers.telnyx import subject_types_for
from hailhq.core.providers.verification import (
    Address,
    DocumentInput,
    SubmitRefused,
    UnsupportedSubjectType,
    UploadedFile,
    VerificationProviderError,
    get_verification_provider,
)
from hailhq.core.providers.verification.base import NOT_ACCEPTED, VALUE_NOT_ACCEPTED
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


@pytest.mark.parametrize("subject", ["person", "business"])
async def test_uk_website_is_only_collected_for_businesses(subject) -> None:
    # Captured from the carrier on 2026-10-26: the website explicitly says
    # "Business Use Only"; contact, identity and address apply to both kinds.
    payload = json.loads((FIXTURES / "telnyx-regulatory-GB-mobile.json").read_text())
    fake = Telnyx(lambda request: httpx.Response(200, json=payload))
    req = await provider(fake).requirements("GB", "mobile", subject)
    fields = {f.name: f for f in req.fields}
    website = "1ce58926-d0d9-41c7-be1b-23b7bbbc572d"
    if subject == "business":
        assert fields[website].required
    else:
        assert website not in fields
    assert fields["2708e569-696a-4fc7-9305-5fdb3eb9c7dd"].required
    assert req.address_required
    assert {s.name for s in req.documents} == {
        "1cfde0b1-f85e-4444-9c1a-413cfa3f079f",
        "b0197fa1-c2fd-4500-9875-2c658b2396eb",
    }


async def test_uk_person_draft_sends_na_for_the_company_website() -> None:
    payload = json.loads((FIXTURES / "telnyx-regulatory-GB-mobile.json").read_text())
    rules = payload["data"][0]["regulatory_requirements"]
    stored: dict[str, str] = {}
    website = "1ce58926-d0d9-41c7-be1b-23b7bbbc572d"

    def answers(request):
        path = request.url.path
        if path.endswith("/regulatory_requirements"):
            return httpx.Response(200, json=payload)
        if request.method == "POST" and path.endswith("/requirement_groups"):
            return httpx.Response(200, json={"data": {"id": GROUP}})
        if request.method == "POST" and path.endswith("/addresses"):
            return httpx.Response(200, json={"data": {"id": "addr-person"}})
        if request.method == "POST" and path.endswith("/documents"):
            return httpx.Response(200, json={"data": {"id": "doc-person"}})
        if request.method == "PATCH":
            for item in json.loads(request.content)["regulatory_requirements"]:
                stored[item["requirement_id"]] = item["field_value"]
            return httpx.Response(200, json={"data": {}})
        if request.method == "GET" and GROUP in path:
            return httpx.Response(
                200,
                json={
                    "data": {
                        "country_code": "GB",
                        "phone_number_type": "mobile",
                        "regulatory_requirements": [
                            {
                                "requirement_id": r["id"],
                                "field_type": r["field_type"],
                                "field_value": stored.get(r["id"], ""),
                                "status": "unapproved",
                            }
                            for r in rules
                        ],
                    }
                },
            )
        if request.method == "DELETE":
            return httpx.Response(204)
        raise AssertionError(f"unexpected {request.method} {path}")

    fake = Telnyx(answers)
    p = provider(fake)
    req = await p.requirements("GB", "mobile", "person")
    _, address, _ = _inputs("mobile")
    fields = {
        "2708e569-696a-4fc7-9305-5fdb3eb9c7dd": "Contact: Ada Lovelace | Business: N/A | Phone: +447700900123",
        "c34075fd-dde8-4e5d-b2d1-73155062e77a": "Personal calls. No sub-allocation.",
    }
    docs = {
        r["id"]: DocumentInput(
            option="file",
            file=UploadedFile(
                filename="proof.pdf",
                content_type="application/pdf",
                data=b"%PDF-1.4 test",
            ),
        )
        for r in rules
        if r["field_type"] == "document"
    }
    result = await p.create_draft(
        organization_id=ORG,
        contact_email="ops@hail.test",
        requirements=req,
        fields=fields,
        address=address,
        documents=docs,
    )
    assert result.problems == []
    assert result.refs["group_id"] == GROUP
    # Telnyx refuses to submit a group with an empty value, even one tagged
    # "Business Use Only" (seen live on 2026-10-26), so Hail fills it in.
    assert stored[website] == "N/A"
    refs = json.loads(json.dumps(result.refs))
    assert await provider(fake).check(refs) == []
    stored.pop("2708e569-696a-4fc7-9305-5fdb3eb9c7dd")
    assert [p.field for p in await provider(fake).check(refs)] == [
        "2708e569-696a-4fc7-9305-5fdb3eb9c7dd"
    ]


def _tagged_rules() -> dict:
    """Brazil-style rules: one shared, one per kind of end user."""
    rules = [
        ("shared", "Contact Information (Name, Business Name, Phone Number)"),
        ("biz", "Brazilian CNPJ Tax Number (Business Use Only)"),
        (
            "ita",
            "Italian VAT Number (For Legal Entities and Sole Proprietorships Only)",
        ),
        ("per", "Brazilian CPF Tax Number (Personal Use Only)"),
    ]
    return {
        "data": [
            {
                "country_code": "BR",
                "phone_number_type": "local",
                "action": "ordering",
                "regulatory_requirements": [
                    {"id": rid, "name": name, "field_type": "textual"}
                    for rid, name in rules
                ],
            }
        ]
    }


@pytest.mark.parametrize(
    ("subject", "expected"),
    [("person", ["shared", "ita", "per"]), ("business", ["shared", "biz", "ita"])],
)
async def test_rules_tagged_for_the_other_subject_are_left_out(
    subject, expected
) -> None:
    fake = Telnyx(lambda request: httpx.Response(200, json=_tagged_rules()))
    req = await provider(fake).requirements("BR", "local", subject)
    assert [f.name for f in req.fields] == expected


def _with_business_file() -> dict:
    payload = _tagged_rules()
    payload["data"][0]["regulatory_requirements"].append(
        {
            "id": "biz-doc",
            "name": "Company Registration (Business Use Only)",
            "field_type": "document",
        }
    )
    return payload


async def test_business_only_file_means_only_a_business_can_buy() -> None:
    # A file cannot be filled in, and Telnyx will not submit without it.
    fake = Telnyx(lambda request: httpx.Response(200, json=_with_business_file()))
    with pytest.raises(UnsupportedSubjectType) as refused:
        await provider(fake).requirements("BR", "local", "person")
    assert refused.value.allowed == ("business",)
    req = await provider(fake).requirements("BR", "local", "business")
    assert req.subject_types == ("business",)
    assert [d.name for d in req.documents] == ["biz-doc"]


@pytest.mark.parametrize(
    ("rule", "allowed"),
    [
        ({"name": "Company Registration (Business Use Only)"}, ("business",)),
        ({"name": "Legal Rep Address (Legal Entity Only)"}, ("business",)),
        # A business can give its representative's ID.
        ({"name": "ID/Passport Copy (Personal Use Only)"}, ("person", "business")),
        (
            {"name": "VAT (For Legal Entities and Sole Proprietorships Only)"},
            ("person", "business"),
        ),
    ],
)
def test_subject_types_for_file_rules(rule, allowed) -> None:
    assert subject_types_for([{**rule, "field_type": "document"}]) == allowed
    # A text rule never limits who can buy: Hail sends N/A for it.
    assert subject_types_for([{**rule, "field_type": "textual"}]) == (
        "person",
        "business",
    )


async def test_requirements_needed_when_only_other_subject_rules_exist() -> None:
    payload = _tagged_rules()
    rules = payload["data"][0]["regulatory_requirements"]
    payload["data"][0]["regulatory_requirements"] = [
        r for r in rules if r["id"] == "biz"
    ]
    fake = Telnyx(lambda request: httpx.Response(200, json=payload))
    req = await provider(fake).requirements("BR", "local", "person")
    # The carrier still wants a group, so the number is not open to buy.
    assert req.required is True and req.fields == ()


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
    # Hail's wording for the customer; the carrier's text is kept for the log.
    assert [pr.message for pr in result.problems] == [NOT_ACCEPTED]
    assert [pr.raw for pr in result.problems] == ["File too large"]
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
    assert any(p.message == VALUE_NOT_ACCEPTED for p in problems)


async def test_submit_posts_for_approval() -> None:
    fake = Telnyx(lambda request: httpx.Response(200, json={"data": _group("mobile")}))
    await provider(fake).submit({"group_id": GROUP})
    (call,) = fake.calls
    assert call.method == "POST"
    assert call.url.path == f"/v2/requirement_groups/{GROUP}/submit_for_approval"


def _submit_422(group_status: str):
    errors = {"errors": [{"title": "Invalid", "detail": "Group is not fulfilled"}]}

    def answers(request):
        if request.method == "POST":
            return httpx.Response(422, json=errors)
        return httpx.Response(200, json={"data": _group("mobile", group_status)})

    return Telnyx(answers)


@pytest.mark.parametrize("group_status", ["pending-approval", "approved"])
async def test_submit_of_a_group_already_sent_succeeds(group_status) -> None:
    # Telnyx answers 422 when a group is sent again; it is already in review.
    await provider(_submit_422(group_status)).submit({"group_id": GROUP})


async def test_submit_refused_by_telnyx_is_final() -> None:
    fake = _submit_422("unapproved")
    with pytest.raises(SubmitRefused) as refused:
        await provider(fake).submit({"group_id": GROUP})
    assert [(p.message, p.raw) for p in refused.value.problems] == [
        (NOT_ACCEPTED, "Group is not fulfilled")
    ]


@pytest.mark.parametrize("code", [401, 429, 503])
async def test_submit_other_errors_stay_retryable(code) -> None:
    fake = Telnyx(lambda request: httpx.Response(code, json={"errors": []}))
    with pytest.raises(VerificationProviderError) as failed:
        await provider(fake).submit({"group_id": GROUP})
    assert not isinstance(failed.value, SubmitRefused)


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
        "verification_id": GROUP,
        "address_id": None,
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
