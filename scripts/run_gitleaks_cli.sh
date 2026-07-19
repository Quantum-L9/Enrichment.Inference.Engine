#!/usr/bin/env bash
# --- L9_META ---
# l9_schema: 1
# origin: l9-implementer
# engine: enrichment
# layer: [ci, security]
# tags: [gitleaks, secrets, soc2-cc6.1]
# owner: platform
# status: active
# --- /L9_META ---
#
# Install and run the open-source gitleaks CLI (no Gitleaks-Action org license).
# Used by gitleaks.yml, ci.yml, and pr-pipeline.yml so secret scanning stays a
# hard gate without depending on secrets.GITLEAKS_LICENSE.

set -euo pipefail

GITLEAKS_VERSION="${GITLEAKS_VERSION:-8.30.1}"
GITLEAKS_CONFIG="${GITLEAKS_CONFIG:-.gitleaks.toml}"
INSTALL_DIR="${RUNNER_TEMP:-/tmp}/gitleaks-${GITLEAKS_VERSION}"
ARCHIVE="gitleaks_${GITLEAKS_VERSION}_linux_x64.tar.gz"
URL="https://github.com/gitleaks/gitleaks/releases/download/v${GITLEAKS_VERSION}/${ARCHIVE}"

mkdir -p "${INSTALL_DIR}"
if [[ ! -x "${INSTALL_DIR}/gitleaks" ]]; then
  echo "Downloading gitleaks v${GITLEAKS_VERSION}..."
  curl -sSfL "${URL}" -o "${INSTALL_DIR}/${ARCHIVE}"
  tar -xzf "${INSTALL_DIR}/${ARCHIVE}" -C "${INSTALL_DIR}" gitleaks
fi

echo "Running: gitleaks detect --source . --config ${GITLEAKS_CONFIG} --redact --verbose"
exec "${INSTALL_DIR}/gitleaks" detect \
  --source . \
  --config "${GITLEAKS_CONFIG}" \
  --redact \
  --verbose \
  --exit-code 1
