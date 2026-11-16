"""What the customer reads when a carrier rejects a number verification.

Each carrier says why in its own way. This module turns that into one
reason the customer can act on: Hail's own fix for each known carrier code,
and the carrier's own text for anything else. The reason is stored in
``carrier_verifications.rejection_reason``, which the console, the email,
the API and the MCP server all show.

The carrier's text can quote what the customer filed (a name, an address),
so it is never logged. It is shown to the workspace (console, API) and in
the rejection email to the workspace owners, and kept in the audit row.
"""

from __future__ import annotations

from collections.abc import Iterable

from hailhq.core.providers.verification.base import DETAILS_REJECTED

# Twilio Regulatory Compliance codes, from Twilio's error list
# (https://www.twilio.com/docs/api/errors, 18001-18064). A code missing here
# shows Twilio's own text instead.
TWILIO_FIXES: dict[int, str] = {
    18001: "The document is not a type the carrier accepts here. "
    "Upload one of the accepted document types.",
    18002: "The document is hard to read. Upload a sharp, well-lit photo or "
    "scan of the whole document.",
    18003: "The document has expired or is not valid. Upload one that is "
    "still valid.",
    18004: "Part of the document is covered or blacked out. Upload it with "
    "nothing hidden.",
    18005: "Some details are missing. Fill in every field.",
    18006: "The details you typed do not match the document. Type them exactly "
    "as printed on it.",
    18007: "The ID must show both sides. Upload the front and the back "
    "together in one file.",
    18008: "The name you typed does not match the ID. Type the first and last "
    "names exactly as printed on it, with every given name and surname.",
    18009: "The nationality you gave does not match the ID. Use the "
    "nationality printed on it.",
    18010: "The company number does not match the company register. Type it "
    "exactly as registered.",
    18011: "The business name does not match the company register. Type it "
    "exactly as registered.",
    18012: "This number type needs a document that shows an address in the "
    "number's country. Upload one with a local address.",
    18013: "This number type needs an emergency address in the number's "
    "country. Upload a document that shows one.",
    18014: "The address is incomplete or not valid. Enter the full address.",
    18015: "The emergency address is incomplete or not valid. Enter the full "
    "street address.",
    18016: "The address you typed does not match the document. Type it exactly "
    "as printed on it.",
    18017: "The document shows no address. Upload one that shows your address.",
    18018: "PO boxes are not accepted. Enter a street address.",
    18019: "The authorized representative's ID is missing. Upload their "
    "passport or government ID.",
    18020: "The company document does not name the authorized representative. "
    "Upload one that does.",
    18021: "The name on the proof of address does not match the name you "
    "typed. Type it exactly as printed on the document.",
    18022: "The proof of address is too old. A person needs one issued in the "
    "last 3 months, a business one issued in the last year.",
    18023: "The company register lists the business as inactive. Upload a "
    "document that shows it is active.",
    18024: "The file could not be opened. Upload it again as a JPG, PNG or PDF.",
    18052: "The person must be old enough: over 13 in the US and UK, over 16 "
    "in the EU.",
    18054: "The phone number is not valid. Enter it in international format, "
    "like +14155550100.",
    18055: "The email address is not valid. Enter a working email address.",
    18059: "The authorized representative's photo ID is missing or not valid. "
    "Upload a valid one.",
    18060: "The excerpt from the commercial register is missing. Upload it.",
    18061: "The company registration could not be checked. Upload a current "
    "excerpt from the commercial register.",
    18062: "The company register lists the business as inactive. Enter the "
    "details of an active business.",
    18064: "The details do not meet the carrier's rules for a business. Try "
    "again as a person.",
}

# "There is an issue with one or more of your Supporting Documents."
_TWILIO_SUMMARY = {18050}

START_AGAIN = "Then start again."
FIX_AND_START_AGAIN = "Fix that and start again."


def _sentence(text: str) -> str:
    text = text.strip()
    return text if text.endswith((".", "!", "?")) else text + "."


def customer_reason(fixes: Iterable[str], carrier_texts: Iterable[str]) -> str:
    """One reason for the customer. ``fixes`` are Hail's own sentences;
    ``carrier_texts`` are the carrier's own words for problems Hail has no
    fix for. Duplicates are dropped, order is kept. Nothing at all gives
    Hail's generic wording."""
    parts = list(dict.fromkeys(f.strip() for f in fixes if f and f.strip()))
    said = list(dict.fromkeys(_sentence(t) for t in carrier_texts if t and t.strip()))
    if said:
        parts.append("The carrier said: " + " ".join(said))
    if not parts:
        return DETAILS_REJECTED
    # After the carrier's words, "Then" would follow no step of ours.
    end = FIX_AND_START_AGAIN if said else START_AGAIN
    return " ".join([*parts, end])


def twilio_reason(problems: Iterable[tuple[int | None, str | None]]) -> str:
    """``problems`` are (error code, Twilio's text) pairs from the rejected
    bundle's documents and evaluation. Twilio's summary code ("see each
    document") is dropped when a document says more."""
    problems = list(problems)
    if any(code not in _TWILIO_SUMMARY for code, _ in problems):
        problems = [p for p in problems if p[0] not in _TWILIO_SUMMARY]
    fixes: list[str] = []
    said: list[str] = []
    for code, text in problems:
        fix = TWILIO_FIXES.get(code) if code is not None else None
        if fix:
            fixes.append(fix)
        elif text:
            said.append(text)
    return customer_reason(fixes, said)
