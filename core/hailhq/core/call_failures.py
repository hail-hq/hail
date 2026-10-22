"""Public call-failure wording. Never expose carrier error text."""

DESTINATION_NOT_ALLOWED = "destination_not_allowed"


def call_failure_message(status: str, metadata: dict) -> str | None:
    if status != "failed":
        return None
    if metadata.get("failure_code") == DESTINATION_NOT_ALLOWED:
        return (
            "We cannot call this destination right now. "
            "Please try a number in another country or region."
        )
    return "We could not complete your call. Please try again in a few minutes."
