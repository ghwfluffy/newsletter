#!/usr/bin/env bash
set -euo pipefail

BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_DIR="${BASE_DIR}/config"
CONFIG_PATH="${CONFIG_DIR}/config.json"
SCHEMA_PATH="${CONFIG_DIR}/schema.sql"

if ! command -v sqlite3 >/dev/null 2>&1; then
  echo "sqlite3 not found. Install sqlite3 and re-run." >&2
  exit 1
fi

if ! command -v jq >/dev/null 2>&1; then
  echo "jq not found. Install jq and re-run." >&2
  exit 1
fi

if [[ ! -f "${SCHEMA_PATH}" ]]; then
  echo "Schema not found at ${SCHEMA_PATH}" >&2
  exit 1
fi

if [[ ! -f "${CONFIG_PATH}" ]]; then
  echo "Config not found at ${CONFIG_PATH}" >&2
  exit 1
fi

DB_PATH="$(jq -r '.db.db_path // empty' "${CONFIG_PATH}")"
if [[ -z "${DB_PATH}" || "${DB_PATH}" == "null" ]]; then
  DB_PATH="${CONFIG_DIR}/list.db"
fi
DB_PATH="${DB_PATH//\$\{config\}/${CONFIG_DIR}}"

sqlite3 "${DB_PATH}" < "${SCHEMA_PATH}"

echo "Initialized DB at ${DB_PATH}"
