#!/usr/bin/env bash
set -euo pipefail

# ═══════════════════════════════════════════════════════════════
# Generate enrichment-credentials secret for any namespace
# Usage: ./generate-secrets.sh <namespace> <perplexity-api-key>
# ═══════════════════════════════════════════════════════════════

NS="${1:?Usage: generate-secrets.sh <namespace> <perplexity-api-key>}"
PPLX_KEY="${2:?Provide Perplexity API key as second argument}"
# Gate admin token for /v1/admin/register (required by Gate in staging/prod).
# Read from the environment so it is never typed into a shell history line.
GATE_ADMIN_TOKEN="${GATE_ADMIN_TOKEN:-}"
# Shared HMAC material this worker signs Gate responses with (L9_SIGNING_KEY).
L9_SIGNING_KEY="${L9_SIGNING_KEY:-}"

# Generate API credentials
API_SECRET_KEY=$(openssl rand -hex 32)
CLIENT_API_KEY=$(python3 -c "import secrets; print(secrets.token_urlsafe(48))")
API_KEY_HASH=$(echo -n "$CLIENT_API_KEY" | sha256sum | awk '{print $1}')

echo "═══════════════════════════════════════════════════════════════"
echo "  Enrichment API — Secret Generation"
echo "  Namespace: $NS"
echo "═══════════════════════════════════════════════════════════════"
echo ""
echo "📋 Client API Key (give to Salesforce/Odoo):"
echo "   $CLIENT_API_KEY"
echo ""
echo "📋 API Key Hash (stored in K8s secret):"
echo "   $API_KEY_HASH"
echo ""

kubectl create secret generic enrichment-credentials \
  --namespace="$NS" \
  --from-literal=perplexity-api-key="$PPLX_KEY" \
  --from-literal=api-secret-key="$API_SECRET_KEY" \
  --from-literal=api-key-hash="$API_KEY_HASH" \
  --from-literal=gate-admin-token="$GATE_ADMIN_TOKEN" \
  --from-literal=l9-signing-key="$L9_SIGNING_KEY" \
  --dry-run=client -o yaml | kubectl apply -f -

echo ""
echo "✅ Secret 'enrichment-credentials' created in namespace '$NS'"
echo ""
echo "╔══════════════════════════════════════════════════════════════╗"
echo "║  SAVE THIS — Client API Key (shown once):                   "
echo "║  $CLIENT_API_KEY"
echo "╚══════════════════════════════════════════════════════════════╝"
echo ""
echo "Configure in Salesforce:"
echo "  Named Credential → URL: https://your-domain/api/v1/enrich"
echo "  Custom Header: X-API-Key = $CLIENT_API_KEY"
echo ""
echo "Configure in Odoo:"
echo "  ir.config_parameter → enrichment.api_key = $CLIENT_API_KEY"
echo "  ir.config_parameter → enrichment.api_url = https://your-domain/api/v1/enrich"
