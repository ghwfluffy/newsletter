#!/usr/bin/env bash

set -euo pipefail

BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${BASE_DIR}"

if [[ ! -f ".env" ]]; then
  cp ".env.example" ".env"
  echo "Created .env from .env.example. Review it if these defaults are not correct." >&2
fi

set -a
# shellcheck disable=SC1091
source ".env"
set +a

: "${DEPLOY_HOST:?DEPLOY_HOST is required}"
: "${DEPLOY_DIR:?DEPLOY_DIR is required}"
: "${GIT_REMOTE_NAME:=github}"
: "${GIT_BRANCH:=master}"

git push "${GIT_REMOTE_NAME}" "${GIT_BRANCH}"

ssh "${DEPLOY_HOST}" \
  "DEPLOY_DIR='${DEPLOY_DIR}' GIT_REMOTE_NAME='${GIT_REMOTE_NAME}' GIT_BRANCH='${GIT_BRANCH}' bash -s" <<'REMOTE'
set -euo pipefail

cd "${DEPLOY_DIR}"

git fetch "${GIT_REMOTE_NAME}" "${GIT_BRANCH}"
git pull --ff-only "${GIT_REMOTE_NAME}" "${GIT_BRANCH}"

if [[ ! -f ".env" ]]; then
  cp ".env.example" ".env"
fi
if [[ ! -f "config/config.json" ]]; then
  cp "config/config.example.json" "config/config.json"
fi

mkdir -p "config/tls" "config/acme" "config/acme-challenge/.well-known/acme-challenge"

docker compose up -d --build --remove-orphans nginx web acme-renew
REMOTE
