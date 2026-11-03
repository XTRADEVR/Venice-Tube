#!/usr/bin/env bash
set -euo pipefail
[ -f .env ] && set -a && . ./.env && set +a
exec streamlit run app.py --server.address 0.0.0.0 --server.port "${PORT:-8501}" \
  --server.maxUploadSize 2048 --browser.gatherUsageStats false
