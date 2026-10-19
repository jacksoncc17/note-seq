#!/usr/bin/env bash
#
# Install the note-seq worker on a machine that will run broker jobs.
#
#   ./service/deploy/worker/install.sh --check      # read-only: say what is present, what is not
#   ./service/deploy/worker/install.sh              # install whatever is missing
#   ./service/deploy/worker/install.sh --secrets-from ~/other/service/broker/worker.env
#
# It runs *from the checkout*, because that is how the worker runs: `run_worker.sh` starts the
# broker's `worker.py` on this repository's venv, and the plugin it loads calls the analyzer in
# this tree. So this is an installer for a machine, not for a package, and it expects to find
# itself inside the clone it is going to run.
#
# Steps, in the order that matters. Every one is skipped when it is already done:
#
#   1. system packages -- ffmpeg (audio conversion), fluidsynth + a soundfont (the reference
#      MIDI is rendered and then compared with the recording), libcairo2 (the page PNG),
#      python3-venv. Needs sudo; where sudo wants a password this prints the one command
#      instead of hanging on a prompt nobody can answer.
#   2. the interpreter -- Python 3.12 via uv, then the pinned venv from requirements.txt
#      beside this script. The system python on Ubuntu 22.04 is 3.10, where pip stops at
#      librosa 0.11.0; uv fetches 3.12 without root. This is the slow step: ~770 MB of wheels.
#   3. the broker's worker.py -- deliberately NOT in this repository, and run_worker.sh refuses
#      to start without it. Both worker.py and the `worker_plugins` package beside it are
#      copied, because worker.py imports that package.
#   4. service/broker/worker.env -- this machine's settings and secrets, 0600, never committed.
#      Created only when absent: an existing one is a machine that is already configured, and
#      rewriting it silently would rotate a working key.
#   5. the systemd user unit -- installed always, enabled and started only when the key in (4)
#      is real, so a placeholder does not become a restart loop.
#   6. verification -- the plugin loads under the venv, and which soundfont the alignment will
#      use. The second is not cosmetic: another soundfont changes the chroma of the rendered
#      reference and therefore the map (measured on the Chopin waltz: bars 1-18 come out at
#      30.53 s with Sonatina, 30.02 s with FluidR3_GM).
#
# What it does not do: clone the repository, register the worker with the broker, or assign the
# archive a bucket. The first is `git clone --branch desktop <url>`; the second is a curl that
# returns the key exactly once; the third is `garage/native/bootstrap.sh` on the archive host.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../../.." && pwd)"
AVENV="$REPO/.venv/bin/python"
WORKER_HOME="$HOME/note-seq-worker"
UNIT_DIR="$HOME/.config/systemd/user"
UNIT="$UNIT_DIR/note-seq-worker.service"
ENVFILE="$REPO/service/broker/worker.env"

CHECK=0
FORCE=0
NO_APT=0
NO_UNIT=0
BROKER_WORKER=""
SECRETS_FROM=""
KEY_FILE=""
TOKEN_FILE=""
NAME="${WORKER_NAME:-note-seq-$(hostname -s 2>/dev/null || echo worker)}"
BROKER_URL="${BROKER_URL:-}"
VIEWER_URL="${NOTE_SEQ_VIEWER_URL:-}"
ARCHIVE_URL="${SERVICE_ARCHIVE_URL:-http://127.0.0.1:3900}"
ARCHIVE_BUCKET="${SERVICE_ARCHIVE_BUCKET:-note-seq}"

say()  { printf '==> %s\n' "$*"; }
warn() { printf '[!] %s\n' "$*" >&2; }
die()  { printf '[x] %s\n' "$*" >&2; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }

usage() {
  cat <<'USAGE'
install.sh -- install the note-seq worker on this machine

  --check                 read-only: report what is present and what is missing
  --force                 rebuild the venv / worker.py copy / worker.env / unit
  --no-apt                skip the system-package step
  --no-unit               skip the systemd unit
  --name NAME             the worker's name at the broker (default: note-seq-<hostname>)
  --broker-url URL        the broker's API root, /api/v1 included
  --viewer-url URL        the page a finished piece links to (default: <broker>/note-seq)
  --archive-url URL       the S3 endpoint of the archive (default: http://127.0.0.1:3900)
  --archive-bucket NAME   bucket name (default: note-seq)
  --broker-worker PATH    the broker checkout's worker.py
  --secrets-from FILE     take WORKER_KEY / SERVICE_PUBLISH_TOKEN / archive credentials
                          from an existing worker.env
  --key-file FILE         just the worker key (one line, or WORKER_KEY=... in a file)
  --token-file FILE       just the publish token
USAGE
}

while [ $# -gt 0 ]; do
  case "$1" in
    --check) CHECK=1 ;;
    --force) FORCE=1 ;;
    --no-apt) NO_APT=1 ;;
    --no-unit) NO_UNIT=1 ;;
    --name) NAME="${2:?--name needs a value}"; shift ;;
    --broker-url) BROKER_URL="${2:?--broker-url needs a value}"; shift ;;
    --viewer-url) VIEWER_URL="${2:?--viewer-url needs a value}"; shift ;;
    --archive-url) ARCHIVE_URL="${2:?--archive-url needs a value}"; shift ;;
    --archive-bucket) ARCHIVE_BUCKET="${2:?--archive-bucket needs a value}"; shift ;;
    --broker-worker) BROKER_WORKER="${2:?--broker-worker needs a path}"; shift ;;
    --secrets-from) SECRETS_FROM="${2:?--secrets-from needs a path}"; shift ;;
    --key-file) KEY_FILE="${2:?--key-file needs a path}"; shift ;;
    --token-file) TOKEN_FILE="${2:?--token-file needs a path}"; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown option: $1 (try --help)" ;;
  esac
  shift
done

[ -f "$REPO/service/broker/run_worker.sh" ] \
  || die "$REPO does not look like the note-seq checkout (no service/broker/run_worker.sh)"
[ -f "$HERE/requirements.txt" ] || die "missing $HERE/requirements.txt"
[ -f "$HERE/note-seq-worker.service" ] || die "missing $HERE/note-seq-worker.service"

MISSING=()

soundfont_file() {
  local f
  for f in "$HOME/soundfonts/Sonatina_Symphonic_Orchestra.sf2" \
           /usr/share/sounds/sf2/FluidR3_GM.sf2 \
           /etc/alternatives/default-GM.sf2; do
    [ -f "$f" ] && { echo "$f"; return 0; }
  done
  return 1
}

apt_missing() {
  local out=()
  have ffmpeg || out+=(ffmpeg)
  have fluidsynth || out+=(fluidsynth)
  soundfont_file >/dev/null || out+=(fluid-soundfont-gm)
  ldconfig -p 2>/dev/null | grep -q 'libcairo\.so\.2' \
    || ls /usr/lib/*/libcairo.so.2 >/dev/null 2>&1 || out+=(libcairo2)
  python3 -c 'import ensurepip' >/dev/null 2>&1 || out+=(python3-venv)
  [ ${#out[@]} -eq 0 ] || printf '%s\n' "${out[@]}"
}

apt_ok() { [ -z "$(apt_missing)" ]; }

venv_ok() {
  [ -x "$AVENV" ] || return 1
  "$AVENV" -c 'import librosa, verovio, soundfile, mido, pretty_midi, cairosvg, numba' \
    >/dev/null 2>&1
}

find_broker_worker() {
  if [ -n "$BROKER_WORKER" ]; then
    [ -f "$BROKER_WORKER" ] || die "--broker-worker $BROKER_WORKER does not exist"
    echo "$BROKER_WORKER"; return 0
  fi
  local candidate
  for candidate in \
      "$REPO/../selfCommentary/full-stack-fastapi-template/worker.py" \
      "$HOME/playground/goGames/selfCommentary/full-stack-fastapi-template/worker.py" \
      "$HOME/full-stack-fastapi-template/worker.py"; do
    [ -f "$candidate" ] && { echo "$candidate"; return 0; }
  done
  return 1
}

worker_py_ok() {
  [ -f "$WORKER_HOME/worker.py" ] && [ -d "$WORKER_HOME/worker_plugins" ] || return 1
  [ -x "$AVENV" ] || return 0                 # nothing to import it with, nothing to judge
  "$AVENV" -c "import sys; sys.path.insert(0, '$WORKER_HOME'); import worker_plugins" \
    >/dev/null 2>&1
}

env_ok() {
  [ -f "$ENVFILE" ] || return 1
  [ "$(stat -c '%a' "$ENVFILE" 2>/dev/null || echo 000)" = 600 ] || return 1
  grep -q '^WORKER_KEY=.\+' "$ENVFILE" && ! grep -q '^WORKER_KEY=replace-me$' "$ENVFILE" || return 1
  grep -q '^SERVICE_PUBLISH_TOKEN=.\+' "$ENVFILE" \
    && ! grep -q '^SERVICE_PUBLISH_TOKEN=replace-me$' "$ENVFILE" || return 1
  return 0
}

key_is_real() {
  [ -f "$ENVFILE" ] || return 1
  grep -q '^WORKER_KEY=.\+' "$ENVFILE" && ! grep -q '^WORKER_KEY=replace-me$' "$ENVFILE"
}

unit_ok() { [ -f "$UNIT" ] && grep -q "^ExecStart=$REPO/service/broker/run_worker.sh" "$UNIT"; }

report() {   # label, then a command
  local label="$1"; shift
  if "$@" >/dev/null 2>&1; then printf '  ok      %s\n' "$label"
  else printf '  missing %s\n' "$label"; MISSING+=("$label"); fi
}

# One value out of a file: either `NAME=value` in an env file, or a file that is the value.
secret_from() {
  local file="$1" name="$2"
  [ -n "$file" ] && [ -f "$file" ] || return 0
  if grep -q "^${name}=" "$file"; then
    grep -m1 "^${name}=" "$file" | cut -d= -f2-
  else
    sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' -e '/^[[:space:]]*$/d' \
        -e '/^#/d' "$file" | head -n1
  fi
}

prompt() {   # message, default -- asks only when someone is there to answer
  local answer=''
  [ -t 0 ] || return 0
  read -rp "$1 [$2]: " answer
  [ -n "$answer" ] && echo "$answer" || echo "$2"
}

# ------------------------------------------------------------------------------- --check
if [ "$CHECK" = 1 ]; then
  say "checking $REPO"
  printf '  python  %s\n' "$(python3 -V 2>&1)"
  printf '  home    %s\n' "$HOME"
  report "system packages (ffmpeg, fluidsynth, soundfont, libcairo2, python3-venv)" apt_ok
  apt_missing | sed 's/^/          needs apt: /'
  report "venv ($REPO/.venv)" venv_ok
  report "broker worker.py + worker_plugins ($WORKER_HOME)" worker_py_ok
  report "worker.env (0600, real key and token)" env_ok
  report "systemd user unit ($UNIT)" unit_ok
  if have systemctl; then
    printf '  state   %s / %s\n' \
      "$(systemctl --user is-active note-seq-worker 2>/dev/null || echo n/a)" \
      "$(systemctl --user is-enabled note-seq-worker 2>/dev/null || echo n/a)"
  fi
  if sf="$(soundfont_file)"; then
    printf '  font    %s\n' "$sf"
    case "$sf" in
      */Sonatina_Symphonic_Orchestra.sf2) ;;
      *) warn "another machine using Sonatina will map the same piece slightly differently
         (30.53 s vs 30.02 s for bars 1-18 of the Chopin waltz). Same file at
         $HOME/soundfonts/Sonatina_Symphonic_Orchestra.sf2 makes them agree." ;;
    esac
  else
    warn "no soundfont: the alignment cannot render its reference MIDI"
  fi
  if [ ${#MISSING[@]} -eq 0 ]; then say "everything is in place"; exit 0; fi
  say "not installed: ${MISSING[*]}"
  exit 1
fi

# ------------------------------------------------------------------------ 1. packages
apt_list="$(apt_missing || true)"
if [ -z "$apt_list" ]; then
  say "system packages already present"
elif [ "$NO_APT" = 1 ]; then
  warn "skipping apt (--no-apt); still needed: $(echo "$apt_list" | tr '\n' ' ')"
elif sudo -n true 2>/dev/null; then
  say "installing system packages: $(echo "$apt_list" | tr '\n' ' ')"
  sudo apt-get update -qq
  # shellcheck disable=SC2086
  sudo apt-get install -y $apt_list
else
  die "these need sudo, and sudo on this machine wants a password. Run:
    sudo apt-get update && sudo apt-get install -y $(echo "$apt_list" | tr '\n' ' ')
  then run this script again."
fi

# ---------------------------------------------------------------------------- 2. venv
if venv_ok && [ "$FORCE" = 0 ]; then
  say "venv already importable: $("$AVENV" -V 2>&1)"
else
  UV="$(command -v uv 2>/dev/null || true)"
  if [ -z "$UV" ] && [ -x "$HOME/.local/bin/uv" ]; then UV="$HOME/.local/bin/uv"; fi
  if [ -z "$UV" ]; then
    say "installing uv (python3 -m pip install --user uv)"
    python3 -m pip install --user -q uv >/dev/null 2>&1 || die "could not install uv with pip"
    UV="$HOME/.local/bin/uv"
    [ -x "$UV" ] || die "pip reported success but there is no $UV"
  fi
  if [ "$FORCE" = 1 ] && [ -d "$REPO/.venv" ]; then
    say "--force: removing $REPO/.venv"
    rm -rf "$REPO/.venv"
  fi
  if [ ! -x "$AVENV" ]; then
    say "fetching Python 3.12 with uv (no root needed)"
    "$UV" python install 3.12
    say "creating $REPO/.venv"
    "$UV" venv --python 3.12 "$REPO/.venv"
  fi
  say "installing requirements.txt (~770 MB of wheels, once)"
  "$UV" pip install -q --python "$AVENV" -r "$HERE/requirements.txt"
  venv_ok || die "still cannot import librosa/verovio after installing requirements.txt"
  say "venv ready: $("$AVENV" -V 2>&1)"
fi

# ------------------------------------------------------------- 3. broker's worker.py
if worker_py_ok && [ "$FORCE" = 0 ]; then
  say "broker worker.py already installed at $WORKER_HOME"
else
  src="$(find_broker_worker || true)"
  if [ -z "$src" ]; then
    die "the broker's worker.py was not found -- it is not in this repository. Point at it:
    $0 --broker-worker /path/to/full-stack-fastapi-template/worker.py"
  fi
  say "copying worker.py and worker_plugins from $(dirname "$src") to $WORKER_HOME"
  mkdir -p "$WORKER_HOME"
  cp "$src" "$WORKER_HOME/worker.py"
  rm -rf "$WORKER_HOME/worker_plugins"
  cp -r "$(dirname "$src")/worker_plugins" "$WORKER_HOME/"
  worker_py_ok || die "worker.py and worker_plugins are in $WORKER_HOME but do not import -- see
    $AVENV -c 'import sys; sys.path.insert(0, \"$WORKER_HOME\"); import worker_plugins'"
fi

# ------------------------------------------------------------------ 4. worker.env
if [ -f "$ENVFILE" ] && [ "$FORCE" = 0 ]; then
  say "keeping the existing $ENVFILE"
  if ! env_ok; then
    warn "$ENVFILE exists but is not complete (placeholder key/token, or mode is not 600)."
    warn "Fix it in place, or re-run with --force to rebuild it."
  fi
else
  if [ -z "$BROKER_URL" ]; then
    BROKER_URL="$(prompt 'Broker URL (including /api/v1)' 'https://broker.example.com/api/v1')"
  fi
  [ -n "$BROKER_URL" ] || die "no broker URL: pass --broker-url https://<broker>/api/v1"
  [ -n "$VIEWER_URL" ] || VIEWER_URL="${BROKER_URL%/api/v1}/note-seq"

  key="$(secret_from "$KEY_FILE" WORKER_KEY)"
  token="$(secret_from "$TOKEN_FILE" SERVICE_PUBLISH_TOKEN)"
  akey="$(secret_from "$SECRETS_FROM" SERVICE_ARCHIVE_KEY)"
  asecret="$(secret_from "$SECRETS_FROM" SERVICE_ARCHIVE_SECRET)"
  [ -n "$key" ] && [ -n "$token" ] \
    || warn "no key and/or token supplied: writing placeholders, and the unit will not be started."

  say "writing $ENVFILE (0600)"
  (
  umask 077
  cat >"$ENVFILE" <<ENVEOF
# This machine's worker settings. Written by service/deploy/worker/install.sh; not committed
# (service/.gitignore). WORKER_KEY comes from the broker exactly once:
#   curl -s -X POST https://<broker>/api/v1/admin/workers/ \\
#     -H "Authorization: Bearer \$TOKEN" -H 'Content-Type: application/json' \\
#     -d '{"name": "$NAME"}'
BROKER_URL=${BROKER_URL}
WORKER_KEY=${key:-replace-me}
WORKER_NAME=${NAME}
NOTE_SEQ_VIEWER_URL=${VIEWER_URL}

# Where a finished piece is sent; must be the page's own SERVICE_UPLOAD_TOKEN or the send
# fails with 401 and the piece waits in data/outbox/ instead of appearing on the page.
SERVICE_PUBLISH_TOKEN=${token:-replace-me}

# The broker's worker.py, copied here by this script -- it is not in this repository.
WORKER_PY=$WORKER_HOME/worker.py

# The archive: loopback on the archive machine itself, its LAN address from anywhere else.
# Archiving happens before publishing, and eviction refuses to run without it.
SERVICE_ARCHIVE_URL=${ARCHIVE_URL}
SERVICE_ARCHIVE_BUCKET=${ARCHIVE_BUCKET}
SERVICE_ARCHIVE_KEY=${akey:-replace-me}
SERVICE_ARCHIVE_SECRET=${asecret:-replace-me}
ENVEOF
  )
  chmod 600 "$ENVFILE"
fi

# --------------------------------------------------------------------- 5. the unit
if [ "$NO_UNIT" = 1 ]; then
  say "skipping the systemd user unit (--no-unit)"
else
  if unit_ok && [ "$FORCE" = 0 ]; then
    say "keeping the existing $UNIT"
  else
    say "installing $UNIT"
    mkdir -p "$UNIT_DIR"
    sed "s#@REPO@#$REPO#g" "$HERE/note-seq-worker.service" >"$UNIT"
    if have systemctl; then systemctl --user daemon-reload || true; fi
  fi
  if ! key_is_real; then
    warn "unit installed but NOT started: $ENVFILE has no real WORKER_KEY yet."
    warn "once it does:  systemctl --user enable --now note-seq-worker"
  elif have systemctl; then
    systemctl --user enable --now note-seq-worker
    sleep 3
    printf '  worker: %s\n' "$(systemctl --user is-active note-seq-worker 2>/dev/null || echo unknown)"
  fi
fi

# ----------------------------------------------------------------- 6. verification
if venv_ok; then
  say "what a worker would load:"
  "$REPO/service/broker/run_worker.sh" --list-services 2>&1 \
    | sed -n '/services this worker can run/,$p' | sed 's/^/  /'
fi
if sf="$(soundfont_file)"; then
  case "$sf" in
    */Sonatina_Symphonic_Orchestra.sf2)
      say "soundfont: $sf (the same one the other machines use)" ;;
    *)
      warn "soundfont: $sf -- a different reference timbre, so this machine's maps will differ
         slightly from a machine using Sonatina (30.02 s vs 30.53 s on the Chopin waltz).
         Copy that .sf2 to $HOME/soundfonts/Sonatina_Symphonic_Orchestra.sf2 -- the path
         analyzer/analyzer/align.py prefers -- to make the two agree." ;;
  esac
fi
say "done -- see $HERE/README.md for what to check next"
