from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from hailhq.core.schemas import (
    CallCreate,
    ContactCreate,
    ContactPatch,
    EmailCreate,
    EmailDomainPatch,
    EmailResponse,
    EmailSummary,
    LLMConfig,
    VoiceConfig,
    parse_resource_id,
)
from pydantic import ValidationError


def test_call_create_minimal_valid():
    req = CallCreate(to="+14155551234", system_prompt="Hi", recipient_consent=True)
    assert req.to == "+14155551234"
    assert req.system_prompt == "Hi"
    assert req.llm is None


def test_call_create_with_byo_endpoint():
    req = CallCreate(
        to="+14155551234",
        llm=LLMConfig(base_url="https://byo.example.com/v1", api_key="k", model="m"),
        recipient_consent=True,
    )
    assert req.llm is not None
    assert req.llm.base_url == "https://byo.example.com/v1"


def test_call_create_rejects_non_e164():
    with pytest.raises(ValidationError):
        CallCreate(to="4155551234", system_prompt="Hi", recipient_consent=True)


def test_call_create_requires_prompt_or_llm():
    with pytest.raises(ValidationError):
        CallCreate(to="+14155551234", recipient_consent=True)


def test_call_create_accepts_prompt_and_llm_together():
    """Mode B plus a task prompt is a legitimate combination — the voicebot's
    ``build_instructions`` composes the preamble + prompt for either LLM."""
    req = CallCreate(
        to="+14155551234",
        system_prompt="Hi",
        llm=LLMConfig(base_url="https://byo.example.com/v1", api_key="k", model="m"),
        recipient_consent=True,
    )
    assert req.system_prompt == "Hi"
    assert req.llm is not None
    assert req.llm.model == "m"


def test_call_create_requires_recipient_consent_field():
    """``recipient_consent`` is required — no default — omitting it is a 422."""
    with pytest.raises(ValidationError, match="recipient_consent"):
        CallCreate(to="+14155551234", system_prompt="Hi")


def test_call_create_message_type_defaults_to_informational():
    req = CallCreate(to="+14155551234", system_prompt="Hi", recipient_consent=True)
    assert req.message_type == "informational"


def test_voice_config_defaults():
    cfg = VoiceConfig()
    assert cfg.tts == "cartesia"
    assert cfg.vad == "silero"


def test_parse_resource_id_call_happy_path():
    u = uuid4()
    rtype, rid = parse_resource_id(f"call:{u}")
    assert rtype == "call"
    assert rid == u
    assert isinstance(rid, UUID)


def test_parse_resource_id_missing_colon():
    with pytest.raises(ValueError, match="missing ':'"):
        parse_resource_id("nocolon")


def test_parse_resource_id_supports_sms():
    u = uuid4()
    rtype, rid = parse_resource_id(f"sms:{u}")
    assert rtype == "sms"
    assert rid == u


def test_parse_resource_id_unsupported_type():
    with pytest.raises(ValueError, match=r"unsupported resource type 'fax'"):
        parse_resource_id(f"fax:{uuid4()}")


def test_parse_resource_id_empty_type():
    with pytest.raises(ValueError, match="missing resource type"):
        parse_resource_id(":")


def test_parse_resource_id_empty_id():
    with pytest.raises(ValueError, match="missing resource id"):
        parse_resource_id("call:")


def test_parse_resource_id_bad_uuid():
    with pytest.raises(ValueError, match="invalid uuid"):
        parse_resource_id("call:not-a-uuid")


def _email_row_stub(**overrides) -> SimpleNamespace:
    """Duck-typed stand-in for the ``Email`` SQLAlchemy model.

    ``model_validate(from_attributes=True)`` reads attributes, not dict
    keys — ``SimpleNamespace`` is enough and keeps the test free of a
    DB dependency.
    """
    base = {
        "id": uuid4(),
        "organization_id": uuid4(),
        "email_domain_id": uuid4(),
        "from_address": "alice+acme@mail.hail.so",
        "to_addresses": ["dest@example.com"],
        "cc_addresses": None,
        "bcc_addresses": None,
        "reply_to": None,
        "subject": "hi",
        "body_text": "hello",
        "body_html": None,
        "status": "sent",
        "end_reason": None,
        "provider_message_id": "ses-msg-1",
        "requested_at": datetime.now(timezone.utc),
        "sent_at": datetime.now(timezone.utc),
        "failed_at": None,
        "metadata_": {"campaign_id": "spring-2026"},
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def test_email_response_metadata_alias_maps_from_metadata_underscore():
    """The wire field ``metadata`` reads the ORM attribute ``metadata_``.

    ``metadata`` is reserved by SQLAlchemy's ``DeclarativeBase``, so the
    ``Email`` model stores the column under ``metadata_`` with
    ``Column("metadata")``. ``EmailResponse`` bridges that via
    ``validation_alias='metadata_'``. If anyone ever changes the alias,
    the response field would silently go empty — catch that with a
    direct assertion rather than relying on the API integration test.
    """
    row = _email_row_stub(metadata_={"campaign_id": "spring-2026", "tier": "free"})
    resp = EmailResponse.model_validate(row, from_attributes=True)
    assert resp.metadata == {"campaign_id": "spring-2026", "tier": "free"}

    dumped = resp.model_dump()
    assert dumped["metadata"] == {"campaign_id": "spring-2026", "tier": "free"}
    assert "metadata_" not in dumped


def test_email_summary_omits_bodies_keeps_metadata_alias():
    """``EmailSummary`` (used by ``GET /emails``) must drop ``body_text``
    and ``body_html`` while still wiring ``metadata_`` to ``metadata``.
    """
    row = _email_row_stub(
        body_text="secret notes",
        body_html="<p>secret</p>",
        metadata_={"tier": "free"},
    )
    summary = EmailSummary.model_validate(row, from_attributes=True)
    dumped = summary.model_dump()
    assert "body_text" not in dumped
    assert "body_html" not in dumped
    assert dumped["metadata"] == {"tier": "free"}


def test_sms_create_requires_consent() -> None:
    import pytest
    from hailhq.core.schemas import SmsCreate
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        SmsCreate(to="+14155551234", body="hi")  # missing recipient_consent


def test_sms_create_validates_e164() -> None:
    import pytest
    from hailhq.core.schemas import SmsCreate
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        SmsCreate(to="not-a-number", body="hi", recipient_consent=True)


def test_sms_create_happy_path() -> None:
    from hailhq.core.schemas import SmsCreate

    sms = SmsCreate(to="+14155551234", body="hi", recipient_consent=True)
    assert sms.to == "+14155551234"
    assert sms.message_type == "informational"


def test_call_create_still_requires_consent_after_mixin_refactor() -> None:
    """Regression: CallCreate moving onto ConsentAttestationMixin must not
    change its externally-visible required-field behavior."""
    import pytest
    from hailhq.core.schemas import CallCreate
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        CallCreate(to="+14155551234", system_prompt="hi")  # missing recipient_consent


def test_email_create_still_requires_consent_after_mixin_refactor() -> None:
    import pytest
    from hailhq.core.schemas import EmailCreate
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        EmailCreate(
            to=["a@example.com"], subject="hi", body_text="hi"
        )  # missing recipient_consent


def test_email_create_accepts_from_name() -> None:
    email = EmailCreate(
        to=["a@example.com"],
        subject="hi",
        body_text="hi",
        from_name="Acme Billing",
        recipient_consent=True,
    )
    assert email.from_name == "Acme Billing"


def test_email_create_rejects_newlines_in_from_name() -> None:
    """A display name lands in the From: header — CR/LF would allow
    header injection. U+0085 (NEL) is a C1 control some header parsers
    treat as a line break."""
    for bad in (
        "Acme\r\nBcc: evil@x.com",
        "Acme\nX",
        "Acme\rX",
        "Acme\x00X",
        "Acme\x85X",
    ):
        with pytest.raises(ValidationError):
            EmailCreate(
                to=["a@example.com"],
                subject="hi",
                body_text="hi",
                from_name=bad,
                recipient_consent=True,
            )


def test_email_create_rejects_overlong_from_name() -> None:
    with pytest.raises(ValidationError):
        EmailCreate(
            to=["a@example.com"],
            subject="hi",
            body_text="hi",
            from_name="x" * 257,
            recipient_consent=True,
        )


def test_email_create_strips_from_name_before_length_check() -> None:
    """Whitespace padding must not push a valid name over max_length."""
    email = EmailCreate(
        to=["a@example.com"],
        subject="hi",
        body_text="hi",
        from_name="Acme" + " " * 260,
        recipient_consent=True,
    )
    assert email.from_name == "Acme"


MALFORMED_ADDRESSES = [
    "<a@b.co>",
    "a,b@c.co",
    "a;b@c.co",
    "a@b.co,c@d.co",
    "a@b.co\n",
    "a\x00b@c.co",
    "a@b\x7f.co",
    '"a@b.co',
    "(a@b.co",
    "x:a@b.co",
    "a\\b@c.co",
    "a@b..co",
    ".a@b.co",
    "a..b@c.co",
    "a@-b.co",
    '"a,b"@c.co',
]
NORMAL_ADDRESSES = [
    "a+tag@sub.example.co",
    "o'brien@example.com",
    "a.b_c-d@example.co",
    "a@b.c",
    "a@xn--bcher-kva.example",
    "a@b\u00fccher.example",
    "m\u00fcller@example.de",
]


def _email(**fields) -> EmailCreate:
    return EmailCreate(
        **{
            "to": ["a@b.co"],
            "subject": "hi",
            "body_text": "hi",
            "recipient_consent": True,
            **fields,
        }
    )


@pytest.mark.parametrize("field", ["to", "cc", "bcc"])
@pytest.mark.parametrize("address", MALFORMED_ADDRESSES)
def test_email_create_refuses_a_malformed_recipient(field, address):
    with pytest.raises(ValidationError, match="invalid email address"):
        _email(**{field: [address]})


@pytest.mark.parametrize("field", ["from", "reply_to"])
@pytest.mark.parametrize("address", MALFORMED_ADDRESSES)
def test_email_create_refuses_a_malformed_sender(field, address):
    with pytest.raises(ValidationError, match="must be a valid email address"):
        _email(**{field: address})


@pytest.mark.parametrize("model", [ContactCreate, ContactPatch])
@pytest.mark.parametrize("address", MALFORMED_ADDRESSES)
def test_contact_refuses_a_malformed_email(model, address):
    with pytest.raises(ValidationError, match="must be a valid email address"):
        model(name="Bob", email=address)


# forward targets are stripped before they are stored, so a newline is harmless.
@pytest.mark.parametrize(
    "address", [a for a in MALFORMED_ADDRESSES if not a.endswith("\n")]
)
def test_forward_to_refuses_a_malformed_address(address):
    with pytest.raises(ValidationError, match="is not an email address"):
        EmailDomainPatch(forward_to=[address])


def test_forward_to_keeps_the_address_it_was_given():
    assert EmailDomainPatch(forward_to=[" a@b.co "]).forward_to == [" a@b.co "]


@pytest.mark.parametrize("address", NORMAL_ADDRESSES)
def test_email_create_keeps_accepting_normal_addresses(address):
    assert _email(to=[address]).to == [address]


@pytest.mark.parametrize(
    "number",
    ["+14155551234\n", "14155551234", "+1\u0664\u0661\u0665\u0665\u0665\u0661\u0662"],
)
def test_contact_refuses_a_malformed_phone_number(number):
    with pytest.raises(ValidationError):
        ContactCreate(name="Bob", phone_e164=number)
