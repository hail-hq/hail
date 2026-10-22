"""Public call-failure wording. Never expose carrier error text."""

from hailhq.core.call_end_reasons import CallEndReason

DESTINATION_NOT_ALLOWED = "destination_not_allowed"

CALL_FAILED_MESSAGE = (
    "We could not complete your call. Please try again in a few minutes."
)

# Failures a retry cannot fix. Every other end_reason gets CALL_FAILED_MESSAGE.
_END_REASON_MESSAGES = {
    CallEndReason.INSUFFICIENT_FUNDS.value: (
        "Your balance is empty. Add funds to make and receive calls."
    ),
    CallEndReason.NO_AGENT.value: (
        "This number has no active voice agent. "
        "Assign an agent or resume it to answer calls."
    ),
    CallEndReason.PROVIDER_KEY_ERROR.value: (
        "Your provider API key was rejected. Check the key, then try again."
    ),
    CallEndReason.LLM_ENDPOINT_FAILED.value: (
        "Your LLM endpoint kept returning errors, so the call was ended. "
        "Check the endpoint, then try again."
    ),
    CallEndReason.CARRIER_ROUTE_FAILED.value: (
        "This number cannot carry calls right now. Please contact support."
    ),
}


def call_failure_message(
    status: str, metadata: dict, end_reason: str | None = None
) -> str | None:
    if status != "failed":
        return None
    if metadata.get("failure_code") == DESTINATION_NOT_ALLOWED:
        return (
            "We cannot call this destination right now. "
            "Please try a number in another country or region."
        )
    return _END_REASON_MESSAGES.get(end_reason or "", CALL_FAILED_MESSAGE)
