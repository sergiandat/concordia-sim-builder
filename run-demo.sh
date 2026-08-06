#!/usr/bin/env bash
# Start the demo inside a Codespace and expose it on a public URL.
set -euo pipefail

PORT="${PORT:-8000}"

if [ -z "${GEMINI_API_KEY:-}" ]; then
  echo "WARNING: GEMINI_API_KEY is not set — simulations will fail at the first"
  echo "LLM call. Add it as a Codespaces secret (see DEPLOY.md) and rebuild, or"
  echo "export it here for a one-off run."
  echo
fi

if [ ! -d frontend/dist ]; then
  echo "frontend/dist missing — building it now"
  (cd frontend && npm run build)
fi

# Codespaces forwards ports privately by default; make it public so teammates
# can open the URL without a GitHub account.
if [ -n "${CODESPACE_NAME:-}" ]; then
  echo "==> Making port ${PORT} public"
  gh codespace ports visibility "${PORT}:public" -c "${CODESPACE_NAME}" \
    || echo "    (could not set visibility automatically — do it from the PORTS tab)"
  echo "==> URL: https://${CODESPACE_NAME}-${PORT}.app.github.dev"
  echo
fi

exec uvicorn backend.main:app --host 0.0.0.0 --port "${PORT}"
