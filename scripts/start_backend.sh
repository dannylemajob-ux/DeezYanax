#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT_DIR}/backend"

HOST_VALUE="$(grep -E '^HOST=' .env 2>/dev/null | cut -d= -f2- || true)"
PORT_VALUE="$(grep -E '^PORT=' .env 2>/dev/null | cut -d= -f2- || true)"

HOST_VALUE="${HOST_VALUE:-0.0.0.0}"
PORT_VALUE="${PORT_VALUE:-8000}"

../.venv/bin/uvicorn main:app --host "${HOST_VALUE}" --port "${PORT_VALUE}"
