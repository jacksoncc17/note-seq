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

**One connection per thread.** The API serves requests while the worker reports progress,
so both write to the same SQLite file — on *one* shared connection their transactions
interleave and SQLite refuses (`cannot start a transaction within a transaction`), which
killed the worker thread mid-job and left the queue at "queued" for as long as anyone
waited. Measured: four threads on one shared connection produced four errors in a few
hundred writes, and none on their own connections. The worker's loop also steps over a
failure and says so on stderr rather than exiting, because a worker that dies on one
transient error stops taking work silently.

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

## Registering with the broker in `selfCommentary` (a plugin service)

The reference stack in `/home/wei/playground/goGames/selfCommentary/full-stack-fastapi-template`
is a *broker* and a *worker*: a service is a plugin that declares a parameter schema,
runs one job, and returns artifacts. That is a different contract from the web app above
— there, a plugin's output is files, and the jobs page shows a download button per file —
but it is the same analysis underneath, so `service/broker/note_seq_align/` implements it
by calling `pipeline.analyse`, the function this service's own jobs call.

Registering it is one command and no change to their repository:

```bash
cd /home/wei/playground/goGames/selfCommentary/full-stack-fastapi-template
WORKER_KEY=... python3 worker.py \
    --plugin-dir /home/wei/music_gen/note-seq/service/broker \
    --service note_seq_align \
    --broker $BROKER_URL --key $WORKER_KEY --name lan-note-seq
```

The worker publishes the contract at startup (`POST /worker/services`) — that *is* the
whole registration: the service appears in their web app's submit form, rendered from
`param_schema`, with a file picker for the score and the recording. Run it with the
interpreter that has this repository's dependencies, not their worker's Python: the
plugin imports the analyzer, and the worker itself is deliberately stdlib-only.

**The contract has to satisfy the oldest broker you register with, not the newest.** A
broker refuses a schema using a keyword it cannot enforce, with a 422 listing what it does
accept, and the sets differ between revisions: this plugin's `accept` (a rendering hint
that makes the browser's file picker offer `.musicxml,.xml,.mxl`) is allowed by the
checkout's validator and *refused* by the deployment at `jinshiming.net`, which predates
it. So the vocabulary used here is the intersection — the file formats are named in the
field descriptions instead, which every broker renders as help text — and
`test_the_schema_only_uses_keywords_an_older_broker_accepts` pins it against that older
list rather than against the local one.

**Changing the plugin needs the worker restarted, not the app.** Two things make that so,
and both are their design rather than an accident: their loader imports a plugin once at
startup, so a running worker holds the old module in memory, and the submit form is built
from the contract published at that same moment — so a new parameter does not exist in
the form until the worker publishes again. (Their broker never runs service code at all:
"the implementation lives in the worker".) Restarting the app does nothing.
`worker.py --list-services --service note_seq_align` shows what a *fresh* worker would
load, which is the cheap way to check before restarting anything.

Artifacts behave differently: `store_artifact` upserts per `(job, kind)` and nothing
removes a kind a job no longer produces, so a job that ran before a change keeps the
artifacts it uploaded. Only jobs run by the restarted worker show the new set.

**The service is called `note_seq_align` for a reason.** Their loader imports a plugin by
its directory name, and this repository already contains a package called `note_seq` (the
melody library `memorization/` uses). A service named `note_seq` would shadow it, or be
shadowed by it, depending on `sys.path` order — and the failure reads as "no plugin
found", which says nothing about the cause.

### What one job returns, and who decides

Measured on the Chopin waltz (204 bars, 309.7 s of audio): **8.6 s**.

The default is the *result*, not the workings:

| artifact kind | file | measured |
|---|---|---|
| `range_audio` | `bars_40-83.wav` (2.63 MB) | 60.98 s: the passage plus the same 0.12 s run-in and 0.25 s run-out the desktop app plays it with |
| `page_url` | `open-the-page.txt` (0.3 KB) | where to open it: `http://localhost:8100/pieces/…`, with a line on how to use it |

The `include` field on the submission form adds the rest, because they are how the
result was arrived at rather than the result:

| choice | adds |
|---|---|
| `the passage and its page` (default) | — |
| `also the engraved score pages` | `score_page`, `score_page_2`, … — 1260×1382 px, one per page the passage touches, with the selected bars washed in colour |
| `also the measure map (JSON)` | `measure_map.json` (77.7 KB): every bar with its start and end, the per-bar confidence, the fit's own numbers, and the measure boxes of all nine pages |
| `everything` | both, plus `report.json` (1.7 KB): `"20 bars had to be clamped: the fit was erratic there, so their times are a plausible guess rather than a measurement."` |

**This is a display decision, forced by their model.** The broker's job dashboard lists
*every* artifact a job uploads, and an artifact has no visibility flag — `ArtifactPublic`
is `id, kind, filename, mime_type, bytes, sha256, created_at` (and `ARTIFACT_KINDS` in
`models_jobs.py` is a dead tuple, referenced nowhere). So the only way for something not
to appear in the dashboard is not to hand it over. Nothing is lost by that: the map, the
geometry, the engraved pages and the quality warnings are all in the piece the page
serves, which is where you go to look at them anyway.

Two consequences of the lean default, both deliberate:

* **A job always returns at least one artifact**, because a worker refuses a job that
  produced nothing (an empty result is not a result). With `include` at its leanest that
  can only be reached when there is no bar range *and* the page could not be published —
  and then the report is the one honest thing left, so it goes out.
* **If publishing was attempted and failed, the report always goes out**, because that is
  where the reason is; a silently missing link would be worse than a file explaining it.
  Switching publishing off (`NOTE_SEQ_VIEWER=0`) is a setting rather than a fault, so it
  explains nothing and writes nothing.

The wash is drawn *into* the SVG before rasterising, at the box the engraving itself
reported — the same boxes the browser puts its click targets on. Where it lands is
tested, not assumed: one test renders the page and checks that the middle of a selected
bar is tinted and the middle of a bar outside the passage is not, and that test was
mutation-checked (placing the wash one SVG level too high makes it fail, which is what
would happen to a highlight drawn in Verovio's outer pixel space instead of the
engraving's units).

`--fake-pipeline` works too: the plugin has a `fake_run` that writes the same kinds of
file without touching audio, which is how the broker's queue plumbing can be exercised.

### The artifacts come with a page

A job that returns only downloads answers "what did it produce?" but not "what does it
sound like?" — so when the plugin finishes it also **publishes the piece into this
service's own store** (`pipeline.publish_piece`, the same function this service's own
jobs call), and puts the link in `report.json`:

```json
"viewer": {"published": true, "slug": "chopin-from-the-broker", "bars": 204,
           "title": "Chopin, from the broker",
           "url": "http://localhost:8100/pieces/chopin-from-the-broker"}
```

Bring up this service (`./service/run.sh`) and that link is the interactive page: bar
40, bar 83, the clip, the playhead. Measured end to end just now — one broker job
(8.6 s, six artifacts) published `chopin-from-the-broker`, and the running service served
it: 204 bars in the map, bar 40 at 62.38 s, nine pages, a 60.98 s clip for bars 40–83.
`npm run check:page -- http://localhost:8100/pieces/chopin-from-the-broker` then drove it
in Chrome: 31 click targets within 0.016 px of the bars, the range set, the clip playing.

Two things follow from where the piece is stored, and both are deliberate:

* **The switch.** `NOTE_SEQ_VIEWER=0` turns publishing off, and `NOTE_SEQ_VIEWER_URL`
  (default `http://localhost:8100`) says where the page will be, for a worker and a page
  that are not on the same machine. A machine that only runs broker workers should not
  have a data directory quietly filling up.
* **Publishing can never fail a job.** The artifacts are the contract; the page is a
  bonus. If the store is unwritable the job still succeeds, `report.json` says
  `{"published": false, "reason": "OSError: read-only file system"}`, and everything else
  is exactly where it was. Tested.

A broker-published piece carries no `job_id` in its meta: the job lives in the broker,
and the worker does not pass its id into the plugin's parameters.

### What the two front doors share, and what they do not

Both call the same analysis and end in the same store, so the map, the geometry and the
page are identical either way. What differs is what a *result* is: the broker hands a
person files, so the plugin returns files — and now also leaves the page behind for what
files cannot express. The two run independently: the plugin writes into the worker's
scratch directory, into this service's cache, and (unless switched off) into its pieces.

## Tests

```bash
.venv/bin/python -m pytest service/tests -q          # 73 tests, ~100 s
cd service/frontend && npm run check:browser         # the whole flow, from an upload
cd service/frontend && npm run check:page -- http://localhost:8100/pieces/<slug>
                                                     # ... or a page that already exists
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

The broker plugin is tested against the *reference implementation's own code* rather than
against a description of it: `worker_plugins.loader.load_plugins` loads it, the broker's
`app.param_schema` validates its schema (a schema the broker cannot enforce is refused at
registration, so this decides whether the service can be published at all), and
`worker.process_one` — their dispatch, staging and artifact-upload path — runs a real job
with a broker that records instead of posting. Those tests skip when that checkout is not
on the machine; everything else runs regardless.

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
