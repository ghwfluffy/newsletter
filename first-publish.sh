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
: "${GIT_REMOTE_URL:?GIT_REMOTE_URL is required}"
: "${GIT_BRANCH:=master}"

git push "${GIT_REMOTE_NAME}" "${GIT_BRANCH}"

ssh "${DEPLOY_HOST}" \
  "DEPLOY_DIR='${DEPLOY_DIR}' SERVER_GIT_REMOTE_NAME='${SERVER_GIT_REMOTE_NAME}' GIT_REMOTE_URL='${GIT_REMOTE_URL}' GIT_BRANCH='${GIT_BRANCH}' bash -s" <<'REMOTE'
set -euo pipefail

BACKUP_DIR="$(mktemp -d)"
restore_backup() {
  if [[ -d "${BACKUP_DIR}/config" ]]; then
    mkdir -p "${DEPLOY_DIR}/config"
    cp -a "${BACKUP_DIR}/config/." "${DEPLOY_DIR}/config/"
  fi
  if [[ -f "${BACKUP_DIR}/.env" ]]; then
    cp "${BACKUP_DIR}/.env" "${DEPLOY_DIR}/.env"
  fi
}
trap 'rm -rf "${BACKUP_DIR}"' EXIT

if [[ -d "${DEPLOY_DIR}" ]]; then
  mkdir -p "${BACKUP_DIR}/config"
  for path in \
    "config/config.json" \
    "config/list.db" \
    "config/test.db" \
    "config/tls" \
    "config/acme" \
    "config/acme-challenge"; do
    if [[ -e "${DEPLOY_DIR}/${path}" ]]; then
      mkdir -p "${BACKUP_DIR}/$(dirname "${path}")"
      cp -a "${DEPLOY_DIR}/${path}" "${BACKUP_DIR}/${path}"
    fi
  done
  if [[ -f "${DEPLOY_DIR}/.env" ]]; then
    cp "${DEPLOY_DIR}/.env" "${BACKUP_DIR}/.env"
  fi
fi

mkdir -p "$(dirname "${DEPLOY_DIR}")"

if [[ "${GIT_REMOTE_URL}" == *github.com* ]]; then
  mkdir -p "${HOME}/.ssh"
  chmod 700 "${HOME}/.ssh"
  touch "${HOME}/.ssh/known_hosts"
  chmod 600 "${HOME}/.ssh/known_hosts"
  if ! ssh-keygen -F github.com >/dev/null 2>&1; then
    ssh-keyscan -H github.com >> "${HOME}/.ssh/known_hosts"
  fi
fi

if [[ ! -d "${DEPLOY_DIR}/.git" ]]; then
  rm -rf "${DEPLOY_DIR}"
  git clone --branch "${GIT_BRANCH}" "${GIT_REMOTE_URL}" "${DEPLOY_DIR}"
  cd "${DEPLOY_DIR}"
  if [[ "${SERVER_GIT_REMOTE_NAME}" != "origin" ]]; then
    git remote rename origin "${SERVER_GIT_REMOTE_NAME}"
  fi
else
  cd "${DEPLOY_DIR}"
  git remote remove "${SERVER_GIT_REMOTE_NAME}" >/dev/null 2>&1 || true
  git remote add "${SERVER_GIT_REMOTE_NAME}" "${GIT_REMOTE_URL}"
  git fetch "${SERVER_GIT_REMOTE_NAME}" "${GIT_BRANCH}"
  git reset --hard "${SERVER_GIT_REMOTE_NAME}/${GIT_BRANCH}"
  git clean -fdx
fi

restore_backup

cd "${DEPLOY_DIR}"

if [[ ! -f ".env" ]]; then
  cp ".env.example" ".env"
fi
if [[ ! -f "config/config.json" ]]; then
  cp "config/config.example.json" "config/config.json"
fi

mkdir -p "config/tls" "config/acme" "config/acme-challenge/.well-known/acme-challenge"

if [[ ! -f "config/list.db" ]]; then
  docker run --rm \
    -v "${DEPLOY_DIR}:/work" \
    -w /work \
    alpine:3.20 \
    sh -c "apk add --no-cache sqlite >/dev/null && sqlite3 config/list.db < config/schema.sql"
fi
REMOTE

"${BASE_DIR}/publish.sh"
