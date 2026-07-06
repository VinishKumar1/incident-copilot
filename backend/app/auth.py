from __future__ import annotations

import logging
import time
from typing import Optional

import httpx
from fastapi import Depends, HTTPException, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from jose import JWTError, jwt

from .config import settings

log = logging.getLogger("auth")

_bearer = HTTPBearer(auto_error=False)

# JWKS cache: (keys_list, fetched_at)
_jwks_cache: tuple[list, float] = ([], 0.0)
_JWKS_TTL = 3600  # refresh public keys every hour


async def _get_jwks() -> list:
    global _jwks_cache
    keys, fetched_at = _jwks_cache
    if keys and (time.time() - fetched_at) < _JWKS_TTL:
        return keys

    url = (
        f"https://login.microsoftonline.com/"
        f"{settings.azure_ad_tenant_id}/discovery/v2.0/keys"
    )
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        data = resp.json()

    keys = data.get("keys", [])
    _jwks_cache = (keys, time.time())
    return keys


async def _validate_token(token: str) -> dict:
    """Validate an Azure AD JWT and return the claims."""
    keys = await _get_jwks()

    # Attempt validation against each key (Azure rotates keys).
    last_err: Optional[Exception] = None
    for key in keys:
        try:
            claims = jwt.decode(
                token,
                key,
                algorithms=["RS256"],
                audience=settings.azure_ad_client_id,
                issuer=(
                    f"https://login.microsoftonline.com/"
                    f"{settings.azure_ad_tenant_id}/v2.0"
                ),
                options={"verify_at_hash": False},
            )
            return claims
        except JWTError as exc:
            last_err = exc
            continue

    raise HTTPException(
        status_code=401,
        detail=f"Invalid or expired token: {last_err}",
    )


async def require_user(
    creds: Optional[HTTPAuthorizationCredentials] = Security(_bearer),
) -> dict:
    """FastAPI dependency — validates the Bearer token when SSO is enabled.

    When SSO_ENABLED=false, returns an empty dict so the app runs without auth
    (useful for local dev without an Azure AD registration).
    """
    if not settings.sso_enabled:
        return {}

    if creds is None or not creds.credentials:
        raise HTTPException(status_code=401, detail="Authorization header required")

    return await _validate_token(creds.credentials)


async def require_admin(
    claims: dict = Depends(require_user),
) -> dict:
    """FastAPI dependency — additionally requires the TFR_Admin app role."""
    if not settings.sso_enabled:
        return claims  # dev mode: skip role check

    roles = claims.get("roles", [])
    if "TFR_Admin" not in roles:
        raise HTTPException(status_code=403, detail="TFR_Admin role required")
    return claims
