"""Typed-resource-id parsing for ``<type>:<uuid>`` strings.

Mirrors ``hailhq.core.schemas.parse_resource_id`` byte-for-byte (including
error message strings) so an SDK user gets the same diagnostic as the API
and the CLI. The SDK can't import from ``hailhq.core`` (it ships
standalone), hence the duplication. Keep these in lockstep when adding new
resource types — supported set lives in :data:`SUPPORTED_RESOURCE_TYPES`.
"""

from __future__ import annotations

from uuid import UUID

from hail._errors import HailMalformedResourceId

SUPPORTED_RESOURCE_TYPES: tuple[str, ...] = ("call",)


def parse_resource_id(value: str) -> tuple[str, UUID]:
    """Parse a ``<type>:<uuid>`` resource id.

    Raises :class:`hail.HailMalformedResourceId` on any of:
      * missing colon
      * empty type or empty id
      * unknown type (not in :data:`SUPPORTED_RESOURCE_TYPES`)
      * id that is not a valid UUID
    """
    if ":" not in value:
        raise HailMalformedResourceId(
            "must be '<type>:<uuid>' (e.g. 'call:abc-...'); missing ':'"
        )
    type_str, _, id_str = value.partition(":")
    if not type_str:
        raise HailMalformedResourceId("missing resource type before ':'")
    if not id_str:
        raise HailMalformedResourceId("missing resource id after ':'")
    if type_str not in SUPPORTED_RESOURCE_TYPES:
        supported = ", ".join(SUPPORTED_RESOURCE_TYPES)
        raise HailMalformedResourceId(
            f"unsupported resource type '{type_str}'; supported: [{supported}]"
        )
    try:
        return type_str, UUID(id_str)
    except ValueError as exc:
        raise HailMalformedResourceId(f"invalid uuid '{id_str}': {exc}") from exc


__all__ = ["SUPPORTED_RESOURCE_TYPES", "parse_resource_id"]
