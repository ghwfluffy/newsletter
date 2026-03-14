#!/usr/bin/env bash

set -euo pipefail

BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_DIR="${BASE_DIR}/config"
ACME_HOME="${CONFIG_DIR}/acme"
ACME_SH="${ACME_HOME}/acme.sh"
CONFIG_PATH="${CONFIG_DIR}/config.json"

if ! command -v curl >/dev/null 2>&1; then
  echo "curl not found. Install curl and re-run." >&2
  exit 1
fi

if ! command -v jq >/dev/null 2>&1; then
  echo "jq not found. Install jq and re-run." >&2
  exit 1
fi

if [[ ! -f "${CONFIG_PATH}" ]]; then
  echo "Missing ${CONFIG_PATH}." >&2
  exit 1
fi

DOMAIN="$(jq -r '.web.domain // empty' "${CONFIG_PATH}")"
if [[ -z "${DOMAIN}" || "${DOMAIN}" == "null" ]]; then
  DOMAIN="hostrelayvec.com"
fi
TLS_DIR="${CONFIG_DIR}/tls/${DOMAIN}"

read -r -p "Contact email for Let's Encrypt (recommended): " ACME_EMAIL

mkdir -p "${ACME_HOME}" "${TLS_DIR}"

if [[ ! -x "${ACME_SH}" ]]; then
  curl -fsSL https://get.acme.sh | sh -s email="${ACME_EMAIL}" --home "${ACME_HOME}"
fi
if [[ ! -x "${ACME_SH}" ]]; then
  echo "acme.sh install failed. Expected ${ACME_SH}" >&2
  exit 1
fi

sudo -E "${ACME_SH}" \
  --issue \
  --standalone \
  -d "${DOMAIN}" \
  --keylength ec-256 \
  --home "${ACME_HOME}"

sudo -E "${ACME_SH}" \
  --install-cert \
  -d "${DOMAIN}" \
  --home "${ACME_HOME}" \
  --ecc \
  --fullchain-file "${TLS_DIR}/fullchain.pem" \
  --key-file "${TLS_DIR}/privkey.pem"

TMP_FILE="$(mktemp)"
jq \
  --arg domain "${DOMAIN}" \
  --arg cert '${config}/tls/${domain}/fullchain.pem' \
  --arg key '${config}/tls/${domain}/privkey.pem' \
  '.web.domain=$domain | .web.tls_cert=$cert | .web.tls_key=$key' \
  "${CONFIG_PATH}" > "${TMP_FILE}"

mv "${TMP_FILE}" "${CONFIG_PATH}"

cat <<MSG
TLS initialized.

- Cert: ${TLS_DIR}/fullchain.pem
- Key:  ${TLS_DIR}/privkey.pem

Ensure port 80 is reachable for ACME HTTP-01 challenges.
MSG
