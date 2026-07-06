from __future__ import annotations

import asyncio
import logging
import os
import subprocess
from pathlib import Path

import httpx

log = logging.getLogger("token_refresh")

_ENV_FILE = Path(__file__).parent.parent / ".env"

# In-memory cache of the live token — updated by the refresh loop.
# loki._grafana_token() checks this first so no file I/O is needed per-request.
_live_token: str = ""

# Full path to az CLI — required when running as a launchd service (no shell PATH).
_AZ = os.environ.get("AZ_PATH", "/opt/homebrew/bin/az")


def get_live_token() -> str:
    return _live_token


def _read_env_var(name: str) -> str:
    try:
        for line in _ENV_FILE.read_text().splitlines():
            if line.startswith(f"{name}="):
                return line.split("=", 1)[1].strip()
    except Exception:
        pass
    return ""


def _update_env_token(token: str) -> None:
    try:
        text = _ENV_FILE.read_text()
        lines = text.splitlines()
        new_lines = []
        replaced = False
        for line in lines:
            if line.startswith("GRAFANA_TOKEN="):
                new_lines.append(f"GRAFANA_TOKEN={token}")
                replaced = True
            else:
                new_lines.append(line)
        if not replaced:
            new_lines.append(f"GRAFANA_TOKEN={token}")
        _ENV_FILE.write_text("\n".join(new_lines) + "\n")
    except Exception as exc:
        log.warning("Could not update .env: %s", exc)


async def _refresh_once() -> bool:
    """Run the full Azure → Pensieve → Grafana token flow.
    Returns True on success, False on failure."""
    client_id = _read_env_var("ARM_CLIENT_ID")
    client_secret = _read_env_var("ARM_CLIENT_SECRET")
    tenant_id = _read_env_var("ARM_TENENT_ID")

    if not all([client_id, client_secret, tenant_id]):
        log.warning("ARM credentials not set in .env — skipping token refresh")
        return False

    try:
        # Step 1: az login
        log.info("Refreshing Grafana token via Azure service principal…")
        proc = await asyncio.create_subprocess_exec(
            _AZ, "login",
            "--service-principal",
            "--username", client_id,
            "--password", client_secret,
            "--tenant", tenant_id,
            "--allow-no-subscriptions",
            "--output", "none",
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        await proc.wait()
        if proc.returncode != 0:
            log.error("az login failed (exit %s)", proc.returncode)
            return False

        # Step 2: get bearer token
        proc2 = await asyncio.create_subprocess_exec(
            _AZ, "account", "get-access-token", "--query", "accessToken", "-o", "tsv",
            stdout=asyncio.subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        stdout, _ = await proc2.communicate()
        bearer = stdout.decode().strip()
        if not bearer:
            log.error("Failed to obtain bearer token from Azure")
            return False

        # Step 3: call Pensieve for a fresh Grafana key
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                "https://telemetry.pensieve.maersk-digital.net/api/grafana/keys",
                headers={"Authorization": f"Bearer {bearer}"},
            )
            resp.raise_for_status()
            data = resp.json()

        token = (
            data.get("key")
            or data.get("token")
            or data.get("apiKey")
            or data.get("grafanaToken")
            or (data.get("data") or {}).get("key")
            or ""
        )
        if not token:
            log.error("Could not parse Grafana token from Pensieve response: %s", data)
            return False

        global _live_token
        _live_token = token
        _update_env_token(token)
        log.info("Grafana token refreshed successfully ✅")
        return True

    except Exception as exc:
        log.error("Token refresh failed: %s", exc)
        return False


async def token_refresh_loop() -> None:
    """Background task: refresh the Grafana token on startup then every 25 minutes.

    Refreshing immediately on startup guarantees the backend always has a valid
    token regardless of how old the token in .env is.
    """
    INTERVAL = 25 * 60  # 25 minutes — well inside the 30-min expiry window

    # Always do an immediate refresh so we start with a guaranteed-fresh token.
    log.info("Performing startup Grafana token refresh…")
    success = await _refresh_once()
    if not success:
        # Fall back to whatever is in .env so we're not left with an empty token.
        global _live_token
        existing = _read_env_var("GRAFANA_TOKEN")
        if existing:
            _live_token = existing
            log.warning("Startup refresh failed — loaded existing token from .env as fallback")

    while True:
        await asyncio.sleep(INTERVAL)
        await _refresh_once()
