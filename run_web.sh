#!/usr/bin/env bash
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_BIN="${VSR_PYTHON:-python3}"
exec "${PYTHON_BIN}" -m uvicorn web_app:app \
  --app-dir "${APP_DIR}" \
  --host "${WEB_HOST:-0.0.0.0}" \
  --port "${WEB_PORT:-8000}" \
  --workers 1
