#!/usr/bin/env bash
# Refreshes the Grafana API token and updates backend/.env automatically.
# Run this whenever you see a 401 error from Grafana (token expires every 30 min).
#
# Usage:  ./refresh_grafana_token.sh

set -euo pipefail

ENV_FILE="$(dirname "$0")/backend/.env"

# ── Load ARM credentials from .env ────────────────────────────────────────────
if [[ ! -f "$ENV_FILE" ]]; then
  echo "❌  backend/.env not found at $ENV_FILE"
  exit 1
fi

ARM_CLIENT_ID=$(grep -E "^ARM_CLIENT_ID=" "$ENV_FILE" | cut -d= -f2-)
ARM_CLIENT_SECRET=$(grep -E "^ARM_CLIENT_SECRET=" "$ENV_FILE" | cut -d= -f2-)
ARM_TENENT_ID=$(grep -E "^ARM_TENENT_ID=" "$ENV_FILE" | cut -d= -f2-)

if [[ -z "$ARM_CLIENT_ID" || -z "$ARM_CLIENT_SECRET" || -z "$ARM_TENENT_ID" ]]; then
  echo "❌  ARM_CLIENT_ID / ARM_CLIENT_SECRET / ARM_TENENT_ID not set in backend/.env"
  exit 1
fi

# ── Step 1: Azure service-principal login ─────────────────────────────────────
echo "🔐  Logging in to Azure as service principal…"
az login \
  --service-principal \
  --username "$ARM_CLIENT_ID" \
  --password "$ARM_CLIENT_SECRET" \
  --tenant "$ARM_TENENT_ID" \
  --allow-no-subscriptions \
  --output none

# ── Step 2: Get bearer token ───────────────────────────────────────────────────
echo "🎟   Fetching bearer token…"
BEARER=$(az account get-access-token --query accessToken -o tsv)

if [[ -z "$BEARER" ]]; then
  echo "❌  Failed to obtain bearer token from Azure"
  exit 1
fi

# ── Step 3: Call Pensieve to get a fresh Grafana key ──────────────────────────
echo "🔑  Requesting new Grafana token from Pensieve…"
RESPONSE=$(curl -s -X POST \
  -H "Authorization: Bearer $BEARER" \
  -H "Content-Type: application/json" \
  https://telemetry.pensieve.maersk-digital.net/api/grafana/keys)

GRAFANA_TOKEN=$(echo "$RESPONSE" | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    # Try common response shapes
    key = d.get('key') or d.get('token') or d.get('apiKey') or d.get('grafanaToken') or (d.get('data') or {}).get('key')
    if key:
        print(key)
    else:
        import sys; print('PARSE_ERROR: ' + str(d)[:200], file=sys.stderr); sys.exit(1)
except Exception as e:
    print('PARSE_ERROR: ' + str(e), file=sys.stderr)
    sys.exit(1)
" 2>/tmp/grafana_token_err)

if [[ -z "$GRAFANA_TOKEN" ]]; then
  echo "❌  Could not parse Grafana token from response."
  echo "    Raw response: $RESPONSE"
  cat /tmp/grafana_token_err 2>/dev/null || true
  exit 1
fi

# ── Step 4: Update GRAFANA_TOKEN in backend/.env ──────────────────────────────
if grep -qE "^GRAFANA_TOKEN=" "$ENV_FILE"; then
  # Replace existing line (works on both macOS and Linux)
  sed -i.bak "s|^GRAFANA_TOKEN=.*|GRAFANA_TOKEN=$GRAFANA_TOKEN|" "$ENV_FILE"
  rm -f "${ENV_FILE}.bak"
else
  echo "GRAFANA_TOKEN=$GRAFANA_TOKEN" >> "$ENV_FILE"
fi

echo "✅  GRAFANA_TOKEN updated in backend/.env"
echo "    Token: ${GRAFANA_TOKEN:0:20}…"
echo "ℹ️   Backend will pick up the new token automatically on the next poll — no restart needed."
