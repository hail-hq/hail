"""Public call-failure wording. Never expose carrier error text."""

DESTINATION_NOT_ALLOWED = "destination_not_allowed"


def call_failure_message(status: str, metadata: dict) -> str | None:
    if status != "failed":
        return None
    if metadata.get("failure_code") == DESTINATION_NOT_ALLOWED:
        return (
            "Calling this destination is currently disabled. "
            "Try another destination or contact support."
        )
    return "The call failed. Try again or contact support."
