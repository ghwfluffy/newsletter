#!/usr/bin/env sh

set -eu

: "${NEWSMAIL_DOMAIN:=newsmail.spjinc.org}"
: "${ACME_EMAIL:=}"

ACME_HOME="/acme.sh"
WEBROOT="/var/www/acme"
TLS_DIR="/tls/${NEWSMAIL_DOMAIN}"

mkdir -p "${ACME_HOME}" "${WEBROOT}" "${TLS_DIR}"

if [ -n "${ACME_EMAIL}" ]; then
  acme.sh --home "${ACME_HOME}" --server letsencrypt --register-account -m "${ACME_EMAIL}"
fi

acme.sh \
  --home "${ACME_HOME}" \
  --server letsencrypt \
  --issue \
  --webroot "${WEBROOT}" \
  -d "${NEWSMAIL_DOMAIN}" \
  --keylength ec-256

acme.sh \
  --home "${ACME_HOME}" \
  --server letsencrypt \
  --install-cert \
  --ecc \
  -d "${NEWSMAIL_DOMAIN}" \
  --fullchain-file "${TLS_DIR}/fullchain.pem" \
  --key-file "${TLS_DIR}/privkey.pem"
