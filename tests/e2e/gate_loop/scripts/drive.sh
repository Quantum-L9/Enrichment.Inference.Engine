#!/usr/bin/env bash
# Run one driver scenario inside the Gate image on the control network.
#   bash drive.sh <scenario> ['<json args>']
set -Eeuo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${GL_ENV_FILE:-${GL_STATE_DIR:-/root/l9e2e/gate_loop}/run.env}"
# shellcheck source=/dev/null
set -a; . "$ENV_FILE"; set +a
docker run --rm --network l9e2e_control \
  -v "$(dirname "$HERE")/driver/gate_loop_driver.py:/driver.py:ro" \
  -e GATE_URL=http://gate:9000 -e HTTPS_PROXY= -e https_proxy= \
  -e GL_SIGNING="${GL_SIGNING:-ed25519}" -e GL_HMAC_ALL_JSON \
  -e GL_ALL_PUBLIC_KEYS_JSON -e GL_PRIV_ODOO -e GL_PRIV_OP -e GL_PRIV_ROGUE -e GL_PRIV_EIE -e GL_PRIV_CEG \
  --entrypoint python l9e2e/gate:local /driver.py "$1" "${2:-{\}}"
