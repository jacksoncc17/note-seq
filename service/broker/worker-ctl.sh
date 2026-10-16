#!/usr/bin/env bash
#
# Start, stop and watch the note_seq_align worker.
#
#   ./service/broker/worker-ctl.sh start      # background, pid in service/data/worker.pid
#   ./service/broker/worker-ctl.sh status     # is it up, since when, what has it been doing
#   ./service/broker/worker-ctl.sh log        # tail -f the log
#   ./service/broker/worker-ctl.sh restart    # after changing the plugin -- which is when
#   ./service/broker/worker-ctl.sh stop       # SIGTERM, and it says if that was safe
#   ./service/broker/worker-ctl.sh services   # what a fresh worker would load (no broker)
#
# `run_worker.sh` is the worker itself and runs in the foreground on purpose: a supervisor
# that hides the first thirty lines of output hides the part that says the token is missing.
# This is the thin layer around it -- a pid file and a log, so a restart is one command and
# "is it running?" is answerable without reading `ps`.
#
# What it deliberately does not do: kill a worker mid-job on its own initiative. A file
# takes a couple of minutes to align, and killing one leaves the job claimed until the
# broker's lease expires (their worker defaults to an hour) before anything else may take
# it. So `stop` sends SIGTERM, waits, and if the process is still there it says so and lets
# you decide -- `kill -9 <pid>` is your call to make, not the script's.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"        # service/broker
ROOT="$(cd "$HERE/../.." && pwd)"                           # the note-seq checkout
DATA="${SERVICE_DATA:-$ROOT/service/data}"
PIDFILE="$DATA/worker.pid"
LOGFILE="$DATA/worker.log"
WORKER="$HERE/run_worker.sh"

usage() { sed -n '3,12p' "$0" | sed 's/^# \{0,1\}//'; exit "${1:-0}"; }

running() {
  [ -f "$PIDFILE" ] || return 1
  local pid; pid="$(cat "$PIDFILE" 2>/dev/null || true)"
  [ -n "$pid" ] || return 1
  kill -0 "$pid" 2>/dev/null || return 1
  echo "$pid"
}

case "${1:-}" in
  start)
    if pid="$(running)"; then
      echo "already running (pid $pid). 'restart' to replace it." >&2
      exit 1
    fi
    mkdir -p "$DATA"
    # setsid when it is there, so the worker survives the terminal it was started from;
    # nohup alone does not detach it from the session.
    launcher=(nohup)
    command -v setsid >/dev/null 2>&1 && launcher=(setsid nohup)
    # "${@:2}" and not "$@": the subcommand is ours, not the worker's -- passing it
    # through made worker.py exit with "unrecognized arguments: start", which the check
    # below then reported as a failure to stay up.
    "${launcher[@]}" "$WORKER" "${@:2}" >>"$LOGFILE" 2>&1 &
    echo $! >"$PIDFILE"
    sleep 1
    if pid="$(running)"; then
      echo "started (pid $pid)"
      echo "  log    $LOGFILE"
      echo "  status $0 status"
    else
      echo "it did not stay up. The last lines of the log usually say why:" >&2
      tail -n 20 "$LOGFILE" >&2 || true
      rm -f "$PIDFILE"
      exit 1
    fi
    ;;

  stop)
    if ! pid="$(running)"; then
      echo "not running"; rm -f "$PIDFILE"; exit 0
    fi
    echo "sending SIGTERM to $pid"
    kill "$pid" 2>/dev/null || true
    for _ in $(seq 1 15); do
      running >/dev/null || break
      sleep 1
    done
    if running >/dev/null; then
      cat >&2 <<EOF
still running after 15 s. Two possibilities:
  * it is in the middle of a job -- stopping now loses that alignment, and the job stays
    claimed until the broker's lease expires, so another worker cannot pick it up either.
    Wait for it to finish (see '$0 log') and stop again.
  * it is stuck. Then: kill -9 $pid
EOF
      exit 1
    fi
    rm -f "$PIDFILE"
    echo "stopped"
    ;;

  restart)
    "$0" stop || { echo "not restarting: it did not stop" >&2; exit 1; }
    exec "$0" start "${@:2}"
    ;;

  status)
    if pid="$(running)"; then
      echo "running (pid $pid)"
      ps -o pid=,etime=,rss=,args= -p "$pid" | sed 's/^/  /' | cut -c1-160
    else
      echo "not running (no live pid in $PIDFILE)"
    fi
    echo "  log    $LOGFILE"
    [ -f "$LOGFILE" ] && { echo "  last lines:"; tail -n 6 "$LOGFILE" | sed 's/^/    /'; } || true
    echo
    echo "settings this worker would use:"
    # Bounded: this loads the plugin to print the contract -- a second of work normally, and
    # a hang if the interpreter or the checkout is wrong. A status command that can hang is
    # worse than one that says less.
    if ! timeout 25 "$WORKER" --list-services 2>&1 | sed -n '1,11p' | sed 's/^/  /'; then
      echo '  (no answer within 25 s; run "services" to see why)'
    fi
    ;;

  log)
    [ -f "$LOGFILE" ] || { echo "no log at $LOGFILE yet" >&2; exit 1; }
    tail -n "${2:-60}" -f "$LOGFILE"
    ;;

  services)
    exec "$WORKER" --list-services
    ;;

  ''|-h|--help) usage 0 ;;
  *) echo "unknown command: $1" >&2; usage 1 ;;
esac
