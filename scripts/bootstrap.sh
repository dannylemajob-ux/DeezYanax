#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT_DIR}"

python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/pip install -r backend/requirements.txt

mkdir -p backend/downloads

echo "Listo. Completa backend/.env y ejecuta scripts/setup_telegram_session.sh cuando tengas las credenciales."
