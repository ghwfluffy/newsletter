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
: "${GIT_REMOTE_URL:=}"
: "${GIT_BRANCH:=master}"

git push "${GIT_REMOTE_NAME}" "${GIT_BRANCH}"

ssh "${DEPLOY_HOST}" \
  "DEPLOY_DIR='${DEPLOY_DIR}' GIT_REMOTE_NAME='${GIT_REMOTE_NAME}' GIT_REMOTE_URL='${GIT_REMOTE_URL}' GIT_BRANCH='${GIT_BRANCH}' bash -s" <<'REMOTE'
set -euo pipefail

cd "${DEPLOY_DIR}"

if ! git remote get-url "${GIT_REMOTE_NAME}" >/dev/null 2>&1; then
  if [[ -z "${GIT_REMOTE_URL}" ]]; then
    echo "Remote ${GIT_REMOTE_NAME} is missing and GIT_REMOTE_URL is not set." >&2
    exit 1
  fi
  git remote add "${GIT_REMOTE_NAME}" "${GIT_REMOTE_URL}"
elif [[ -n "${GIT_REMOTE_URL}" ]]; then
  git remote set-url "${GIT_REMOTE_NAME}" "${GIT_REMOTE_URL}"
fi

git fetch "${GIT_REMOTE_NAME}" "${GIT_BRANCH}"
git pull --ff-only "${GIT_REMOTE_NAME}" "${GIT_BRANCH}"

if [[ ! -f ".env" ]]; then
  cp ".env.example" ".env"
fi
if [[ ! -f "config/config.json" ]]; then
  cp "config/config.example.json" "config/config.json"
fi

mkdir -p "config/tls" "config/acme" "config/acme-challenge/.well-known/acme-challenge"

docker compose up -d --build --remove-orphans nginx web relay acme-renew
REMOTE
