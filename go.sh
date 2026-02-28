#!/usr/bin/env bash

set -euo pipefail

echo "Initialize sudo:"
sudo echo "" &> /dev/null

BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="${BASE_DIR}/venv"

if [[ ! -d "${VENV_DIR}" ]]; then
  python3 -m venv "${VENV_DIR}"
fi

# shellcheck disable=SC1091
source "${VENV_DIR}/bin/activate"

# Ensure deps are installed if requirements.txt exists
if [[ -f "${BASE_DIR}/requirements.txt" ]]; then
  pip install -r "${BASE_DIR}/requirements.txt" >/dev/null
fi

stop_all() {
  echo "Stopping watchdogs..." >&2
  if [[ -n "${RELAY_PID:-}" ]]; then
    kill "${RELAY_PID}" 2>/dev/null || true
  fi
  if [[ -n "${WEB_PID:-}" ]]; then
    kill "${WEB_PID}" 2>/dev/null || true
  fi
  if [[ -n "${TLS_PID:-}" ]]; then
    kill "${TLS_PID}" 2>/dev/null || true
  fi
  wait 2>/dev/null || true
}

trap stop_all INT TERM

watch_relay() {
  while true; do
    python3 "${BASE_DIR}/src/replay-daemon.py" || true
    sleep 1
  done
}

watch_web() {
  # Run the webserver as root for 443 binding
  sudo -E bash -c "
    set -euo pipefail
    child_pid=''
    stop_child() {
      if [[ -n \"\${child_pid}\" ]]; then
        kill \"\${child_pid}\" 2>/dev/null || true
      fi
      exit 0
    }
    trap stop_child INT TERM
    while true; do
      source '${VENV_DIR}/bin/activate'
      python3 '${BASE_DIR}/src/webserver.py' &
      child_pid=\$!
      wait \"\${child_pid}\" || true
      sleep 1
    done
  "
}

watch_tls() {
  local acme_home="${BASE_DIR}/config/acme"
  local acme_sh="${acme_home}/acme.sh"
  if [[ ! -x "${acme_sh}" ]]; then
    echo "acme.sh not found at ${acme_sh}. Run ./init-tls.sh first." >&2
    return 0
  fi
  sudo -E bash -c "while true; do '${acme_sh}' --cron --home '${acme_home}' >/dev/null 2>&1 || true; sleep 12h; done"
}

watch_relay &
RELAY_PID=$!

watch_web &
WEB_PID=$!

watch_tls &
TLS_PID=$!

# Block until terminated
wait
