#!/usr/bin/env bash
set -euo pipefail

BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_DIR="${BASE_DIR}/config"
CONFIG_WEB="${CONFIG_DIR}/web.json"

if ! command -v openssl >/dev/null 2>&1; then
  echo "openssl not found. Install openssl and re-run." >&2
  exit 1
fi

if ! command -v jq >/dev/null 2>&1; then
  echo "jq not found. Install jq and re-run." >&2
  exit 1
fi

if [[ ! -f "${CONFIG_WEB}" ]]; then
  echo "Missing ${CONFIG_WEB}." >&2
  exit 1
fi

DOMAIN="$(jq -r '.domain // empty' "${CONFIG_WEB}")"
if [[ -z "${DOMAIN}" || "${DOMAIN}" == "null" ]]; then
  DOMAIN="hostrelayvec.com"
fi

TLS_DIR="${CONFIG_DIR}/tls/${DOMAIN}"
mkdir -p "${TLS_DIR}"

CERT_PATH="${TLS_DIR}/fullchain.pem"
KEY_PATH="${TLS_DIR}/privkey.pem"

openssl req \
  -x509 \
  -nodes \
  -newkey rsa:2048 \
  -days 365 \
  -keyout "${KEY_PATH}" \
  -out "${CERT_PATH}" \
  -subj "/CN=${DOMAIN}"

cat <<MSG
Fake TLS initialized.

- Cert: ${CERT_PATH}
- Key:  ${KEY_PATH}

These are self-signed and intended for development only.
MSG
