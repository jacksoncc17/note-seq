# service — a score and a recording, in a browser

Upload a MusicXML score and a recording (a file, or a YouTube URL). The service lines the
two up — every bar gets a time in the recording — and serves a page showing the engraved
score where you can **pick any range of bars and listen to exactly those bars**.

A one-page introduction for a general audience — what it does, how, and what it is not —
is `project-page.html`. It is a single self-contained file: no build, no external assets,
so it opens from `file://`, a static host, or GitHub Pages as it is.

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
cd /home/wei/music_gen/note-seq
./service/broker/run_worker.sh            # foreground, and it prints what it will use
./service/broker/worker-ctl.sh start      # background, with a pid file and a log
```

`run_worker.sh` is the worker itself; `worker-ctl.sh` is the thin layer around it
(`start`, `stop`, `restart`, `status`, `log`, `services`) that keeps the process in
`service/data/worker.pid` and its output in `service/data/worker.log`. Settings come from
`service/broker/worker.env` (copy `worker.env.example`), so a restart is one command and no
secret is retyped. The controller sends SIGTERM and *waits* rather than killing: taking a
worker down mid-alignment loses that alignment and leaves the job claimed until the broker's
lease expires, so `stop` says so and leaves `kill -9` to you.

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

## Serving on its own (a container without the analyzer)

The page can be deployed somewhere the analysis never runs — a small container behind the
broker's reverse proxy, say — and that is only possible because the *serving* half needs
none of the heavy stack. Measured: `create_app()` imports no analyzer, no verovio, no
librosa, no fluidsynth, and a serving environment is **109 MB over 20 packages**
(fastapi, uvicorn, python-multipart, soundfile, numpy and their dependencies). The
analyzer's own stack is another gigabyte on top, and it belongs where the *worker* runs.

Two functions used to be imported from the analyzer at request time — `slice_file` for
the clip and `SyncResult.range_for_measures` for bars-to-seconds — which would have made
a slim container serve every page and then fail with an `ImportError` on the audio. Both
now live in `service/app/clips.py`, and `test_serving_a_piece_needs_no_analyzer` proves
the point the blunt way: it makes *every* import of `analyzer` raise, then asks the API
for a piece, an engraved page and a clip of a bar range. Mutation-checked: putting the
analyzer import back makes it fail.

`AnalyzerConfig`, `measures`, `scoreview` and `align` are still reached for on the
analysis path (`pipeline.analyse`), which is where they are needed and where the worker
runs; `SERVICE_EMBEDDED_WORKER=0` keeps the serving container from starting that loop.

### Building it

```bash
docker build -t note-seq:local service/                 # 226 MB, two stages
docker run --rm -p 8100:8000 -v note-seq-data:/data note-seq:local
```

226 MB is what `docker images` reports; measured by layer, 98 MB of it is the web layer as
pip installs it, 7 MB is libsndfile, and the rest is the interpreter. The node stage that
builds the page is not in the final image -- only its `dist/` is (401 kB).

The build context is `service/` alone, which is the point: the analyzer's 236 MB cache, the
100 MB `cyolo_score_following` clone, the venv and the piece data are not in it and never
reach the builder. `.dockerignore` says so as well for callers who pass the repository root.

Two bugs in this deployment were found by building the image and using it, and both were
invisible from the LAN:

* **`soundfile` was missing from `requirements.txt`.** The comment there said the rest came
  from the checkout's venv, which is true on the machine the service was developed on and
  false in a container. The image served `/`, the piece list and every engraved page, and
  answered 500 for every clip -- `ModuleNotFoundError: No module named 'soundfile'`, from an
  import inside the function that slices the audio. `test_api.py` now fails if a module
  `service/app` imports is not in that file (mutation-checked: removing the line fails it).
* **The page's own JavaScript 404'd under a path prefix.** `StaticFiles` mounted at
  `/assets` plus `SERVICE_ROOT_PATH` is broken, and only there: `Mount.matches` sets the
  child scope's `root_path` to `/note-seq/assets`, `get_route_path` cannot strip that from
  the already-stripped `/assets/app-abc123.js`, and `StaticFiles.lookup_path` refuses the
  absolute path it is left with. The page loaded and came up blank. Everything under
  `dist/` is now served by one route that resolves the path against the build directory
  (and refuses paths that climb out of it), with a test for each. Empty
  `SERVICE_ROOT_PATH` answers 200 for the same request, which is why running on the LAN
  hid it.

## Deploying under the broker's origin

The arrangement this service was written for: the page is served at `${DOMAIN}/note-seq`
by the broker's own Traefik, so it shares the broker's **origin** -- and therefore the
broker's token, because `localStorage` is per-origin. A subdomain would be a different
origin and would need a login of its own; that is the whole reason for the prefix.

`service/deploy/compose.note-seq.yml` is an overlay that adds one service and one volume to
that stack and touches nothing else. Paste its two blocks into `compose.deploy.yml` (no
change to `deploy/deploy.sh`), or bring the stack up with a third `-f`. On the droplet:

```bash
git clone <this repository> ~/note-seq                 # beside the broker's checkout
cd ~/full-stack-fastapi-template && ./deploy/deploy.sh  # builds and starts everything
```

What the overlay sets, and why:

| setting | value | why |
| --- | --- | --- |
| `SERVICE_EMBEDDED_WORKER` | `0` | a worker here would claim jobs and fail every one of them; the worker runs where the analyzer, ffmpeg and the soundfont are |
| `SERVICE_READ_ONLY` | `1` | no submission form, and `POST /api/v1/jobs` answers 405 instead of queueing an analysis nobody runs |
| `SERVICE_TRUST_JWT` | `${SECRET_KEY}` | the broker's own key, so the token this browser already holds is accepted |
| `SERVICE_ROOT_PATH` | `/note-seq` | the public prefix, for the API's own docs and redirects |
| `VITE_BASE`, `VITE_API_BASE` (build args) | `/note-seq/`, `/note-seq/api/v1` | where the page is served from and where its API is |

Verified against the running broker, not against a description of it: no token gives
`401 {"detail": "sign in first"}`; the broker's own token gives `200` and the pieces; a
token signed with the repository's development key gives `401` (the deployed key and the
one in the repository's `.env` are different -- measured 9 characters against 10, which is
why the overlay takes it from the environment); an expired token gives `401`;
`POST /api/v1/jobs` gives `405 this service only serves finished pieces; submit an analysis
on the broker`. And through a local stand-in for Traefik's routing
(`service/tools/prefix_proxy.py`, which strips the prefix exactly as `stripPrefix` does):
the page renders, 18 click targets land within 0.019 px of the engraving, the clip for bars
1-18 is 43.51 s, and the playhead follows it -- all inside the **container**, not the
development server.

Configuring `SERVICE_TRUST_JWT` is what puts the gate up; leaving it empty keeps the
service open, which is right for loopback. `SERVICE_ALLOWED_SUBJECTS` lists **operators**
-- accounts that see every piece -- and you want it set to your own user id; without it,
pieces whose owner is unknown are visible to nobody, including you. `SERVICE_BROKER_LOGIN`
(default `/login`) is where a browser without a token is sent.

## Getting a finished piece to the page

The job runs where the analyzer is, and the page is served from somewhere else, so a piece
has to cross. It does that as one file: `PUT /api/v1/pieces/{slug}`, whose body is a zip of
exactly what the page reads -- `meta.json`, `map.json`, `geometry.json`, `audio.wav` and
`pages/NNNN.svg`. The score is *not* in it: serving never reads MusicXML, the engraving
already happened, and re-analysing is the worker's job. That is ~16 MB per piece, nearly all
of it the recording.

**Why a `PUT`, and why an outbox.** The hop is the one part of this system that can fail for
reasons nobody controls, so it is a queue rather than a step in the job:

* the plugin writes the piece locally and drops a marker (`data/outbox/<slug>.json`) **before**
  it tries to send it, so a job killed mid-send leaves a queued piece and never a lost one;
* the send is then attempted immediately, and anything that did not get through stays marked;
* the next job drains up to three older pieces on its way past, so a page that was down for an
  hour catches up on its own;
* `python -m service.app.publish` does it without waiting for a job (`--status`, `--watch`,
  `--to <service root>`), and `/api/v1/status` reports `outbox: N` -- which is also what the
  page shows in its header when there is a backlog.

Sending is idempotent by construction, which is what makes retrying safe: the same package
twice leaves the same piece, and the swap into place is two renames, so the page keeps
serving the old piece until the new one is complete.

What is *not* in a marker: the token. A marker is a file on a disk; the token is a capability
to publish to your site, so it is read from the environment at send time.

**Configuration**, on the machine that analyses:

| setting | what it is |
| --- | --- |
| `SERVICE_PUBLISH_URL` | the page to send to, as a *service root* -- `https://example.com/note-seq`. The same value as `NOTE_SEQ_VIEWER_URL`, and if it is unset the plugin follows that variable, so the link and the piece cannot drift apart (they did, once: the link said the droplet and the piece was written locally, so every link 404'd) |
| `SERVICE_PUBLISH_TOKEN` | what to send as. Falls back to `SERVICE_UPLOAD_TOKEN`, which is the same secret from the other end of the wire |
| `SERVICE_UPLOAD_TOKEN` | on the *serving* side: which tokens may publish. Comma-separated, so each machine can have its own and one can be revoked without rotating the rest. A signed-in user's broker token is also accepted, which is what makes `curl` enough |

On the serving side, `SERVICE_ACCEPT_PIECES=0` refuses uploads (405) for a mirror that
should stay as it is, and `SERVICE_MAX_PIECE_MB` (default 64) caps the body. Note that this
works in a `SERVICE_READ_ONLY=1` deployment on purpose: read-only is about the queue, and a
deployment whose whole job is to serve pieces must be able to receive them.

What is checked before anything is written, because an uploaded archive is a list of names
chosen by somebody else: the slug must match `^[a-z0-9][a-z0-9-]{0,63}$` (validated, not
slugified -- `slugify` lives in the analyzer, and this endpoint must not need it), every
member must be one of the five names above, `..` and absolute paths are refused, and the
declared uncompressed size is checked before anything is inflated. A rejected package leaves
no partial piece behind.

Measured end to end, against the container serving on loopback: a piece queued against a port
with nothing listening stayed marked with `attempts 1` and `Connection refused`; `--status`
printed it and exited 1; the same piece then delivered with `--to` and the token (`1 sent, 0
still waiting`, exit 0); the page reported `pieces 1`; and the clip for bars 1-18 from the
uploaded copy hashes **identically** (`242cfca44450d74f…`) to the clip from the machine that
analysed it.

## Whose piece is it

A piece belongs to the broker account whose job produced it, and you see the pieces you
own. That sentence is the whole model; what follows is how it is carried and what happens
at the edges.

**How it travels.** The broker's job row already knows its `owner_id`, and its own job list
is scoped by it, but nothing downstream did: the worker was never told, so neither the
plugin nor the piece could be. The chain is now `job.owner_id` → `JobClaimed.owner_id` →
`params['owner_id']` → `meta.json` → the package a worker uploads → the store that serves
it. Only the first step is a change to the broker, and it is three lines: see
`service/broker/patches/owner-through-the-worker.patch`. The owner travels *inside* the
piece rather than being looked up, which is what lets a page decide what to show without
asking anybody — and is why the upload token is what makes that statement trustworthy.

**What a reader sees.** `SERVICE_ALLOWED_SUBJECTS` used to be an access list: a valid token
for anybody else got a 403. It now lists **operators**, who see everything; every other
valid token sees the pieces it owns and nothing else, answering an empty list rather than a
refusal. That change is what makes a closed deployment possible — otherwise every student
would have to be on a list that means "sees everything" — and it is why a broker with open
sign-up is no longer a reason to keep the whole store private: a stranger who signs in and
asks for somebody else's piece gets the same 404 as one that does not exist. Whether a slug
is taken is not this service's business to disclose.

**Edges.** Three cases are worth knowing, and all three fail closed:

* **no owner** — every piece published before this existed, and anything analysed on a
  machine that was never told whose it is. Operators only. Claim them with
  `python -m service.app.manage --adopt-unowned --owner <sub>`, which writes one field into
  `meta.json` and changes nothing about the music. Run it on whichever machine holds the
  pieces: the store that serves them, or the one that is about to send them (with
  `python -m service.app.publish --owner <sub>` doing the same on the way out).
* **no gate** — `SERVICE_TRUST_JWT` unset means one person on loopback, and nothing is
  filtered. Ownership is recorded anyway, so the pieces carry it to wherever they are sent.
* **`SERVICE_OWNER`** — for an analysis that did not come from a broker job (the LAN page,
  `curl`): this machine stamps its pieces with that subject, so they show up for the right
  account on the page that serves them.

**What it is not.** Not sharing, and not roles: there is no way to let one account read
another's piece, and no notion of a teacher. Both are small additions on top of this — a
group table, or an access row keyed by `(piece, subject)` — but each is a different
decision, and this is the one that had to come first, because a piece that belongs to
nobody cannot be shared correctly either.

## An archive at home

The serving deployment keeps each piece on its own disk, which is the right place for it —
that is what makes a page click fast and keep working when your house is offline. The risk it
leaves is that the droplet's volume is the *only* copy. So the machine that analyses can also
write each finished piece to an S3-compatible bucket at home, and a fresh droplet can be
refilled from it:

```
worker (LAN)  ──publish──►  droplet     ← serves the page; disposable
     └─────────archive───►  Garage at home   ← master copy, ~10 MB per piece
```

Both destinations get **the same bytes**: one package (`pieces/<slug>.zip`, the format in
`uploads.py`), packed once. `service/deploy/garage/` runs that store in Docker on another
machine; `bootstrap.sh` there prints the four settings below.

| setting | what it is |
| --- | --- |
| `SERVICE_ARCHIVE_URL` | the endpoint, e.g. `http://garage.lan:3900`. Empty means off |
| `SERVICE_ARCHIVE_BUCKET` | the bucket, `note-seq` in the example deployment |
| `SERVICE_ARCHIVE_KEY`, `SERVICE_ARCHIVE_SECRET` | an S3 key with read+write on that bucket |
| `SERVICE_ARCHIVE_REGION` | default `garage`; it must match the store's own region or Garage answers `AuthorizationHeaderMalformed`, which reads like a credentials problem |

Archiving is **never on the request path** and never fails a job: the analysis already
succeeded, so a store that is down costs a log line and a missing archive entry, and the piece
is still on the droplet. It happens *before* the push in `publish_remote`, because the bucket
is the copy that survives losing the serving machine.

**Backfilling what predates the archive** — the archive is written when a piece is published,
so anything analysed before it existed is missing:

```bash
python -m service.app.manage --archive-all
```

Measured on the deployment this was written for: 15 pieces, 152.6 MB, **10.9 s** over the LAN.
Garage's own accounting afterwards said 15 objects / 145.5 MiB, and `du` on its data directory
said **112 MB** — zstd plus the fact that several pieces share one recording, whose blocks are
then stored once. Restoring all fifteen into an empty store gave back audio that hashes
identically.

Settings come from `service/broker/worker.env` when they are not in the environment — the same
file `run_worker.sh` sources — so `publish`, `manage --archive-all` and `manage --restore-from`
all find the same bucket without anything being retyped.

**Refilling a wiped deployment** — this is the whole point:

```bash
python -m service.app.manage --restore-from note-seq            # whatever is missing
python -m service.app.manage --restore-from note-seq --overwrite  # or everything
```

It lists `pieces/`, downloads what the local store does not have, and installs each one through
exactly the same path the upload endpoint uses — so a restored piece is byte-for-byte the
piece that was archived. Running it twice skips what is already there.

The client is `app/s3.py`: signature v4 over `urllib`, about four verbs. boto3 would add ~40 MB
to an image whose whole design is 20 packages, for `PUT`, `GET`, `HEAD` and `LIST`. Measured
against the real store in `test_archive.py::test_the_signing_works_against_a_real_store`
(opt-in, `SERVICE_ARCHIVE_URL=... pytest`), which is also how two of its own bugs were found:
`dict(response.headers)` loses the case-insensitive lookup, so a `HEAD` of a real object
reported zero bytes; and Garage answers `AccessDenied`, not AWS's `SignatureDoesNotMatch`, to a
wrong secret.

## Keeping yt-dlp current

A YouTube URL is the one input that depends on something outside this repository, and
`yt-dlp` is the part of it that decays: YouTube changes how it serves video every few weeks,
the package is patched within days, and a version that has drifted fails with something
cryptic in a job log. Three things make that a five-minute problem instead of an afternoon.

**It says when it is the likely cause.** A fetch failure whose message looks like a stale
version (`Unable to extract`, `Sign in to confirm`, `HTTP Error 403`, `nsig`, …) gets a
sentence appended, naming the installed version and the exact command:

```
could not fetch that YouTube URL: ERROR: Unable to extract player response
  --  yt-dlp 2026.8.19 may be out of date: YouTube changes and the fix is usually
      `/path/to/.venv/bin/python -m pip install -U yt-dlp`, then retry the job
```

A video that is genuinely unavailable (`Private video`, `This video is unavailable`) does
*not* get that sentence — sending somebody to upgrade over a deleted video wastes the time
they needed for the real problem. Both behaviours are pinned in `tests/test_tools.py`.

**Update it where it lives.** It is in the checkout's venv, so:

```bash
.venv/bin/python -m pip install -U yt-dlp     # what `manage.py --tools` prints, verbatim
.venv/bin/python -m service.app.manage --tools   # versions of python, ffmpeg, ffprobe, yt-dlp
```

Unattended, weekly is enough in practice (a `systemd` timer, or the cron line):

```bash
# ~/.config/systemd/user/ytdlp-update.{service,timer}
#   ExecStart=/path/to/note-seq/.venv/bin/python -m pip install -U yt-dlp
#   OnCalendar=weekly
```

A *pinned* version is the wrong trade here: it reproduces until the day YouTube changes the
next thing, and then reproduces a failure. Latest is what works; the pinning that matters is
of the recordings, not of the downloader.

**The recording is kept.** A fetched video is converted to `audio.wav` in the job directory
and stored with the piece, so YouTube is needed exactly once per piece — re-analysing,
listening, clipping and moving the piece to another host all work offline afterwards. If a
download is ever hopeless, an audio file of the same music uploads fine and produces an
identical piece; the URL only changes `meta.audio.source`.

## Housekeeping

Nothing here cleans up after itself, and two things grow without limit: **jobs** (a row with
its log, plus a directory holding the score and the recording that were uploaded — often
10-50 MB each) and **pieces** (~16 MB each). A third is `data/output/`, where the analyzer's
renderer leaves derived audio that nothing reads once the piece is written. Measured on the
machine this was developed on, after a handful of analyses: `data/pieces` **144 MB** over
nine pieces, `data/output/render` **30 MB**, and `data/jobs` empty — jobs that run through
the broker's worker do not create local rows.

```bash
python -m service.app.manage                          # what is on disk. Deletes nothing.
python -m service.app.manage --jobs done,failed --older-than 14 --delete
python -m service.app.manage --jobs all --keep 5 --delete
python -m service.app.manage --pieces serenade-3 --delete
python -m service.app.manage --keepers 3 --delete      # keep the three newest pieces
python -m service.app.manage --renders --delete        # derived renders, regenerable
python -m service.app.manage --scratch --delete        # debris from killed processes
python -m service.app.manage --outbox --delete         # queued sends that will never land
python -m service.app.manage --adopt-unowned --owner <sub>   # claim pre-ownership pieces
```

**Nothing is deleted without `--delete`**, and the summary is the same either way: this is
the one tool here that destroys work, and a dry run that costs nothing beats a confirmation
prompt that gets typed through. A `queued` or `running` job is refused unless `--force`,
because deleting one out from under a worker leaves it uploading artifacts for a job that no
longer exists; renders are refused while any job is queued or running, because that is where
the analyzer is writing.

The one genuinely irreversible thing is a job's directory: it holds the score and the
recording that were *uploaded*, and once those are gone the piece cannot be re-analysed.
`--older-than` is the flag that matters. Everything else is regenerable — a piece can be
re-analysed from its source, and a queued send can be re-queued with
`python -m service.app.publish --queue-all`.

Two things found while building it. `PieceStore.slugs()` used to count any directory holding
a `meta.json`, so a half-written piece appeared in the page's list as `waltz.building` for as
long as an analysis took — `write()` puts that file in place *before* it renames the
directory. And the `pieces` table in `db.py`'s schema is never written or read; the
filesystem is the truth, so deleting piece *files* is what removes a piece.

## Tests

```bash
.venv/bin/python -m pytest service/tests -q          # 160 tests, ~120 s
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
