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
: "${SERVER_GIT_REMOTE_NAME:=origin}"
: "${GIT_REMOTE_URL:=}"
: "${GIT_BRANCH:=master}"

git push "${GIT_REMOTE_NAME}" "${GIT_BRANCH}"

ssh "${DEPLOY_HOST}" \
  "DEPLOY_DIR='${DEPLOY_DIR}' SERVER_GIT_REMOTE_NAME='${SERVER_GIT_REMOTE_NAME}' GIT_REMOTE_URL='${GIT_REMOTE_URL}' GIT_BRANCH='${GIT_BRANCH}' bash -s" <<'REMOTE'
set -euo pipefail

cd "${DEPLOY_DIR}"

if [[ "${GIT_REMOTE_URL}" == git@github.com:* || "${GIT_REMOTE_URL}" == ssh://*github.com* ]]; then
  mkdir -p "${HOME}/.ssh"
  chmod 700 "${HOME}/.ssh"
  touch "${HOME}/.ssh/known_hosts"
  chmod 600 "${HOME}/.ssh/known_hosts"
  if ! ssh-keygen -F github.com >/dev/null 2>&1; then
    ssh-keyscan -H github.com >> "${HOME}/.ssh/known_hosts"
  fi
fi

if ! git remote get-url "${SERVER_GIT_REMOTE_NAME}" >/dev/null 2>&1; then
  if [[ -z "${GIT_REMOTE_URL}" ]]; then
    echo "Remote ${SERVER_GIT_REMOTE_NAME} is missing and GIT_REMOTE_URL is not set." >&2
    exit 1
  fi
  git remote add "${SERVER_GIT_REMOTE_NAME}" "${GIT_REMOTE_URL}"
elif [[ -n "${GIT_REMOTE_URL}" ]]; then
  git remote set-url "${SERVER_GIT_REMOTE_NAME}" "${GIT_REMOTE_URL}"
fi

git fetch "${SERVER_GIT_REMOTE_NAME}" "${GIT_BRANCH}"
git pull --ff-only "${SERVER_GIT_REMOTE_NAME}" "${GIT_BRANCH}"

if [[ ! -f ".env" ]]; then
  cp ".env.example" ".env"
fi
if [[ ! -f "config/config.json" ]]; then
  cp "config/config.example.json" "config/config.json"
fi

mkdir -p "config/tls" "config/acme" "config/acme-challenge/.well-known/acme-challenge"

docker compose up -d --build --remove-orphans nginx web relay acme-renew
REMOTE
