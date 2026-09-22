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
../.venv/bin/pip install PyQt5 yt-dlp
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
  what is actually played. On controlled material with rubato this recovers bar
  onsets to about **±0.1 s** (one analysis frame).

  On a *real* recording it is rougher, and you should know the numbers: against a
  real 309.7 s YouTube performance of this very Chopin waltz, the fit landed a
  median 7.2 s away from the even division — which is musically plausible for a
  *Lento rubato* waltz, only 2.3% cumulative — but its *local* behaviour was
  erratic: implied bar lengths from 0.0 s to 7.2 s, with a 5.8 s jump between
  neighbouring bars. So treat an automatic map on real audio as a **starting
  point at the region level**, not a bar-accurate result. The app enforces
  plausibility on every map it loads (no zero-length bars, strictly increasing
  markers) and says so when it had to clamp, and the match cost is shown so a
  poor fit is visible instead of silent.

Then **correct it**: drag any green marker, or hover it and use ← / → (Shift for
fine steps). Moving a marker switches the map to `manual` and records that a human
touched it. This is not a fallback for a bad algorithm — every tool in this space
needs it, because fermatas, rolled chords, ornaments, page turns and ambiguous
repeats are exactly where automatic alignment is least reliable and most visible.

**Save map** writes JSON under `analyzer/output/sync/<slug>.json`.

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
    library.py              finds pieces, their scores, PDFs, recordings, maps
    ui/
      main_window.py        the window
      waveform.py           envelope + draggable measure markers
      player.py             sample-accurate playback of a bar range
      workers.py            download and alignment on background threads
  tests/                    pytest, head-less (QT_QPA_PLATFORM=offscreen)
```

## Status

**M1 and M2 are complete and verified against the real thing**: the window, the
piece list, the repeat-aware measure list, and the cached-audio download (a live
YouTube search and download of a 309.7 s performance was fetched, converted to
mono 22 kHz WAV and served from cache on the second call).

**M3 is in progress.** Working today: the average-tempo estimate, manual
correction by dragging (saved as `method: "manual"`), the sidecar JSON with
staleness detection, and a plausibility validator that every map passes through.
The chroma + DTW refinement is the remaining piece.

**M4**, planned next, replaces fighting the PDF for clickable bars: render the
authoritative MusicXML with `verovio`, which gives an exact page and rectangle for
every measure (verified on the Chopin: 204 of 204 measures), and keep the PDF
beside it as the edition you actually read.

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
