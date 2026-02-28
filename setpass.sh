#!/usr/bin/env bash
set -euo pipefail

BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_PATH="${BASE_DIR}/config/web.json"

if ! command -v jq >/dev/null 2>&1; then
  echo "jq not found. Install jq and re-run." >&2
  exit 1
fi

if [[ ! -f "${CONFIG_PATH}" ]]; then
  echo "Config not found at ${CONFIG_PATH}" >&2
  exit 1
fi

read -r -p "Admin username [admin]: " ADMIN_USER
if [[ -z "${ADMIN_USER}" ]]; then
  ADMIN_USER="admin"
fi

read -r -s -p "Password: " ADMIN_PASS
echo
read -r -s -p "Confirm: " ADMIN_PASS_CONFIRM
echo

if [[ "${ADMIN_PASS}" != "${ADMIN_PASS_CONFIRM}" ]]; then
  echo "Passwords do not match." >&2
  exit 1
fi

ADMIN_HASH=$(ADMIN_PASS="${ADMIN_PASS}" python3 - <<'PY'
import os
import bcrypt
pw = os.environ["ADMIN_PASS"].encode("utf-8")
print(bcrypt.hashpw(pw, bcrypt.gensalt()).decode("utf-8"))
PY
)

TMP_FILE="$(mktemp)"
jq --arg user "${ADMIN_USER}" --arg hash "${ADMIN_HASH}" \
  '.admin_user=$user | .admin_pass_bcrypt=$hash' \
  "${CONFIG_PATH}" > "${TMP_FILE}"

mv "${TMP_FILE}" "${CONFIG_PATH}"

echo "Updated admin credentials in ${CONFIG_PATH}"
