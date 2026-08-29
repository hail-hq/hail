"""``/providers`` — public, API-key-authenticated standing BYO provider config.

Same rows, same semantics as the console's
``/internal/orgs/{organization_id}/providers`` router: per-org ``llm`` /
``tts`` / ``stt`` provider keys, write-only (stored Fernet-encrypted,
surfaced only as last4 + set-at). This router exists so hosted customers who
manage configuration as code don't have to click through the console.

**Auth and scope.** The organization is resolved from the caller's API key
(``get_current_principal``) and never appears in a path or body — one org's
key cannot address another org's rows.

**No refactor of the internal router.** The console runs on
``routes/internal/provider_config.py`` in production; extracting shared
handlers out of it to serve this route would put a working page at risk for
no user-visible gain. So this file is a thin auth-and-scope layer that
imports the internal handlers and calls them with the resolved organization.
The duplication is the intended tradeoff. Behavior — upsert-then-activate,
delete-with-sibling-promotion, 409 on a concurrent write, 503 when
``HAIL_PROVIDER_SECRET_KEY`` is unset — is therefore identical by
construction, not by parallel maintenance.

Request/response models live in ``hailhq.core.schemas`` (not in the internal
module) because they are the public contract: their names become the OpenAPI
component names and, downstream, the Go CLI's and the SDK's type names. The
internal handlers return bare ``dict``; ``response_model`` here is what gives
the public routes a schema for the spec.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from hailhq.api.deps import Principal, get_current_principal
from hailhq.api.ratelimit import GENERAL_RATE_LIMITED_RESPONSES
from hailhq.api.routes.internal.provider_config import (
    ActivateIn,
    ProviderConfigIn,
    ValidateIn,
    _get_row,
)
from hailhq.api.routes.internal.provider_config import (
    activate_provider_config as _activate_provider_config,
)
from hailhq.api.routes.internal.provider_config import (
    delete_provider_config as _delete_provider_config,
)
from hailhq.api.routes.internal.provider_config import (
    list_provider_configs as _list_provider_configs,
)
from hailhq.api.routes.internal.provider_config import (
    upsert_provider_config as _upsert_provider_config,
)
from hailhq.api.routes.internal.provider_config import (
    validate_provider_config as _validate_provider_config,
)
from hailhq.core.db import get_session
from hailhq.core.schemas import (
    ProviderActivateRequest,
    ProviderConfigEntry,
    ProviderConfigListResponse,
    ProviderConfigUpsert,
    ProviderValidateRequest,
    ProviderValidateResult,
)
from sqlalchemy.ext.asyncio import AsyncSession

router = APIRouter(
    prefix="/providers", tags=["providers"], responses=GENERAL_RATE_LIMITED_RESPONSES
)


@router.get(
    "",
    response_model=ProviderConfigListResponse,
    operation_id="list_providers",
)
async def list_providers(
    principal: Annotated[Principal, Depends(get_current_principal)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> dict:
    """Every saved provider row for the caller's organization, all layers."""
    return await _list_provider_configs(principal.organization_id, db)


@router.put(
    "/{layer}",
    response_model=ProviderConfigEntry,
    operation_id="upsert_provider",
)
async def upsert_provider(
    layer: str,
    body: ProviderConfigUpsert,
    principal: Annotated[Principal, Depends(get_current_principal)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> dict:
    """Save a provider for ``layer`` and make it the active one.

    A partial write: anything you omit keeps its saved value. Omitting
    ``api_key`` keeps the stored key, ``params`` keys you don't send are
    preserved, and omitting ``fallback_enabled`` leaves the flag alone
    (``false`` on a new row). The merged result — not the partial input —
    is validated against the layer's schema (422 on a mismatch). 404 on an
    unknown layer.
    """
    # Merge here, in the public router only. The internal (console) router
    # keeps full-replace semantics on purpose: the console strips empty
    # fields before it PUTs, so merging there would resurrect a value the
    # user just cleared. This route's callers are the CLI and the SDK, which
    # send a handful of flags and would otherwise wipe everything else.
    #
    # _get_row keys by (organization, layer, provider) — so `row` is the row
    # for the provider being written, and switching provider finds nothing
    # to merge. That is the behavior we want: a different provider is a
    # different config, and inheriting the old one's params would be wrong
    # (cartesia's voice_id is meaningless to elevenlabs).
    row = await _get_row(db, principal.organization_id, layer, body.provider)
    merged_params = {**row.params, **body.params} if row is not None else body.params
    if body.fallback_enabled is not None:
        fallback_enabled = body.fallback_enabled
    elif row is not None:
        fallback_enabled = row.fallback_enabled
    else:
        fallback_enabled = False

    return await _upsert_provider_config(
        principal.organization_id,
        layer,
        ProviderConfigIn(
            provider=body.provider,
            api_key=body.api_key,
            params=merged_params,
            fallback_enabled=fallback_enabled,
        ),
        db,
    )


@router.delete(
    "/{layer}/{provider}",
    status_code=204,
    operation_id="delete_provider",
)
async def delete_provider(
    layer: str,
    provider: str,
    principal: Annotated[Principal, Depends(get_current_principal)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> None:
    """Delete one provider row. Deleting the active row promotes the
    most-recently-updated sibling. Idempotent: deleting a row that isn't
    there is a 204 too. 404 on an unknown layer."""
    await _delete_provider_config(principal.organization_id, layer, provider, db)


@router.post(
    "/{layer}/activate",
    response_model=ProviderConfigEntry,
    operation_id="activate_provider",
)
async def activate_provider(
    layer: str,
    body: ProviderActivateRequest,
    principal: Annotated[Principal, Depends(get_current_principal)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> dict:
    """Switch which saved provider is active for ``layer``. 404 when that
    provider has no saved config."""
    return await _activate_provider_config(
        principal.organization_id, layer, ActivateIn(**body.model_dump()), db
    )


@router.post(
    "/{layer}/validate",
    response_model=ProviderValidateResult,
    operation_id="validate_provider",
)
async def validate_provider(
    layer: str,
    body: ProviderValidateRequest,
    principal: Annotated[Principal, Depends(get_current_principal)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> dict:
    """Probe a provider key against the real provider.

    Empty body tests the layer's active provider with its stored key; send
    ``provider`` to test a specific saved row, or ``api_key`` (plus
    ``provider``/``params``) to test a key before saving it.
    """
    return await _validate_provider_config(
        principal.organization_id, layer, ValidateIn(**body.model_dump()), db
    )
