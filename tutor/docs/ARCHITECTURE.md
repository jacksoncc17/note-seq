# Architecture: voice-first conversational music tutor

A self-hosted, voice-driven piano tutor that (a) talks with you, (b) hears you
play, and (c) never invents a musical fact. This document is the design; the
code in this directory is a runnable skeleton of it.

---

## 1. The core insight

Three things make this different from a generic voice assistant, and each one
shapes the architecture:

1. **Two audio channels, not one.** Speech and piano have different failure
   modes. Merging them into one microphone stream means the tutor's own voice
   triggers note detection, your voice mangles the transcriber, and the tutor
   "hears" notes that were never played. Voice and instrument are separate
   paths from the microphone inward.
2. **Two knowledge paths.** The LLM is a conversation and policy engine, not a
   knowledge base. Every musical fact comes from a deterministic tool over the
   score. This is enforced after generation, not merely requested in a prompt.
3. **Turn taking must respect music.** Silence means something different when
   someone is holding a fermata. "Stop talking and let them play" is a
   first-class rule, and phrase boundaries (not just VAD) are the events that
   drive coaching.

---

## 2. Component map

```
   you (piano)
     │  speech ──► mic A ──► EnergyVad ──► Asr ─────────────┐
     │  playing ─► mic B / MIDI ──► InstrumentSource ──┐     │
     │                                                 │     │
     ▼                                                 ▼     ▼
 ┌──────────────────────────── Conductor ──────────────────────────────┐
 │  modes.on_event(state, event, cfg) -> decisions                     │
 │  floor control · barge-in · phrase endpointing · coach delay        │
 │                                                                     │
 │  respond(trigger):                                                  │
 │    build_messages(system prompt + STATE, history)                   │
 │      └─► LlmClient.complete(messages, TOOL_SPECS)                   │
 │            └─► ToolBox.dispatch(name, args)  ──► FactStore          │
 │                  (facts, gate, grading, comparison, renders)        │
 │    GroundingVerifier.check(text) ──► block / hedge / warn           │
 │    Tts.speak(text, cancel_event)                                    │
 │    Session.log_turn(...)                                            │
 └─────────────────────────────────────────────────────────────────────┘
                                  │
                    tutor/output/session.jsonl  (durable memory)
```

Files: `tutor/conductor.py`, `tutor/modes.py`, `tutor/grounding.py`,
`tutor/core/{facts,tools,registry}.py`, `tutor/llm/*`, `tutor/audio/*`,
`tutor/session.py`.

---

## 3. The seams

Every seam is a `Protocol` with a stub that runs offline plus a real adapter.
That is what lets the whole loop be tested without a GPU, and separates "the
architecture is wrong" from "the model is bad".

| Seam | Protocol | Offline | Self-hosted | File |
|---|---|---|---|---|
| VAD | `Vad.push(frame) -> 'speech_start'\|'speech_end'\|None` | `EnergyVad` | Silero / webrtcvad (drop-in) | `audio/vad_energy.py` |
| ASR | `Asr.transcribe(pcm, rate)` | `StubAsr` (scripted) | `OpenAiCompatAsr` (`/v1/audio/transcriptions`) or `WhisperAsrWebservice` (native `/asr`), chosen by `detect_asr_api` | `audio/asr.py` |
| TTS | `Tts.speak(text, cancel)` → cancellable per sentence | `StubTts` (prints) | Kokoro-FastAPI, openedai-speech, Piper | `audio/tts.py` |
| Instrument | `InstrumentSource.events()` → `MusicStart/NoteEvent/MusicEnd` | `StubInstrument` | `MidiInstrument` (mido) | `audio/instrument.py` |
| LLM | `LlmClient.complete(messages, tools)` → text + tool calls | `ScriptedTutor` | any OpenAI-compatible server | `llm/stub.py`, `llm/openai_compat.py` |

The scripted tutor is **not a mock**: it issues the same tool calls a real model
would (draw a question, grade an answer), so the tool loop, verifier and log are
all exercised. It doubles as executable documentation of the tutoring policy.

---

## 4. Turn taking (the mode machine)

`modes.on_event` is pure — state + event in, decisions out — which is why
turn-taking has unit tests and needs no audio.

| Mode | Behaviour |
|---|---|
| `TEACH` | explain, answer questions, comment after you play |
| `DRILL` | ask one question, wait for speech *or* playing, grade, advance |
| `LISTEN_COACH` | stay silent while you play; comment at phrase ends |
| `CONVERSE` | free conversation |
| `REVIEW` | go over the session log |

| Event | Decisions |
|---|---|
| `SpeechStart` while the tutor is speaking | `CANCEL_TTS` (barge-in) |
| `SpeechEnd` with text | `RESPOND_TEXT` |
| `SpeechEnd` empty | `STAY_SILENT` (a skipped question is not a wrong answer) |
| `MusicStart` while the tutor is speaking | `CANCEL_TTS` if `duck_tts_while_playing` |
| `MusicEnd` in DRILL with a pending question | `COMMENT_PLAYING` — the playing *is* the answer |
| `MusicEnd` in LISTEN_COACH / TEACH | arm `comment_due_at = now + coach_delay_ms`, `STAY_SILENT` |
| `Tick` past `comment_due_at`, still silent | `COMMENT_PLAYING` |
| `Tick` while music is active again | `STAY_SILENT` — never talk over playing |

Parameters that matter: `music_endpoint_silence_ms` (what counts as a phrase
boundary), `coach_delay_ms` (how long to wait after one before speaking — a
fermata or a breath must not trigger a lecture), `vad_hangover_ms`.

---

## 5. Grounding: the part that keeps the tutor honest

Prompt instructions are not enforcement. This project's own history supplies the
cautionary tale: a heuristic reported "the A major section starts at bar 25" when
the score's key signature changes at bar 53, and the wrong answer was delivered
with full confidence.

Two mechanisms, in order:

**A. A quizzability gate on facts** (`core/facts.py`). Each question inherits
provenance from the artifact it came from:

| Provenance | Meaning | Quizzable |
|---|---|---|
| `score-exact` | read from the score's notation (bars, meters, key signatures, repeats verified note-for-note) | yes |
| `derived-validated` | deterministic pass over the notes (per-bar chords) | yes, with "inferred" wording |
| `heuristic` | a guess (energy-based "climax", windowed tonal areas) | **never** |
| `performance` | needs grading actual playing | only with a grader wired |

For the Chopin fingerprint: 858 score-exact, 210 derived, 3 heuristic (excluded).
When the analyser's section basis is the heuristic rather than the score's key
signatures, *every* section and key-change question is gated out automatically.

**B. Post-generation verification** (`grounding.py`). Before anything is spoken,
the draft is scanned for claims — bar numbers, key names, note names, chord
tokens — and each must appear in evidence a tool actually returned this session.
Policies: `block` (drop the offending sentence, retry once), `hedge` (say it and
flag uncertainty), `warn` (log only).

Two subtleties that stop it crying wolf, both learned by watching it fail:

* a **question is not an assertion** — "What is the chord in measure 25? Is it
  Am, E7 or Dm?" names candidate answers, so only the bar reference is checked;
* **chord tokens are matched before notes**, so `E7` is read as a chord, not as
  the note E in octave 7.

```
$ python -m tutor.cli --demo-grounding
  "The A major section starts at bar 25."      -> UNGROUNDED [('bar','25')]
        policy=block -> None                       (dropped entirely)
  "The first A major stretch begins at bar 53." -> grounded
```

---

## 6. The tool surface

The model chooses *what to ask about*; it can never choose a fact.

| Tool | Purpose |
|---|---|
| `get_piece_map` | bar count, key, key areas, verified repeats, provenance |
| `get_bar` | one bar: chord, top note, note count, rhythm, contour, key area |
| `next_question` | draw a gated question (never returns the answer) |
| `grade_answer` | grade loosely-phrased answers, explain, update mastery |
| `compare_played` | pitch content of the played passage vs the score |
| `session_status` | attempts, accuracy, weak spots |
| `set_mode` | mode switching on request |
| `practice_render` | MIDI of a bar range at a steady tempo |

Answer normalisation is code, not prompt: `53`, `m53`, `measure 53`,
`measure number 53` all grade correctly; `up` == `rises`; `Q Q E E` ==
`q-q-e-e`; `Bb4` == `A#4`; `A minor` == `a minor` == `A`.

---

## 7. Latency budget

Target ~700–900 ms from end-of-speech to first tutor audio. Budget for
self-hosted components:

| Stage | Approach | Budget |
|---|---|---|
| VAD + endpointing | local, frame-level | 50–150 ms tail |
| ASR | streaming partials, 2-pass (fast partial, accurate final) | 150–300 ms |
| Tool calls | local JSON/SQLite, ≤2 per turn | 0–50 ms |
| LLM | token streaming, stable prefix for KV-cache reuse | 200–400 ms to first token |
| TTS | sentence-level streaming, cancellable | 150–250 ms to first audio |
| Transport | WebRTC/Opus | 20–60 ms |

ASR and TTS dominate, not the LLM — so prefer a 7–14B model with good tool
calling over a slower larger one. The system prompt and tool schemas are a
stable prefix; keeping them byte-identical across turns is the difference
between ~200 ms and ~2 s to first token.

Music analysis never blocks the voice path: `compare_played` is deterministic
and local, and real transcription would run on a worker with lower priority.

---

## 8. Self-hosted deployment

| Role | Options | Rough VRAM |
|---|---|---|
| ASR | faster-whisper (distil/large-v3-turbo), Parakeet-TDT, sherpa-onnx (CPU) | 1–3 GB |
| TTS | Kokoro-82M, Piper (CPU), XTTS-v2 / F5-TTS for a cloned voice | 0.5–3 GB |
| LLM | 7–14B instruct, 4-bit, function calling | 5–16 GB |
| Note tracking | MIDI keyboard (free) · basic-pitch / Onsets-and-Frames / MT3 | CPU–4 GB |
| Alignment | the DTW machinery in `note_seq/alignment` | CPU |

One 24 GB GPU is comfortable with MIDI-in and a 14B 4-bit model. Put real-time
ASR/TTS and background transcription behind a priority queue — a 3-second
transcription burst must not spike voice latency. Recommended shape: a small
appliance at the piano (mic array, speaker, MIDI interface) streaming WebRTC to
a GPU host elsewhere, which keeps fan noise away and swaps models freely.

**Full-duplex speech-to-speech** is tempting but wrong for the spine: S2S models
are weaker at tool calling and factual grounding, which is precisely what a
tutor must not be weak at. Use ASR → text LLM with tools → TTS for tutoring, and
optionally an S2S model for rapport-only chat.

---

## 9. ASR integration notes (from a live server)

Tested against `ahmetoner/whisper-asr-webservice` v1.10 on a LAN GPU box. Four
things came out of it that shaped the code:

1. **There is no single "Whisper API".** That server exposes only `POST /asr`
   (multipart `audio_file`, query parameters `output`, `language`,
   `initial_prompt`, `vad_filter`, `word_timestamps`) plus `/detect-language`.
   It has no `/v1/audio/transcriptions`, so an OpenAI-shaped client silently
   targets a route that does not exist. `detect_asr_api` reads the server's
   `/openapi.json` and picks the right client; `asr_api` can force either.
2. **Wrong shape, wrong diagnosis.** Posting the OpenAI shape there resets the
   connection instead of returning 404. "Server unreachable — start it" would
   send you debugging a server that is running perfectly, so the message names
   the endpoint tried and points at `TUTOR_ASR_API=auto`.
3. **Biasing works and it matters.** `initial_prompt` is where `asr_hotwords`
   goes on that server, and it changed "measure fifty three" into
   "Measure 53" — numerals, ready for the grader.
4. **Confidence must degrade honestly.** The server reports no `confidence`
   field (it is derived from `avg_logprob`) and returns an *empty* transcript
   for music, with no hallucinated words. An empty transcript now reports
   confidence `0.0`, not `1.0`, so "I recognised nothing" can never look like
   "I am certain" to the turn policy. `no_speech_prob` is surfaced too: in a
   room with a piano, plenty of audio is not speech.

Latency measured on that box: a 5.6 s clip transcribed in **0.09 s** (0.02×
realtime), i.e. ASR will not be the bottleneck — TTS and the LLM are where the
milliseconds go.

## 10. Known limitations (and the fix for each)

| Limitation | Consequence | Fix |
|---|---|---|
| `compare_played` works on pitch *classes* over a bar range | one altered note inside a dense passage can pass; verified in the demo | store per-bar note lists (with onsets) in the fingerprint, then compare per note |
| No timing analysis | the tutor never comments on rhythm or hesitation | DTW/HMM alignment of onsets to the score (`note_seq/alignment`) |
| The fingerprint stores distinct pitches per bar, not note lists | note-count feedback is impossible (an earlier version claimed "49 of 67 notes" — a false comparison) | extend the fingerprint schema |
| No acoustic echo cancellation | TTS bleeds into the instrument channel and looks like notes | AEC using the TTS reference signal, or headphones, or separate mics |
| Energy VAD | false triggers in a noisy room | Silero VAD / webrtcvad |
| ASR on musical vocabulary ("B double flat", "measure fifty-seven") | misheard bar numbers derail a lesson | hotword biasing (already plumbed via `asr_hotwords`) plus echoing numbers back for confirmation |
| A pending question swallows any utterance as an answer | "measure 53" was graded as an answer instead of treated as a question | intent detection: if an utterance references a bar the pending question is not about, treat it as a query |
| No streaming LLM/TTS path yet | latency is higher than the budget above | token streaming + sentence-level TTS chunks |
| Scheduler is a weak-spot heuristic, not spaced repetition | a miss returns too soon or too late | SM-2/FSRS over question and unit |

---

## 11. Testing

`tests/` covers the load-bearing parts (52 tests, no model, no audio — the ASR
parser tests use payloads captured verbatim from a live server):

* **`test_modes.py`** — barge-in cancels speech; empty speech is not a turn;
  playing over the tutor ducks it; LISTEN_COACH waits out the coach delay and
  never comments while music is active; a played phrase answers a drill question.
* **`test_grounding.py`** — claims are ungrounded before any tool call; observed
  facts become citable; question options are not assertions; "I am" is not the
  chord Am; `E7` is a chord not a note; block drops only the offending sentence,
  hedge flags uncertainty, warn passes through.
* **`test_tools.py`** — heuristic facts are never quizzable; section questions
  follow the section basis; chords are derived, not exact; performance tasks are
  opt-in; loose answer phrasings grade; the same question is not re-asked
  immediately; grading feeds weak spots; playing comparison finds missing pitch
  classes and never claims timing.
* **`test_asr.py`** — parses the exact payload a live whisper-asr-webservice
  returns; derives confidence from `avg_logprob`; empty transcripts are not
  confident; multipart bodies carry the right field names; the biasing prompt is
  URL-encoded; API shape detection reads `/openapi.json` and falls back safely; a
  connection reset is not reported as "server down"; WAV resampling produces the
  right number of 16 kHz samples.

The philosophical point: the properties that matter here (an honest tutor, sane
turn taking) are *testable* because they live in code rather than in a prompt.

---

## 12. Phasing

| Phase | Scope | Why first |
|---|---|---|
| **0** (this skeleton) | conductor, mode machine, fact gate, tools, grounding, stub + real clients | validates grounding and turn taking without a GPU |
| **1** | real ASR + streaming TTS, half-duplex with push-to-talk | reliable voice loop, immediately usable |
| **2** | barge-in, full duplex, floor pedal, per-bar note lists, DTW timing | real performance feedback |
| **3** | audio piano transcription (drop the MIDI keyboard), AEC | works on an acoustic piano |
| **4** | FSRS scheduling, repertoire planner, review mode, voice persona | long-term progress across pieces |

Phase 1 is where a self-hosted stack earns its keep: `TUTOR_LLM`, `TUTOR_ASR`
and `TUTOR_TTS` are the only changes needed to go from the scripted demo to a
real conversation.
