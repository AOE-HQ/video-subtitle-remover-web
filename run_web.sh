#!/usr/bin/env bash
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ -f "${APP_DIR}/.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "${APP_DIR}/.env"
  set +a
fi

if [[ -n "${VSR_PYTHON:-}" ]]; then
  PYTHON_BIN="${VSR_PYTHON}"
elif [[ -x "${APP_DIR}/.venv/bin/python" ]]; then
  PYTHON_BIN="${APP_DIR}/.venv/bin/python"
else
  PYTHON_BIN="python3"
fi

exec "${PYTHON_BIN}" -m uvicorn web_app:app \
  --app-dir "${APP_DIR}" \
  --host "${WEB_HOST:-0.0.0.0}" \
  --port "${WEB_PORT:-8000}" \
  --workers 1
