#!/usr/bin/env bash
set -euo pipefail

BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_DIR="${BASE_DIR}/config"
DB_PATH="${CONFIG_DIR}/list.db"
SCHEMA_PATH="${CONFIG_DIR}/schema.sql"

if ! command -v sqlite3 >/dev/null 2>&1; then
  echo "sqlite3 not found. Install sqlite3 and re-run." >&2
  exit 1
fi

if [[ ! -f "${SCHEMA_PATH}" ]]; then
  echo "Schema not found at ${SCHEMA_PATH}" >&2
  exit 1
fi

sqlite3 "${DB_PATH}" < "${SCHEMA_PATH}"

echo "Initialized DB at ${DB_PATH}"
