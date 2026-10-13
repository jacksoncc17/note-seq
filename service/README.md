# service — a score and a recording, in a browser

Upload a MusicXML score and a recording (a file, or a YouTube URL). The service lines the
two up — every bar gets a time in the recording — and serves a page showing the engraved
score where you can **pick any range of bars and listen to exactly those bars**.

It is the analyzer's analysis, moved to a server: the same alignment, the same measure
map, the same engraving geometry. It is not a port of the Qt code but a reuse of it —
`service/app/pipeline.py` calls `analyzer.align`, `analyzer.measures` and
`analyzer.scoreview` directly, and the numbers below are the proof that the two agree.

```
┌ browser ────────────────────────────────────────────────────────────────┐
│  score + recording  →  job (queued → running → done)  →  score page     │
│                                                    ↓                    │
│  click bar 40, click bar 83  →  "bars 40–83 · 1:02–2:02 · 1:01"         │
│  ▶  the clip plays, the bar under the playhead is outlined in amber     │
└─────────────────────────────────────────────────────────────────────────┘
```

## Quick start

```bash
./service/run.sh                  # http://127.0.0.1:8100
```

First run installs the web layer (FastAPI, uvicorn) into the checkout's `.venv` and
builds the page with npm. After that it starts in about a second. What it needs on the
machine — `ffmpeg`, `yt-dlp`, the venv, `memorization/analyze.py` — is checked and
reported by `GET /api/v1/status`, and the page says so in a banner when something is
missing rather than failing at the end of a job.

Development, with the page hot-reloading and the API proxied:

```bash
cd service/frontend && npm run dev      # http://127.0.0.1:5173
./service/run.sh                        # the API and the worker, on 8100
```

## What it was measured to do

The Chopin waltz the analyzer's own tests use (204 bars, nine engraved pages, a real
piano recording of 309.7 s), uploaded through the API and analysed by the worker:

| step | measured |
|---|---|
| whole job, upload to stored piece | **6.9 s** (alignment 4.1 s of it) |
| the map it produced | **identical to the desktop app's**, bar for bar: 204 bars compared, worst difference **0.000 s**, same `normalized_cost` 0.7046, same 20 clamped bars |
| bar 40 / bar 83 / bar 204 | 62.38 / 121.46 / 305.08 s — the desktop app's own numbers |
| engraving | 9 pages, 204 clickable bars, page 1 is 561 KB of SVG |
| the clip for bars 40–83 | 60.98 s of audio for 60.61 s of music (the 0.37 s is the run-in and run-out the desktop app also uses) |
| a YouTube URL instead of a file | searched, downloaded 15.4 MB, converted, aligned in **4.2 s**, 9 pages, 350.1 s of audio |

And in a real browser (`npm run check:browser`, headless Chrome, the whole flow):

| what | measured |
|---|---|
| the flow | upload → job queued in 128 ms → done in 7.4 s → click the job link → `/pieces/…` |
| click targets vs the engraving | within **0.017 px** of where the SVG draws each bar |
| the passage | bars 40–83 → "1:02–2:02 · 1:01 of music" |
| the clip | 60.978 s, playing: `currentTime` 2.500 s after 2.5 s |
| the playhead | reports bar 42, outlines it, and offers the page it is on |

Two of those numbers came from bugs the browser check found, which is the reason it
exists: Verovio wraps its engraving in an outer `<svg>` with a fixed pixel size and no
`viewBox`, so the music was letterboxed 230 px inside the box the click targets were
positioned in — **every bar was clickable and none of them were where they looked**. The
second was in the range logic: the end of a passage could never be set, because "no end
yet" and "the range is still open" were the same test.

## How it works

| file | what it does |
|---|---|
| `app/config.py` | paths, upload limits, which tools this machine has |
| `app/db.py` | SQLite: `jobs` and `pieces`, WAL, `BEGIN IMMEDIATE` for claiming |
| `app/jobs.py` | the queue and its leases: progress and results from a worker that still holds the job |
| `app/pipeline.py` | the analysis: recording → reference MIDI → bar order → align → engrave |
| `app/pieces.py` | what a finished analysis wrote, and how the API reads it back |
| `app/worker.py` | claim a job and run it — in this process, or on its own |
| `app/api.py` | the HTTP API, the clip endpoint, and the built page |
| `frontend/` | React + Vite + TypeScript + Tailwind + TanStack Query/Router |

The pipeline is the desktop app's, in the same order, calling the same code:

1. **the recording** — an uploaded file, or `yt-dlp` for a URL, converted by `ffmpeg` to
   22050 Hz mono PCM, which is the one format everything downstream is timed in;
2. **the reference MIDI** — `memorization/analyze.py --midi-only`, run as a subprocess
   with the checkout's own interpreter, exactly as the README tells a person to run it.
   It writes the steady-tempo performance the alignment compares against (120.28 qpm for
   this score);
3. **the bar order** — `analyzer.measures.expand_measures`: repeats, endings and jumps,
   so "bar 40" means here what it means in the desktop app;
4. **the alignment** — `analyzer.align.align_to_audio`, the DTW fit every map in this
   repository has been built with, reporting progress as it goes;
5. **the engraving** — `analyzer.scoreview.render_svg_pages` (added for this service):
   Verovio to SVG per page, with every measure's box in the SVG's own units. No
   rasteriser is involved, because a browser draws the SVG itself.

### The queue

A job moves `queued → running → done|failed` and carries its progress, its log, and — when
it succeeds — the slug of the piece it produced, which is the link the UI shows.

Claiming a job takes a **lease**: a token and an expiry, written in the same transaction
that takes the job out of `queued`, and every later write from that worker is guarded by
the token. That is not decoration. An alignment can take minutes; if a worker stalls past
its lease, another worker takes the job, and without the guard the first one would come
back and overwrite a finished result with a stale one. `reap()` returns jobs whose worker
died to the queue, and fails the ones that have killed `SERVICE_JOB_ATTEMPTS` workers,
because a job that fails every time is not going to work on the fourth.

The worker runs **inside the API process** by default, so one command starts everything.
For a busy machine, or to keep FluidSynth and DTW out of the web server's process:

```bash
SERVICE_EMBEDDED_WORKER=0 ./service/run.sh
.venv/bin/python -m service.app.worker            # as many as you like, same database
```

### On disk

```
service/data/
  service.db                  jobs and pieces
  jobs/<id>/                  the uploads, the reference MIDI, the converted recording
  pieces/<slug>/
    score.musicxml  score.mid  audio.wav
    map.json      bar -> seconds, with confidences and the fit's own numbers
    geometry.json per page: the viewBox and every measure's box, in SVG units
    meta.json     title, composer, bars, duration, quality, warnings
    pages/0001.svg …
```

`map.json` and `geometry.json` are what the page is built on: the first says *when* each
bar sounds, the second *where* it is. Serving the score page is reading two small JSON
documents, whatever the piece's size.

## The API

| endpoint | what it is for |
|---|---|
| `POST /api/v1/jobs` | multipart: `score` (MusicXML), and `audio` **or** `youtube_url`; optional `title` |
| `GET /api/v1/jobs` | the queue, newest first — the list you click |
| `GET /api/v1/jobs/{id}` | one job: status, progress, log, and the piece it made |
| `POST /api/v1/jobs/{id}/retry` | put a failed job back in the queue |
| `GET /api/v1/pieces` | what has been analysed |
| `GET /api/v1/pieces/{slug}` | the score page's data: map, pages, measure boxes |
| `GET /api/v1/pieces/{slug}/pages/{n}.svg` | one engraved page |
| `GET /api/v1/pieces/{slug}/audio` | the whole recording (range requests supported) |
| `GET /api/v1/pieces/{slug}/audio?first_bar=40&last_bar=83` | **the clip: the music of those bars**, with `X-Clip-Start`/`X-Clip-End` saying where in the recording it begins |
| `GET /api/v1/status` | what this machine can do |
| `/docs` | the generated OpenAPI documentation |

The clip is answered from the **map**, not from a guess about tempo, so "bars 40–83"
means the same thing here as it does in the desktop app. The headers matter to the page:
clip time plus `X-Clip-Start` is a position in the recording, and that is what puts a bar
under the playhead without recomputing anything. `pad=0` turns the 0.12 s run-in and
0.25 s run-out off; the desktop app plays a passage with them because starting exactly on
the downbeat clips the piano's attack.

## What it deliberately does not have

* **No accounts, no Postgres.** One SQLite file and no authentication: this is a service
  for one person on one machine. The reference implementation in `selfCommentary` uses a
  Postgres queue with `FOR UPDATE SKIP LOCKED` because several machines pull from it; the
  *shape* here — jobs, leases, attempts, artifacts — is the same, the machinery is not.
* **The engraving is not compressed.** A page is ~550 KB of SVG because it carries
  Verovio's own bounding boxes, which is where the measure geometry comes from. Fetching
  nine pages is about 5 MB, once, and the browser caches them for a day.
* **No curtain, no practice tracks, no cards.** This is the three things that were asked
  for: the score, a range of bars, and the sound of them. The Sync-playing reveal and the
  hands-separate tracks stay in the desktop app.
* **One performance, one score.** The alignment is the analyzer's, with the same limits
  it documents: on this Chopin recording 20 bars had to be clamped because the fit was
  erratic there, and the page shows that warning rather than hiding it. A YouTube search
  may also find a different performance than the one you had in mind — the page shows
  which recording each piece was built from.
* **Nothing is deleted for you.** Re-analysing the same score with a new recording
  replaces that piece (matched by the score's sha256); a different score with the same
  name gets `-2`. The uploads stay in `data/jobs/`.

## Tests

```bash
.venv/bin/python -m pytest service/tests -q          # 47 tests, ~10 s
cd service/frontend && npm run check:browser         # the whole flow in headless Chrome
```

The pytest suite runs the real pipeline — real Verovio, real DTW, real ffmpeg — on real
material, because every step of it is the thing being tested: the map is compared
*against the desktop app's own map* for the same piece, bar by bar, and the clip is
compared *against the recording*, sample by sample. The two network tests (a YouTube URL
that resolves, and one that does not) are opt-in:

```bash
ANALYZER_NETWORK_TESTS=1 .venv/bin/python -m pytest service/tests -q
```

`npm run check:browser` starts the service on a spare port with its own data directory,
drives Chrome through upload → job → score page → range → playback, and prints what it
measured; it exits non-zero if the click targets, the clip or the playhead are wrong.

## Troubleshooting

* **"the web page has not been built yet"** — run `npm install && npm run build` in
  `service/frontend`, or use `npm run dev` and open port 5173. `run.sh` does the build on
  its first run if npm is available.
* **npm fails with `EROFS` on `~/.npm/_cacache`** — that is a sandboxed home directory,
  not npm: give it somewhere writable, `npm install --cache /tmp/npm-cache`.
* **"ffmpeg is not installed"** / **"yt-dlp is not installed"** — the form disables the
  part it cannot do, and `POST /api/v1/jobs` answers 503 with the reason. On Debian:
  `apt install ffmpeg`; `yt-dlp` comes with the checkout's venv.
* **A job sits at "queued"** — no worker is running. The embedded worker starts with the
  API unless `SERVICE_EMBEDDED_WORKER=0`; check the API's own output, and note that a
  worker launched by hand must be run from the repository root.
* **A job failed** — open the job's log in the page (the chevron on the row). It carries
  what each step was told to do and what the tools said back, which is usually enough to
  see whether the upload, the score or the alignment was the problem.
* **Port already in use** — `./service/run.sh --port 8101`.
