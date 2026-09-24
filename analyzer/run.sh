#!/usr/bin/env bash
# Launcher for the analyzer: a Qt5 tool that lines a score's measures up with a
# recording of that piece, so you can play any range of bars.
#
#   ./run.sh check          what is installed, and which pieces were found
#   ./run.sh run            open the window (default)
#   ./run.sh run --piece chopin-waltz-in-a-minor-op-34-no-2
#   ./run.sh score FILE     open a specific MusicXML/MXL (no question bank needed)
#   ./run.sh serve          the practice client for a phone/tablet browser
#                           (audio comes from the local cache, never from YouTube)
#   ./run.sh test           run the analyzer's test suite
#
# Environment overrides:
#   ANALYZER_CACHE=/tmp/a   where recordings are cached
#   ANALYZER_OUTPUT=/tmp/o  where measure maps are written
#   ANALYZER_PORT=8090      port for ./run.sh serve
#   ANALYZER_BACKEND=filesystem  which store implementation to use
#   ANALYZER_TTS_BASE_URL=http://192.168.2.174:8880/v1   speech for card questions
#   ANALYZER_TTS_VOICE=af_heart                          voice for those questions
set -euo pipefail
cd "$(dirname "$0")"

PY="../.venv/bin/python"
if [[ ! -x "$PY" ]]; then
  echo "no virtualenv at $(cd .. && pwd)/.venv" >&2
  echo "create it with:  python3 -m venv .venv && .venv/bin/pip install ." >&2
  exit 2
fi

mode="${1:-run}"
shift || true

case "$mode" in
  check)
    exec "$PY" -m analyzer.app --check "$@"
    ;;
  run)
    exec "$PY" -m analyzer.app "$@"
    ;;
  score)
    if [[ $# -lt 1 ]]; then
      echo "usage: ./run.sh score <file.musicxml|file.mxl>" >&2
      exit 2
    fi
    exec "$PY" -m analyzer.app --score "$1" "${@:2}"
    ;;
  serve)
    exec "$PY" -u -m analyzer.app --serve --host "${ANALYZER_HOST:-0.0.0.0}" \
         --port "${ANALYZER_PORT:-8090}" "$@"
    ;;
  test)
    exec "$PY" -m pytest tests -q "$@"
    ;;
  *)
    echo "usage: run.sh [check|run|score FILE|serve|test] [args...]" >&2
    exit 2
    ;;
esac
