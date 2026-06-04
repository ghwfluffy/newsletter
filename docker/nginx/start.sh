#!/usr/bin/env sh

set -eu

DOMAIN="newsmail.spjinc.org"
CERT="/etc/nginx/tls/${DOMAIN}/fullchain.pem"
KEY="/etc/nginx/tls/${DOMAIN}/privkey.pem"
CONF="/etc/nginx/conf.d/newsletter.conf"
TEMPLATE_DIR="/etc/nginx/newsletter/templates"

install_config() {
  rm -f /etc/nginx/conf.d/default.conf
  if [ -s "${CERT}" ] && [ -s "${KEY}" ]; then
    cp "${TEMPLATE_DIR}/newsletter-https.conf" "${CONF}"
  else
    cp "${TEMPLATE_DIR}/newsletter-http-only.conf" "${CONF}"
  fi
}

install_config

(
  while true; do
    sleep 30
    if [ -s "${CERT}" ] && [ -s "${KEY}" ]; then
      cp "${TEMPLATE_DIR}/newsletter-https.conf" "${CONF}"
      nginx -s reload || true
      break
    fi
  done
) &

(
  while true; do
    sleep 21600
    install_config
    nginx -s reload || true
  done
) &

nginx -g "daemon off;"
