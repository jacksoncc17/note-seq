# tutor — voice-first conversational music tutor (skeleton)

A runnable skeleton of the architecture in [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md):
a conductor with a mode state machine, a tool-calling loop over the
deterministic score facts built by `memorization/analyze.py`, post-generation
grounding verification, and swappable audio seams (VAD / ASR / TTS / instrument).

It runs today with **no model and no audio hardware** — a scripted tutor drives
the same tool loop a real LLM would — and points at a self-hosted model by
changing environment variables.

## Running it

### The Python environment

**Use the virtualenv at `../.venv`** (Python 3.12). It is required, not optional:

```bash
$ .venv/bin/python -c "import note_seq"     # ok
$ python3 -c "import note_seq"              # ModuleNotFoundError
```

The system interpreter has neither `note_seq` nor `absl`; everything here runs
against the venv. You do **not** need to activate it — `./run.sh` and
`../.venv/bin/python` both work directly.

If you ever need to rebuild it (≈720 MB, because `note-seq` pulls in librosa,
bokeh, pretty_midi and friends):

```bash
cd /home/wei/music_gen/note-seq
python3 -m venv .venv
.venv/bin/pip install .          # note-seq and its dependencies
.venv/bin/pip install pytest     # only for running the test suite
```

### The launcher

```bash
cd tutor
./run.sh check     # verify interpreter, question bank, both servers, mic, playback
./run.sh quiz      # drill questions in the terminal  -- no servers needed
./run.sh text      # free text conversation           -- no servers needed
./run.sh voice     # full voice conversation          -- needs ASR + TTS servers
./run.sh mic       # microphone + VAD test            -- needs ASR
./run.sh lessons   # scripted demo of the whole loop  -- no hardware needed
./run.sh tts       # TTS probe,  e.g. ./run.sh tts --roundtrip
./run.sh asr       # ASR probe,  e.g. ./run.sh asr clip.wav
```

`./run.sh check` first: it makes a real request to each server rather than a
ping, records a second of microphone audio, and prints the fix for anything
broken.

| | needs |
|---|---|
| `quiz`, `text`, `lessons` | nothing but the venv — scripted tutor, printed replies |
| `mic`, `asr` | an ASR server |
| `voice`, `tts` | an ASR server and a TTS server |

The LAN addresses live in `run.sh` (override without editing it):

```bash
TUTOR_ASR_BASE_URL=http://other-host:9000 ./run.sh mic
TUTOR_TTS_VOICE=zf_xiaoxiao TUTOR_TTS_LANG_CODE=z ./run.sh voice   # Mandarin
TUTOR_SECONDS=300 ./run.sh voice                                  # longer session
```

Every `TUTOR_*` variable maps to a field of `TutorConfig` (`tutor/config.py`).
Two of them must be set for a real conversation, because the defaults are
offline stubs: `TUTOR_ASR=openai` and `TUTOR_TTS=openai` — `run.sh` sets both
for the modes that need them.

### Without the launcher

```bash
cd tutor

# offline scripted session: greeting, drill, answers, playing, modes
../.venv/bin/python -m tutor.cli --scripted

# see the grounding verifier catch a fabricated fact (bar 25 vs bar 53)
../.venv/bin/python -m tutor.cli --demo-grounding

# interactive drill (typed), with the debug commands
../.venv/bin/python -m tutor.cli --text --mode drill
#   :drill  :listen  :teach  :map  :bars 40 48  :play 1 8  :play-wrong 1 8
#   :status  :quit

# the test suite
../.venv/bin/python -m pytest tests -q
```

The tutor reads the fingerprint produced by the deterministic core. If it is
missing:

```bash
cd .. && .venv/bin/python memorization/analyze.py /path/to/score.musicxml
```

### Adding a piece to learn

Submit a **MusicXML file** — `.musicxml`, `.xml` or compressed `.mxl`. That is
the only input; everything else is derived. (A PDF is not usable directly: export
MusicXML from MuseScore/Dorico/Sibelius/Finale first.)

```bash
cd /home/wei/music_gen/note-seq
.venv/bin/python memorization/analyze.py /path/to/new-piece.mxl
```

That is the whole submission step. It takes ~45 s for a 70-bar piece and ~2.5 min
for a 200-bar one (per-bar chord inference dominates), prints what it found, and
writes **its own directory** so nothing is overwritten:

```
memorization/output/pieces/<slug>/fingerprint.json     the question bank
memorization/output/pieces/<slug>/fingerprint.mid      expressive timing
memorization/output/pieces/<slug>/fingerprint_steady.mid   steady practice tempo
memorization/output/pieces/index.json                  one entry per piece
```

Then:

```bash
cd tutor
./run.sh check --piece <slug>     # verify everything, including both servers
./run.sh quiz  --piece <slug>     # drill in the terminal
./run.sh voice --piece <slug>     # spoken conversation
./run.sh quiz --list-pieces       # what exists, newest first
```

With no `--piece`, the **most recently generated** bank is used. Adding a piece
never destroys an existing one; each stays drillable by name.

What to look at in the analyzer's output for a new piece:

* **bar count** — compare against the printed score. If it disagrees, the tool
  says so and falls back to a time grid (and the bar numbers stop matching the
  page).
* **`sections (...)`** — `key-signatures` means the section boundaries came from
  the score and section questions are quizzable; `tonal-areas (heuristic)` means
  the piece has no key changes, so those questions are **gated out** and you get
  a bank built from bar-level facts and chords instead.
* **`repeated passages`** — verified note-for-note repeats. For a piece assembled
  from blocks, this is the single biggest memory saving.

### The original question-bank tool

`memorization/` is the simpler, non-conversational half of this project: it
turns a score into questions and can sample them without any tutor machinery.

```bash
cd /home/wei/music_gen/note-seq
.venv/bin/python memorization/analyze.py /path/to/score.musicxml   # build the bank
.venv/bin/python memorization/quiz.py sheet --measures             # study map
.venv/bin/python memorization/quiz.py drill --tier 1 --count 8     # sample questions
.venv/bin/python memorization/quiz.py stats                        # progress so far
```

## Point it at a self-hosted model

```bash
TUTOR_LLM=openai \
TUTOR_LLM_BASE_URL=http://127.0.0.1:8000/v1 \
TUTOR_LLM_MODEL=Qwen2.5-14B-Instruct \
  ../.venv/bin/python -m tutor.cli --text
```

Any OpenAI-compatible server with tool calling works (vLLM, llama.cpp server,
Ollama, LM Studio, TGI). Same pattern for speech:

```bash
TUTOR_ASR=openai TUTOR_ASR_BASE_URL=http://127.0.0.1:8081/v1 \
TUTOR_TTS=openai TUTOR_TTS_BASE_URL=http://127.0.0.1:8880/v1 \
TUTOR_TTS_VOICE=af_heart ... 
```

Piano in:

```bash
TUTOR_INSTRUMENT=midi ../.venv/bin/python -m tutor.cli --text
```

Every `TUTOR_*` variable maps to a field of `TutorConfig`
(`tutor/config.py`) — tier ceiling, endpointing, coach delay, grounding policy,
steady practice tempo, log path.

## ASR servers: two API shapes, auto-detected

Whisper servers disagree about their HTTP surface, so the adapter speaks both
and `asr_api: auto` decides by reading the server's `/openapi.json`:

| Server | Shape | Endpoint |
|---|---|---|
| `ahmetoner/whisper-asr-webservice` | `whisper_asr` | `POST /asr?output=json&language=..&initial_prompt=..`, multipart field `audio_file` |
| faster-whisper-server, speaches, vLLM audio, whisper.cpp | `openai` | `POST /v1/audio/transcriptions`, multipart field `file` |

```bash
# verified against a live whisper-asr-webservice v1.10 on a LAN GPU box
../.venv/bin/python -m tutor.cli --asr-probe --asr-url http://192.168.2.174:9000
#   api detected: whisper_asr
#   transcript  : 'measure 53, the key changes to a major, now play bar for D'
#   confidence  : 0.674   no-speech: 0.019   latency: 0.09s (0.02x realtime)
```

Notes from that test:

* `initial_prompt` is the biasing hook on the `whisper_asr` shape, and it works:
  "measure fifty three" came back as **"Measure 53"**, which is exactly what the
  grader wants. `asr_hotwords` feeds it.
* Posting the *wrong* shape gets the connection reset, not a clean 404 — so the
  error message names the endpoint it tried and tells you to try
  `TUTOR_ASR_API=auto`.
* Music returns an empty transcript with no hallucinated lyrics, and empty
  transcripts report confidence `0.0` rather than `1.0`.

## Layout

```
tutor/
  tutor/events.py      the conductor's input alphabet (speech, music, ticks)
  tutor/modes.py       mode state machine + floor control (pure, tested)
  tutor/audio/         VAD, ASR, TTS, instrument: protocols + stub + HTTP adapters
  tutor/core/facts.py  fact model with provenance + the quizzability gate
  tutor/core/tools.py  the deterministic tools, answer normalisation, grading
  tutor/core/registry.py  tool schemas handed to the model
  tutor/llm/           LlmClient protocol, scripted tutor, OpenAI-compatible client
  tutor/grounding.py   post-generation claim verification (block / hedge / warn)
  tutor/conductor.py   the loop that joins all of it
  tutor/session.py     attempts, mastery, append-only turn log
  tutor/cli.py         entry point
  tests/               35 tests over the load-bearing parts
  docs/ARCHITECTURE.md the design document
```

## What is real vs stubbed

| Piece | State |
|---|---|
| Mode machine, floor control, barge-in, coach delay | **real**, unit-tested |
| Fact model, provenance gate, answer normalisation, grading | **real**, runs on the Chopin fingerprint |
| Grounding verifier (claim extraction + block/hedge/warn) | **real**, unit-tested |
| Tool loop, session log, JSONL turn records | **real** |
| Scripted tutor (no model needed) | **real policy**, not a mock — it calls the same tools |
| OpenAI-compatible LLM / ASR / TTS clients | **real code**, untested against a live server here |
| Energy VAD | real but basic; use Silero/webrtcvad in production |
| MIDI instrument | real (mido), needs a keyboard |
| Audio piano transcription, DTW alignment, AEC | **not implemented** — see the doc's known limitations |

## Where the value is

The tutor can only say what the tools returned. Run `--demo-grounding` to watch
it refuse "the A major section starts at bar 25" (a fabrication this project
actually made) while accepting "at bar 53". That single property — a tutor that
cannot bluff about your score — is the point of the whole design.
