#!/usr/bin/env sh

set -eu

: "${NEWSMAIL_DOMAIN:=newsmail.spjinc.org}"

mkdir -p /acme.sh /var/www/acme "/tls/${NEWSMAIL_DOMAIN}"

while true; do
  acme.sh --home /acme.sh --server letsencrypt --cron || true
  sleep 43200
done
