#!/usr/bin/env bash
# Run the service: the API, the analysis worker, and the built web page.
#
#   ./run.sh                 # http://127.0.0.1:8100
#   ./run.sh --port 9000     # somewhere else
#   ./run.sh --no-worker     # leave the queue to `python -m service.app.worker`
#
# It installs the web layer into the checkout's venv if it is missing, and builds the
# front end if it has never been built, because a first run that fails on a missing
# dependency is a first run nobody finishes.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
PY="${SERVICE_PYTHON:-$ROOT/.venv/bin/python}"

if [ ! -x "$PY" ]; then
  echo "no interpreter at $PY -- set SERVICE_PYTHON, or make the checkout's venv:" >&2
  echo "  python3 -m venv .venv && .venv/bin/pip install -r analyzer/requirements.txt" >&2
  exit 1
fi

if ! "$PY" -c 'import fastapi, uvicorn, multipart' >/dev/null 2>&1; then
  echo "installing the web layer into $PY ..."
  "$PY" -m pip install -r "$HERE/requirements.txt"
fi

if [ ! -f "$HERE/frontend/dist/index.html" ]; then
  if command -v npm >/dev/null 2>&1; then
    echo "building the web page (first run) ..."
    ( cd "$HERE/frontend" && npm install --no-fund --no-audit && npm run build )
  else
    echo "note: npm is not installed, so the page has not been built." >&2
    echo "      the API works (see /docs); build the page elsewhere and copy dist/ here." >&2
  fi
fi

cd "$ROOT"                     # `service` and `analyzer` must both be importable
exec "$PY" -m service.app "$@"
