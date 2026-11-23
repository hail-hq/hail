"""Schemas for the structured results published in MCP tools/list.

Use the response models HailClient serializes, with the additions and
projections made by the tool functions. These schemas document the wire
contract without changing the SDK's existing conversion of returned dicts.
"""

from __future__ import annotations

from typing import Any

from hailhq.core import schemas as api
from hailhq.core.carrier_offer import NumberQuotesResponse
from hailhq.core.providers.verification.base import VerificationRequirements
from hailhq.core.providers.verification.sms_form import CampaignText
from hailhq.core.regions import NumberRegionsResponse
from pydantic import TypeAdapter
from pydantic.json_schema import GenerateJsonSchema


class _SerializedFields(GenerateJsonSchema):
    def field_is_required(self, field, total):
        # HailClient dumps defaults and nulls, even when the API omitted them.
        if self.mode == "serialization" and field["type"] in {
            "model-field",
            "dataclass-field",
        }:
            return (
                not field.get("serialization_exclude")
                and field.get("serialization_exclude_if") is None
            )
        return super().field_is_required(field, total)


def _model(
    model: type,
    *,
    extra: dict[str, Any] | None = None,
    by_alias: bool = False,
) -> dict[str, Any]:
    schema = TypeAdapter(model).json_schema(
        mode="serialization", by_alias=by_alias, schema_generator=_SerializedFields
    )
    if extra:
        schema["properties"].update(extra)
        schema.setdefault("required", []).extend(extra)
    return schema


def _object(properties: dict[str, Any]) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": list(properties)}


def _result(
    *successes: dict[str, Any], error_fields: dict[str, Any] | None = None
) -> dict[str, Any]:
    error = _object({"error": _STRING})
    if error_fields:
        error["properties"].update(error_fields)
    definitions: dict[str, Any] = {}
    alternatives = []
    for success in successes:
        # Pydantic's local references are relative to the whole outputSchema.
        definitions.update(success.get("$defs", {}))
        alternatives.append({k: v for k, v in success.items() if k != "$defs"})
    schema = {"type": "object", "anyOf": [*alternatives, error]}
    if definitions:
        schema["$defs"] = definitions
    return schema


_STRING = {"type": "string"}
_TRUE = {"type": "boolean", "const": True}
_RETRY_KEY = {
    "idempotency_key": {
        "type": "string",
        "description": "Key that identifies this exact request when retrying.",
    },
}
_PROBLEMS = {
    "problems": {
        "type": "array",
        "description": "Validation issues with an input path and a message.",
        "items": _object(
            {
                "loc": {"type": "array", "items": {"type": ["string", "integer"]}},
                "msg": _STRING,
            }
        ),
    },
}
_FILE_URL = _result(
    _object({"url": {"type": "string", "description": "Short-lived download URL."}})
)
_VERIFICATION = _model(api.VerificationResponse)
_VERIFICATION_RESULT = _result(_VERIFICATION)
_REGISTRATION_RESULT = _result(_VERIFICATION, error_fields=_PROBLEMS)
_VERIFICATION_LIST = _object(
    {
        "items": {
            "type": "array",
            "items": {k: v for k, v in _VERIFICATION.items() if k != "$defs"},
        }
    }
)
_VERIFICATION_LIST["$defs"] = _VERIFICATION.get("$defs", {})
_PREVIEW = _model(CampaignText)
_PREVIEW["properties"]["schema_version"]["minimum"] = 1
_PREVIEW["additionalProperties"] = False
_AGENT = _result(_model(api.AgentResponse))
_NUMBER = _result(_model(api.PhoneNumberResponse))
_CONTACTS = _result(_model(api.ContactListResponse))
_MANUAL_CONTACT = _model(api.ContactEntry)
_MANUAL_CONTACT["description"] = "A manually saved workspace contact."
_MANUAL_CONTACT["properties"].update(
    {
        "id": {"type": "string", "description": "The contact's ID."},
        "kind": {"type": "string", "const": "manual"},
        "role": {
            "type": "null",
            "description": "Manual contacts have no workspace role.",
        },
    }
)
_QUOTES = _model(NumberQuotesResponse)
# This tool never sends subject_type, so the API omits subject-specific paperwork.
_QUOTES["$defs"]["QuoteOffer"]["properties"]["paperwork"] = {
    "type": "null",
    "description": "Always null for this tool. Applicant-specific paperwork is "
    "available from get_verification_requirements for the chosen offer.",
}
_QUOTES["$defs"].pop("PaperworkItem")

OUTPUT_SCHEMAS = {
    "place_call": _result(_model(api.CallResponse, extra=_RETRY_KEY)),
    "send_email": _result(_model(api.EmailResponse, extra=_RETRY_KEY)),
    "upload_email_attachment": _result(_model(api.EmailAttachmentUploadResponse)),
    "create_file_upload": _result(
        _model(
            api.FileUploadLinkResponse,
            extra={
                "curl": {"type": "string", "description": "Upload command template."},
                "next": {"type": "string", "description": "Upload instructions."},
            },
        )
    ),
    "get_call": _result(_model(api.CallResponse)),
    "list_calls": _result(_model(api.CallListResponse)),
    "send_sms": _result(_model(api.SmsResponse, extra=_RETRY_KEY)),
    "get_sms": _result(_model(api.SmsResponse)),
    "list_sms": _result(_model(api.SmsListResponse)),
    "get_email": _result(_model(api.EmailResponse)),
    "list_emails": _result(_model(api.EmailListResponse)),
    "get_email_raw": _FILE_URL,
    "get_email_attachment": _FILE_URL,
    "get_email_events": _result(_model(api.EmailEventListResponse)),
    "get_email_stats": _result(_model(api.EmailStatsResponse, by_alias=True)),
    "get_events": _result(_model(api.EventStreamResponse)),
    "list_agents": _result(_model(api.AgentListResponse)),
    "create_agent": _AGENT,
    "route_number": _NUMBER,
    "route_email_domain": _result(_model(api.EmailDomainResponse)),
    "quote_numbers": _result(_QUOTES),
    "acquire_number": _result(
        _model(api.PhoneNumberResponse, extra=_RETRY_KEY), error_fields=_RETRY_KEY
    ),
    "list_numbers": _result(_model(api.PhoneNumberListResponse)),
    "list_number_regions": _result(_model(NumberRegionsResponse)),
    "get_number": _NUMBER,
    "release_number": _result(
        _object({"released": _TRUE, "number_id": _STRING, "e164": _STRING}),
        _object({"dismissed": _TRUE, "number_id": _STRING, "e164": _STRING}),
    ),
    "get_verification_requirements": _result(_model(VerificationRequirements)),
    "check_verification": _result(
        _model(api.VerificationCheckResponse), error_fields=_PROBLEMS
    ),
    "check_verification_documents": _result(
        _model(api.VerificationDocumentCheckResponse), error_fields=_PROBLEMS
    ),
    "submit_verification": _REGISTRATION_RESULT,
    "resubmit_verification": _REGISTRATION_RESULT,
    "list_verifications": _result(_VERIFICATION_LIST),
    "get_verification": _VERIFICATION_RESULT,
    "get_sms_application": _result(_model(api.SmsApplicationResponse)),
    "preview_sms_application": _result(_PREVIEW, error_fields=_PROBLEMS),
    "amend_sms_application": _REGISTRATION_RESULT,
    "cancel_verification": _VERIFICATION_RESULT,
    "confirm_verification": _VERIFICATION_RESULT,
    "get_agent": _AGENT,
    "update_agent": _AGENT,
    "delete_agent": _result(
        _object({"deleted": _TRUE, "agent_id": _STRING, "name": _STRING})
    ),
    "list_contacts": _CONTACTS,
    "lookup_contact": _CONTACTS,
    "list_email_domains": _result(_model(api.EmailDomainListResponse)),
    "whoami": _result(_model(api.WhoamiResponse)),
    "create_contact": _result(_MANUAL_CONTACT),
}
