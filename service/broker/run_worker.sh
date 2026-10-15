#!/usr/bin/env bash
#
# Start the LAN worker that runs `note_seq_align` jobs from the broker.
#
#   BROKER_URL=https://example.com/api/v1 WORKER_KEY=... ./service/broker/run_worker.sh
#   ... --once              # claim one job, run it, exit -- the smoke test
#   ... --list-services     # what a fresh worker would load; no broker call
#
# Two things this script exists to get right:
#
#   * **The interpreter.** Their `worker.py` is deliberately standard-library only, but
#     this plugin is not: it imports `service.app.pipeline`, which imports the analyzer.
#     So the worker has to run under this repository's venv, not the broker's python.
#   * **The plugin directory.** Their loader imports a plugin by its directory name, so
#     `--plugin-dir` is the directory *containing* `note_seq_align/`, which is this one.
#
# The worker publishes its contract to the broker at startup (`POST /worker/services`),
# and that is the whole registration: after it, the service appears in their submit form,
# built from `param_schema`. Changing the plugin therefore needs this restarted -- their
# loader imports it once, and the form is built from what was published then.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"        # service/broker
REPO="$(cd "$HERE/../.." && pwd)"                           # the note-seq checkout

PYTHON="${PYTHON:-$REPO/.venv/bin/python}"
VIEWER="${NOTE_SEQ_VIEWER_URL:-http://localhost:8100}"
NAME="${WORKER_NAME:-lan-note-seq}"

# Where the broker's own `worker.py` lives. Set WORKER_PY to override; otherwise the
# two layouts this has been run from are tried, and the failure says what to do.
if [ -z "${WORKER_PY:-}" ]; then
  for candidate in \
      "$REPO/../selfCommentary/full-stack-fastapi-template/worker.py" \
      "$HOME/playground/goGames/selfCommentary/full-stack-fastapi-template/worker.py" \
      "$HOME/full-stack-fastapi-template/worker.py"; do
    [ -f "$candidate" ] && WORKER_PY="$candidate" && break
  done
fi
if [ -z "${WORKER_PY:-}" ] || [ ! -f "$WORKER_PY" ]; then
  cat >&2 <<'EOF'
the broker's worker.py was not found. Point WORKER_PY at it:

  WORKER_PY=/path/to/full-stack-fastapi-template/worker.py ./service/broker/run_worker.sh
EOF
  exit 2
fi

if [ ! -x "$PYTHON" ]; then
  echo "no interpreter at $PYTHON -- set PYTHON, or create the venv (see README)" >&2
  exit 2
fi

printf '==> %s\n' "note_seq_align worker"
printf '    python     %s\n' "$PYTHON"
printf '    worker     %s\n' "$WORKER_PY"
printf '    plugin dir %s\n' "$HERE"
printf '    broker     %s\n' "${BROKER_URL:-<not set: worker.py defaults to http://127.0.0.1:8000/api/v1>}"
printf '    name       %s\n' "$NAME"
printf '    key        %s\n' "$([ -n "${WORKER_KEY:-}" ] && echo 'set' || echo '<not set>')"
printf '    page link  %s\n' "$VIEWER"
# Where a finished piece is *sent*: the same page as the link, unless it is overridden.
# Printing which of the two applies is what makes the failure visible in advance -- the one
# where the link names a deployment and the piece stays on this machine.
if [ -n "${SERVICE_PUBLISH_URL:-}" ]; then
  printf '    publishing %s\n' "$SERVICE_PUBLISH_URL"
elif [ "$VIEWER" != 'http://localhost:8100' ]; then
  printf '    publishing %s (following the page link)\n' "$VIEWER"
else
  printf '    publishing %s\n' '<nowhere: set SERVICE_PUBLISH_URL or NOTE_SEQ_VIEWER_URL>'
fi
printf '    token      %s\n' "$([ -n "${SERVICE_PUBLISH_TOKEN:-}${SERVICE_UPLOAD_TOKEN:-}" ] \
  && echo 'set' || echo '<not set>')"
echo

# BROKER_URL and WORKER_KEY are read by worker.py from the environment, so they can be
# given either way; --name and the plugin directory are set here rather than repeated.
exec env NOTE_SEQ_VIEWER_URL="$VIEWER" \
  "$PYTHON" "$WORKER_PY" \
  --plugin-dir "$HERE" \
  --service note_seq_align \
  --name "$NAME" \
  "$@"
