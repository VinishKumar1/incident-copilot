from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from jose import jwt as _jwt

from .analytics import init_analytics, record_event
from .auth import require_user
from .config import settings
from .poller import poll_loop
from .routes import analytics, chat, issues, snow
from .token_refresh import token_refresh_loop

logging.basicConfig(level=logging.INFO)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_analytics()
    poll_task = asyncio.create_task(poll_loop())
    refresh_task = asyncio.create_task(token_refresh_loop())
    try:
        yield
    finally:
        for task in (poll_task, refresh_task):
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass


app = FastAPI(title="K8s Issue Assistant", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_methods=["*"],
    allow_headers=["*", "Authorization"],
)

# Apply auth to all /api/* routes. When SSO_ENABLED=false, require_user is a no-op.
_auth = [Depends(require_user)]
app.include_router(issues.router, dependencies=_auth)
app.include_router(chat.router, dependencies=_auth)
app.include_router(analytics.router, dependencies=_auth)
app.include_router(snow.router, dependencies=_auth)


# ─── Usage tracking middleware ────────────────────────────────────────────────

_ACTION_MAP = {
    ("POST", "/api/namespace"):  "namespace_change",
    ("POST", "/api/issues"):     "analyze",       # analyze endpoint contains issue id
    ("GET",  "/api/search"):     "search",
}

def _extract_user(request: Request) -> tuple[str, str]:
    """Pull email + name from the JWT without re-validating (already done by require_user)."""
    try:
        auth = request.headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            token = auth[7:]
            claims = _jwt.get_unverified_claims(token)
            email = claims.get("email") or claims.get("preferred_username") or claims.get("upn") or "unknown"
            name  = claims.get("name") or ""
            return email, name
    except Exception:  # noqa: BLE001
        pass
    return "anonymous", ""


@app.middleware("http")
async def track_usage(request: Request, call_next):
    response = await call_next(request)

    # Only track authenticated successful API calls
    if not request.url.path.startswith("/api/") or response.status_code >= 400:
        return response

    email, name = _extract_user(request)

    path = request.url.path
    method = request.method

    # Determine action label
    if method == "POST" and "/analyze" in path:
        action, detail = "analyze", path
    elif method == "POST" and path == "/api/namespace":
        action, detail = "namespace_change", request.query_params.get("namespace", "")
    elif method == "GET" and path.startswith("/api/search"):
        action, detail = "search", request.query_params.get("key", "")
    elif method == "GET" and path == "/api/namespaces":
        action, detail = "login", ""   # first meaningful call after auth
    else:
        return response  # don't log polling noise (/api/issues, /api/status)

    await record_event(email, name, action, detail)
    return response


@app.get("/healthz")
async def healthz():
    return {"ok": True}


@app.get("/auth/config")
async def auth_config():
    """Returns SSO config the frontend needs to initialise MSAL. Public endpoint."""
    return {
        "sso_enabled": settings.sso_enabled,
        "tenant_id": settings.azure_ad_tenant_id if settings.sso_enabled else "",
        "client_id": settings.azure_ad_client_id if settings.sso_enabled else "",
    }
