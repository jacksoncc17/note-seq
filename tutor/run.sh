#!/usr/bin/env bash
# Convenience launcher for the tutor.
#
#   ./run.sh check     verify interpreter, question bank, servers, mic
#   ./run.sh quiz      drill questions in the terminal (no servers needed)
#   ./run.sh text      free text conversation with the tutor
#   ./run.sh voice     full voice conversation (needs ASR + TTS servers)
#   ./run.sh mic       microphone + VAD test (needs ASR)
#   ./run.sh lessons   scripted demo of the whole loop, no hardware needed
#   ./run.sh tts ...   TTS probe, e.g. ./run.sh tts --roundtrip
#   ./run.sh asr ...   ASR probe, e.g. ./run.sh asr some.wav
#
# Everything is overridable in your environment, e.g.
#   TUTOR_TTS_VOICE=zf_xiaoxiao TUTOR_TTS_LANG_CODE=z ./run.sh voice
#   TUTOR_ASR_BASE_URL=http://other-host:9000 ./run.sh mic
set -euo pipefail
cd "$(dirname "$0")"

PY="../.venv/bin/python"
if [[ ! -x "$PY" ]]; then
  echo "no virtualenv at $(cd .. && pwd)/.venv" >&2
  echo "create it with:  python3 -m venv .venv && .venv/bin/pip install ." >&2
  exit 2
fi

# Servers on the LAN. Override these two lines for a different setup.
use_servers() {
  export TUTOR_ASR=${TUTOR_ASR:-openai}
  export TUTOR_TTS=${TUTOR_TTS:-openai}
  export TUTOR_ASR_BASE_URL=${TUTOR_ASR_BASE_URL:-http://192.168.2.174:9000}
  export TUTOR_TTS_BASE_URL=${TUTOR_TTS_BASE_URL:-http://192.168.2.174:8880/v1}
}

mode="${1:-check}"
shift || true

case "$mode" in
  check)
    use_servers
    exec "$PY" -m tutor.cli --doctor "$@"
    ;;
  quiz)
    # Offline: scripted tutor, printed replies, no audio.
    export TUTOR_ASR=stub TUTOR_TTS=stub
    exec "$PY" -m tutor.cli --text --mode drill "$@"
    ;;
  text)
    export TUTOR_ASR=stub TUTOR_TTS=stub
    exec "$PY" -m tutor.cli --text --mode converse "$@"
    ;;
  lessons)
    export TUTOR_ASR=stub TUTOR_TTS=stub
    exec "$PY" -m tutor.cli --scripted "$@"
    ;;
  voice)
    use_servers
    exec "$PY" -m tutor.cli --voice --mode converse \
         --mic-seconds "${TUTOR_SECONDS:-120}" "$@"
    ;;
  mic)
    use_servers
    exec "$PY" -m tutor.cli --mic-test "$@"
    ;;
  tts)
    use_servers
    exec "$PY" -m tutor.cli --tts-probe "$@"
    ;;
  asr)
    use_servers
    exec "$PY" -m tutor.cli --asr-probe "$@"
    ;;
  *)
    echo "usage: run.sh [check|quiz|text|voice|mic|lessons|tts|asr] [args...]" >&2
    exit 2
    ;;
esac
