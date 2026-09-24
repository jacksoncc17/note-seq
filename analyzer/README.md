# analyzer — line a score's measures up with a recording

A Qt5 desktop tool that answers one question well:

> I am looking at bar 40 of this piece and listening to a recording of it — where
> exactly in that recording does bar 40 start?

Three inputs, one output:

| input | why |
|---|---|
| a **MusicXML / MXL** score | the authoritative measure list and their order |
| a **recording** (a YouTube link) | fetched once with `yt-dlp`, converted with `ffmpeg`, cached locally |
| a **PDF** of the edition | shown for reference — read the score *you* practise from |

The output is a **measure map**: for every measure, the time range in the
recording where it is played. The app then plays any range of bars, exactly.

## Quick start

```bash
cd analyzer
./run.sh check                 # what is installed, which pieces were found
./run.sh run                   # open the window
./run.sh score ~/sheet_music/Other_piece.mxl    # a score with no question bank yet
./run.sh test                  # the test suite
```

Reaching it: `PyQt5` and `yt-dlp` are the only additions to the project venv:

```bash
../.venv/bin/pip install PyQt5 yt-dlp verovio cairosvg
```

## The three steps in the window

**1. Piece** — anything under `memorization/output/pieces/`, the same banks the quiz
and tutor tools use, plus (via *Open score…*) any MusicXML file. The score the bank
was built from is found through the bank's own `source_file` field.

**2. Recording** — paste a YouTube link and press *Fetch audio*. The audio is
downloaded once, converted to mono 22 kHz WAV and cached under `analyzer/cache/audio/`,
so everything afterwards works offline. *Re-fetch* replaces it (and the app warns
that an existing map was fitted to the old recording).

**3. Line the bars up** — two ways, and you will usually use both:

* **Average-tempo estimate** — spreads the recording evenly across the bars. Be
  clear about what this is: measured against a real performance it is **~1–2 s
  wrong per bar**, and only a few percent of bars land within half a second. Fine
  for "roughly this region", useless for "this exact bar".
* **Align to recording** — chroma features plus dynamic time warping, fitted to
  what is actually played. The score side is rendered to audio with FluidSynth so
  both sides are compared with the same features; leading silence is trimmed and
  added back; a transposed recording is detected and corrected; and the result is
  clamped so no bar is given an impossible length.

  **How accurate is it?** Measured against performances whose truth is known
  exactly — a 32-bar score, rendered, with the note times rewritten through a
  known tempo curve, then aligned back to the untouched score:

  | performance | median error | p90 | worst bar | within 0.10 s | estimate, for comparison |
  |---|---|---|---|---|---|
  | dead straight | 0.024 s | 0.048 s | 0.058 s | 100% | 0.055 s |
  | 5% rubato | 0.031 s | 0.082 s | 0.100 s | 100% | 0.285 s |
  | 10% rubato | 0.028 s | 0.084 s | 0.096 s | 100% | 0.279 s |
  | 10% rubato, 5% slower | 0.036 s | 0.063 s | 0.097 s | 100% | 0.291 s |
  | 18% rubato, 8% slower | 0.042 s | 0.080 s | 0.118 s | 97% | 0.381 s |
  | +2 semitones, corrected | 0.045 s | 0.090 s | 0.138 s | 94% | (shift found exactly) |

  So on material that matches the score structurally: **tens of milliseconds,
  worst bars around a tenth of a second** — and about **ten times better than
  spreading the recording evenly**. At roughly 1.5 s per bar, 0.1 s is under a
  tenth of a bar: close enough to start playing on the beat, though a pianist
  will still hear it on a slow passage.

  Three caveats worth as much as the numbers. Those figures are *rendered* piano;
  a real recording adds pedal, room, dynamics and wrong notes, so expect worse.
  On the real 309.7 s YouTube performance of the Chopin waltz there is no ground
  truth to score against, and the honest report is plausibility instead: 20 of 204
  bars had to be clamped (raw bar lengths ran 0.09-7.24 s) and the fit sat a median
  3.15 s from the even division, which for a *Lento rubato* waltz is musically
  plausible (2.3% cumulative) but not verified. And an earlier version of these
  numbers was wrong: a first attempt warped the *waveform*, which shifts pitch as
  well as time, and a second compared against notated time when the renderer
  itself lags its own notation by 0.3 s when there is a lead-in. Both produced
  confident, incorrect figures until the fixture was checked.

  One thing that looks like a quality measure but is not: the DTW match cost.
  Calibrated here, an identical render scores 0.00, the same music with rubato
  1.69, the same music transposed 1.01, and *different* music 1.41 — a correct
  rubato performance scores worse than the wrong piece, because the number is
  dominated by path length. It is reported as a diagnostic and nothing gates on
  it; the checks that do discriminate are bar-length plausibility and whether the
  recording lasts about as long as the score implies.

Then **correct it**: drag any green marker, or hover it and use ← / → (Shift for
fine steps). Moving a marker switches the map to `manual` and records that a human
touched it. This is not a fallback for a bad algorithm — every tool in this space
needs it, because fermatas, rolled chords, ornaments, page turns and ambiguous
repeats are exactly where automatic alignment is least reliable and most visible.
Aligning again asks first, since it replaces those corrections.

**Save map** writes JSON under `analyzer/output/sync/<slug>.json`.

## The score view (M4)

The **Score** tab shows the engraved score, and **every bar is clickable**: click a
bar, then click another to select everything between them (a third click starts
again, Shift+click always extends); double-click plays one bar, `←`/`→` step bar by
bar, Ctrl+wheel or `+`/`−` zoom, and *follow playback* keeps the page under the music
as it plays. The tab has its own **▶ Play range** and **■**, so selecting bars and
hearing them does not need a tab change.

The bars come from the **authoritative MusicXML**, not from your PDF:

```
MusicXML --verovio--> SVG per page --cairosvg--> PNG --Qt--> the tab
                            |
                            +--> a page and a rectangle for every measure
```

Verovio is used because it hands back *structure*: with `svgBoundingBoxes` every
element gets a bounding box, so each measure's exact rectangle is known (the
*number* comes from the matching MEI `xml:id`). Those rectangles are what a click
is tested against, so clicking a bar plays the bar you clicked.

Two notes on that pipeline, because both cost real debugging time:

* **Qt cannot rasterise Verovio's SVG.** QtSvg fails to resolve Verovio's
  `<use xlink:href>` references into its `<defs>` glyph layer (the glyphs are
  `<g><path>` there, not `<symbol>`), and on this machine it did not merely drop
  glyphs — it segfaulted the process. cairosvg resolves them correctly, so Qt only
  ever draws a PNG.
* **Your PDF is still there**, behind *Open PDF*, and it is the edition you
  actually read. The rendered view is a *re-engraving*: page breaks, fingering and
  layout will differ from your copy, which is the price of getting exact measure
  coordinates.

Rendering takes about fifteen seconds for a 200-bar piece and is then cached under
`analyzer/output/render/`, keyed by the score's size and mtime; *Engrave again*
forces a fresh one. It needs `verovio` and `cairosvg`:

```bash
../.venv/bin/pip install verovio cairosvg
```

## On your phone (M5a)

```bash
cd analyzer
./run.sh serve                 # prints the URL to open
```

Then open `http://192.168.0.128:8090` on the phone (change the address to whatever
the machine prints). You get the same piece list, the same engraved score, and the
same measure map as the desktop — pick a bar, tap the last bar of the passage,
press **Play**, loop it if you like. `?piece=…&from=40&to=83` is kept in the URL,
so a bookmark or a home-screen icon reopens that exact passage, on the page that
contains it. The bar being played is outlined and the page turns with it.

**The audio comes from this machine, not from YouTube.** yt-dlp fetched the
recording once, on the desktop, and the map's timestamps are offsets into that
cached file — which is why `syncmap` fingerprints it. So `/audio/<slug>?from=&to=`
decodes the cache and returns *exactly* that passage as a standalone WAV: bars
40-83 of the Chopin comes back as 2.67 MB decoding to 60.608 s against the map's
60.610 s. Sample-accurate, no client-side seeking, and no media-fragment quirks on
iOS. The phone needs no internet and no yt-dlp.

| endpoint | what it returns |
|---|---|
| `GET /` | the client page (self-contained; no external assets) |
| `GET /pieces`, `GET /piece/<slug>` | piece list, and one piece with its map, played order and page geometry |
| `GET /audio/<slug>?from=&to=` or `?from_bar=&to_bar=` | that passage as a WAV. Bars are answered from the map. |
| `GET /audio/<slug>` | the whole cached recording |
| `GET /score/<slug>/pages.json` | page sizes and a rectangle per measure, in image pixels |
| `GET /score/<slug>/page-N.png` | one engraved page |

No TLS is needed here, unlike the tutor: browsers only insist on a secure origin
for the **microphone**, and this feature has none.

What the phone deliberately does *not* do is fetch a recording, align it, or
engrave a score. Those are one-time setup steps that want a keyboard and a
progress bar, so they stay in the desktop app — and the client says so plainly
when a piece has no recording or no map yet (`intermezzo-left — no map` in the
list) rather than appearing broken.

## Storage, and the way out to a database

Everything the app keeps lives behind four ports in `store.py`, so no caller knows
whether it is on a filesystem or in SQL:

| port | what it holds | the one thing to know |
|---|---|---|
| `PieceStore` | the catalogue: list/get/upsert/remove, with a `PieceQuery` | queries are objects, so `list()` can become a `WHERE` clause |
| `BlobStore` | bytes under an opaque key | `local_path()` exists because soundfile, Qt and Verovio insist on a filename; a database adapter answers it by materialising into a scratch dir |
| `MapStore` | the measure → time maps | the data that costs real time to produce |
| `RenderStore` | engraved pages | reproducible from the score, so a cache |

Two adapters ship, and both run the same contract suite in
`tests/test_store_contract.py`:

* **filesystem** (the default) reads today's layout unchanged — question banks,
  `cache/audio/`, `output/sync/`, `output/render/`, plus a new `output/library/`
  for curated metadata. No migration; your existing caches and maps keep working.
* **memory** keeps everything in a dict. It exists to keep the ports honest: with
  only a filesystem implementation, filesystem assumptions creep into the
  interfaces unnoticed. It is not a product feature.

**Switching to a database is one line.** `store.open_repository(cfg)` reads
`cfg.backend` (or `ANALYZER_BACKEND`), so the move is: write an adapter, register
it in that function, point the config at it. Nothing above `store` changes — the
window, the HTTP API and the phone client all go through `library`, which goes
through the ports. A new adapter is accepted by adding one line to the ADAPTERS
table in the contract suite; if it passes there, callers keep working.

Two behaviours worth knowing, because they differ between backends by design:

* **Scores are referenced in place, not copied**, by the filesystem adapter: the
  catalogue records where your MusicXML lives. Move it and the piece stays listed
  with an explanation, but its score is gone. A database adapter would import the
  bytes at `upsert` time and keep both — that choice is exactly what belongs behind
  the port, so callers only ever ask "has a score?".
* **The catalogue owns curation, the bank owns derivation.** Title, composer, tags
  and *hidden* are stored as a row (a sidecar here); bar count, key and the
  reference MIDI come from the question bank and are never overwritten by an
  `upsert`. That is the split a database would bake into columns.

With that in place, multiple pieces are ordinary: *Open score…* now registers a
piece (so a score you have never analysed still appears in both clients), the
desktop has a filter box, the HTTP API takes
`/pieces?q=&has_audio=&has_map=&tag=&limit=&order=`, and the phone client has a
search field that queries the same way.

## Anki cards for a passage (M7)

In the desktop app, the **Score** tab has an **Anki cards…** button. It opens on the
bars you have selected and builds a card for them:

```
front   the passage with chosen notes hidden,
        then the question — written, and spoken
back    the same passage, plus what the bank already knows:
        chords  C → E7 → Am → E      rhythm  Q E E E E, Q Q Q
        contour down, up             section B      bars 40-43
        and the recording of exactly those bars
```

**A question can be asked on the front, out loud.** The *Question* field is prefilled
with whatever the question bank already asks about those bars — "What is the chord in
measure 53?" comes from the bank's own 1080 questions, so the wording is already
written to be read aloud — and *suggest* re-asks for the current bars if you change
the range. Tick **speak it** and a clip of the question is attached to the front, so
the card can be answered with your eyes on the keys rather than the screen. The
question is on **both sides**: on the back it sits under the picture (the score stays
where it was on the front, so nothing jumps on reveal), which matters when the card is
used with the sound off.

A prefilled suggestion only has to *name every bar of the passage*. The bank's
questions are all about a single bar, so for bars 40-43 a single-bar question is
refused rather than silently asking about bar 40 and ignoring the rest — bar 53 alone
gets its chord question, and the 53-68 passage gets the bank's section question, which
does cover all of it. Otherwise the field falls back to "Play bars 40 to 43 from
memory."

Speech goes to a **Kokoro server on the LAN** (the one the tutor already uses,
`ANALYZER_TTS_BASE_URL`, default `http://192.168.2.174:8880/v1`, voice
`ANALYZER_TTS_VOICE`, default `af_heart`). **If it is not running the card is still
built**, without the clip and with a warning in the export result — losing a card
because a speech server is down would be the wrong trade. The clip is requested with
`stream: false` on purpose: Kokoro's streamed WAV header claims 2147483647 frames,
which some players cannot get past.

**Fingering** can be added too, in the same dialog: tick *tap notes to set the
fingering*, choose a digit (1-5) and tap the notes, or step through them with `◀ ▶`
and press *assign to this note* when the mouse is fiddly. Right-hand numbers are
engraved **above** the staff and left-hand ones **below**, and they are **hidden on
the front** and shown on the back — the card asks "what fingering would you use?"
and answers it. A note-by-note *undo* covers a mis-tap.

Fingerings are kept **with the piece**, so you enter them once and every card of
those bars picks them up; a card also records the ones it used, so re-exporting an
old card does not depend on the piece's current set. *clear fingerings* empties the
card's set.

Hiding works two ways, and you can combine them:

* **rules** — `everything` (pitches go, rhythm stays), `staves_only` (a bar left to
  fill in), `right_hand`, `left_hand`, `melody` (the top note of each onset),
  `inner_voices`, `first_onset`, `accidentals`;
* **tapping** — switch on *tap notes on the score to hide them* and click the notes
  you want gone.

Then **Export .apkg…** writes a deck you import into AnkiDroid, or **Save to the
phone library**, which keeps it and offers it for download in the practice client:
`/decks/<slug>` lists them and `/decks/<slug>/<name>/download` hands over the
package, so AnkiDroid can open it straight from the phone.

**Anki does the scheduling, and there is no server to run for it.** The intervals
and the review history live in Anki's collection, on your devices; we only author.
Multi-device sync is AnkiWeb, or Anki's own self-hosted sync server if you would
rather not use it.

A word on how fingering is added, because it is the one place the tool writes score
content: the numbers are injected into a **copy of the MusicXML held in memory**,
which Verovio engraves — so they are placed by the engraver (clear of stems and
slurs) instead of painted over the picture. Verovio takes the document as a string,
so **no file is written anywhere**, and your score is never modified. Verified: after
injection the measure count, note count and every pitch come back identical, and the
source file's bytes *and* mtime are untouched.

Two things worth knowing about how the hiding actually works, because the obvious
approach is wrong:

* **Masking hides the element in the SVG, it does not paint over the note.** The
  first implementation drew a white rectangle over each note's bounding box, and it
  did not work: Verovio's per-note box is a *staff-position* box, not a glyph box —
  measured on this score its median ink coverage is 1.4% and 109 of 221 boxes are
  empty — so the patch landed beside the note. Suppressing the element
  (`display: none`, which the rasteriser honours) is exact: hiding one note changes
  480 pixels in a local area, hiding all seven noteheads of bar 53 drops the ink
  from 10.3% to 8.7%.
* **A hidden notehead leaves its stem**, because stems and beams are separate
  engraved elements. That is usually what you want — the rhythm stays readable and
  the pitches are what has to be recalled — and `staves_only` clears those too.

Masks are stored as positions ("bar 53, left hand, 5th notehead"), never as Verovio
element ids, because those are regenerated on every engraving; verified that all 15
glyphs of bar 53 land on the same pixels after a fresh engraving. Card ids are
stable too, so **re-exporting a deck updates your cards rather than duplicating
them**. A card's identity is its passage and its masks — *not* its question — so
rewording a question updates the card you already have, with its review history,
instead of leaving a second copy behind in Anki.

## What the map knows about itself

A map is only as good as the recording it came from, so each one stores its
provenance and the app refuses to pretend otherwise:

* `method` — `linear`, `dtw` or `manual`. Only the last two count as trusted.
* the recording's **size and SHA-256**, plus the score's mtime, so replacing
  either is *detected* and reported as a stale map rather than silently
  mis-pointing at the wrong bars.
* `quality` — the DTW match cost, how far the alignment moved bars from the
  average-tempo guess, the analysis frame size, and any estimated key shift.
* `warnings` — anything the code was not willing to guess at.

## Details that matter musically

**Repeats.** Printed measure numbers are not playing order. `measures.py` expands
repeat barlines, first/second endings and D.C./D.S./To Coda/Fine into the order a
performance actually plays, and refuses to guess where the notation is ambiguous
(`time-only` repeats, a D.S. with no segno) — it warns instead. This is not a
detail: aligning a repeated performance against an unexpanded score was measured
at **+11 s median error**, i.e. useless, where the expanded version was ~0.1 s.
Your own Chopin waltz and Ponce intermezzo turn out to have no repeat notation at
all (204 and 70 measures, played once through), which the app reports plainly.

**Transposed recordings** are common on YouTube. Chroma alignment without a key
correction fails catastrophically on them (measured: 0% of bars within 0.5 s at
+2 semitones), so the aligner estimates the shift and corrects the *score* side,
reporting it as `key shift N st` so you can see it happened.

**Suspicious pairings** are flagged: if the DTW match cost is high the alignment
is more likely to be a different piece, a different arrangement, or an
introduction-only excerpt, and the app says so instead of returning a confident
map of nonsense.

**Leading silence** is trimmed before aligning and added back afterwards, because
an applause or a 2.5 s lead-in otherwise offsets every bar by exactly that much.

## What is deliberately *not* here

* **No OMR.** The MusicXML is authoritative, so reading the PDF with optical music
  recognition could only disagree with it — and even the best open-source OMR
  misreads roughly one token in four. The PDF is display-only.
* **No audio transcription.** Transcribing a recording to MIDI first throws away
  the measure structure you are trying to use, and is less accurate than aligning
  audio to the score directly.
* **No cloud services.** Everything runs locally.

## Layout

```
analyzer/
  run.sh                    launcher: check | run | score FILE | test
  analyzer/
    app.py                  entry point (--check, --list-pieces, --score, --piece)
    config.py               paths (ANALYZER_CACHE / ANALYZER_OUTPUT override them)
    schema.py               AudioSource, MeasureTime, SyncResult
    measures.py             repeat/ending/jump expansion -> performance order
    ytaudio.py              yt-dlp + ffmpeg -> cached WAV, with progress
    align.py                linear estimate, silence trim, chroma + DTW alignment
    syncmap.py              the sidecar JSON: save, load, staleness
    store.py                the storage ports, and the filesystem + memory adapters
    library.py              the domain view: a record plus paths, a map, warnings
    scoreview.py            MusicXML -> page images with a rectangle per measure
    ui/
      main_window.py        the window
      waveform.py           envelope + draggable measure markers
      score_widget.py       the engraved score: click a bar, mask a note
      card_dialog.py        author cards: rules, taps, preview, export
      player.py             sample-accurate playback of a bar range
      workers.py            download, alignment and engraving on background threads
    cards.py                Anki cards: masks, crops, audio, questions, decks
    speech.py               Kokoro text-to-speech for a card's question
    server.py               the HTTP API for the phone client
  web/
    index.html              the phone client: one self-contained page
  tests/                    pytest; the store contract suite runs against every adapter
```

## Status

**M1-M7 are implemented**, with 316 tests, 314 of which run by default (two are
opt-in: one needs the speech server on the network, one needs an audio device).

* **M1** the window · **M2** the cached recording · **M3** the measure map
* **M4** the engraved score view · **M5a** the phone client
* **M6** storage behind ports a database can implement
* **M7** Anki cards for a selected passage, exported as `.apkg`

Verified beyond the unit tests: a live YouTube search and download (309.7 s
recording, served from cache); a full align run through the real Qt worker threads
(204 markers, bars 40-83 → 62.38-122.99 s); the score tab driven end to end (9
pages, 204 measures clickable); the phone client's JavaScript executed in headless
Chrome; and, for the cards, the whole flow on the real Chopin — the dialog opening
on bars 40-43, the left-hand rule hiding 22 notes, a tapped note making it 23, a
169 KB `.apkg` saved, and that package downloaded over HTTP with its images, audio
and text intact. The spoken question was built against the running Kokoro server and
then read back out of the package: a 19,821-byte MP3 (`ID3` header) in `media`,
`[sound:question-…]` on the front after the picture.

## Troubleshooting

* **Nothing appears** — the window needs a display. On a headless box use
  `QT_QPA_PLATFORM=offscreen` for the tests, but the GUI itself needs a desktop.
* **"no reference MIDI"** — alignment needs a machine-readable performance of the
  score, which the question bank provides. Build it with
  `.venv/bin/python memorization/analyze.py <score.musicxml> --name <slug>`.
  Until then, place bars by hand from the average-tempo estimate.
* **Fetch fails** — check `yt-dlp --version` (it needs updating regularly as
  YouTube changes) and that `ffmpeg`/`ffprobe` are on `PATH`. The error message in
  the dialog says which of these it was.
* **Playback silent** — `player.py` needs a working audio device; the status bar
  reports what went wrong instead of failing quietly.
