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
press **Play**, loop it if you like. Above the transport there are chips for the
practice tracks: **both hands**, **left hand**, **right hand** (the score, synthesised)
and **♪ Performance** (the recording); with nothing tapped, Play plays the whole piece
in whichever is chosen. `?piece=…&from=40&to=83` is kept in the URL,
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
| `GET /pieces`, `GET /piece/<slug>` | piece list, and one piece with its map, played order, page geometry, and the practice tracks it offers |
| `GET /audio/<slug>?from=&to=` or `?from_bar=&to_bar=` | that passage as a WAV. Bars are answered from the map. |
| `GET /audio/<slug>?track=left\|right\|both` | a practice track, whole, or `&from_bar=&to_bar=` for a passage of it |
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

### Practice tracks: left hand, right hand, or the whole piece

Above the transport there are chips. **Both hands**, **Left hand** and **Right
hand** play the *score*, synthesised — and that is the only way to get one hand
alone, because separating the hands in a *recording* is source separation, which
nothing here does. The **♪ Performance** chip is the real recording, kept because it
is the reference for how the piece actually goes; it is offered only when one has
been fetched, so a piece with no recording shows the three score tracks and nothing
else.

The split is by the score's own `<staff>`, never by pitch: on the Chopin, 297 of
1912 notes would be misassigned by a "below middle C is the left hand" rule, because
the hands overlap in the middle — the right hand goes down to B3 and the left up to
E♭4. `synth.py` splits at the parse step, since the repo's MusicXML reader keeps
`<voice>` but drops `<staff>` (voices are per staff, not per hand; this score uses
five of them across two staves). Deleting the other staff's notes after parsing moves
nothing, because onsets are fixed while parsing — so all three tracks are exactly as
long as each other, **308.079 s** on the Chopin, and bar 53 is at the same instant in
every one of them.

The tracks are laid out in **strict time, at the tempo of the piece's own reference
MIDI** — which is already a steady render of the score, the one `align.midi_grid`
reads. So bar positions are arithmetic (`(bar − 1) × seconds_per_bar`) rather than a
fitted map, and the phone gets `seconds_per_bar` in `/piece/<slug>` and follows the
bar highlight from it. Measured on the real Chopin over HTTP, bars 40-43:

| track | audio | energy below middle C |
|---|---|---|
| left hand | 6.36 s | 64.2% |
| right hand | 6.36 s | 0.4% |
| both hands | 6.36 s | 35.6% |
| the performance | 4.88 s | 21.5% |

The two hands are *different* audio, not the same render relabelled, and they sum to
the whole to within 2.5% (the residue is FluidSynth's own mixing). Building all three
takes about 5 s for a four-minute piece and they are cached per score — a track is
named after the size and mtime of the score it came from, so an edited score is never
served as a stale render.

Pressing **Play** with nothing tapped plays the whole piece in the chosen track; that
is new, and it is what makes "whole piece playback" one of the three choices rather
than a separate control. A recording that was never aligned stays a dead end — but
the score tracks carry their own grid, so the phone falls back to one of those and
says which, instead of showing a greyed-out button.

## Sync playing: the score at the pace of the music

A third tab. The recording plays and the page keeps up — but **the music that has not
been reached yet is covered**, so it cannot be read off the screen in advance. It is a
recall aid rather than a viewer: you have to know what comes next, at tempo.

```
Sync playing  [▶ Play] [■] [loop]  view [one line ▾]  reveal [every beat ▾]
              play speed [1.00 x] [1x]  page speed [1.00 x] [0 ms]
              page 2 / 9 · line 2 / 4      bar 40 · beat 2 of 3 · 62.9 s
```

**One line at a time** is the default view, and it is not only tidier. On a whole page
the curtain had to reach up into the bottom of the line above to keep the next line out
of sight, and it could not do that cleanly: ledger lines and dynamics are drawn *outside*
the staves, lower than the noteheads the reveal knows about, so "cover everything from
here down" clipped music that had already been played. With a single system on screen
there is no line above or below — to cover, or to be revealed. The curtain is cut to the
line being shown, the label says `page 2 / 9 · line 2 / 4`, and the view moves down the
page line by line as the music does. `view: whole page` puts the page back.

The line is shown with air above and below for those ledger lines and dynamics — two
notehead heights, 23 px on this score — but *bounded by the neighbouring line's ink*:
on page 2 the next line's music starts 133–169 px below, so the air never reaches it,
and on a tightly-set score where it would, the air is reduced instead. The line below is
the future, and the one thing this view must never do is show it.

**Play speed** (0.40–2.00×) slows the *recording* down, next to the dial that lines the
*page* up with it. Pitch is preserved — a phase vocoder, `librosa.effects.time_stretch`
— because the point of practising slowly is to hear the same notes go by more slowly,
not transposed ones. Verified: a 440 Hz tone stays 440 Hz at 0.5×, 0.75× and 1.25×, and
on the real recording the same three seconds of music took **2.84 s of wall time at
1.00× and 5.60 s at 0.50×**.

Two things make it safe to use everywhere rather than only in this tab:

* **The rest of the app keeps counting in the recording's own seconds.** Only the buffer
  and the wall clock are stretched, and `_tick` multiplies the elapsed wall time back
  into recording time — so the measure map, the waveform playhead, the bar reveal and
  the whole-bar boundaries all keep working untouched. At 0.5×, 0.5 s of wall time is
  0.25 s of music, and the page follows the music.
* **It is a phase vocoder, so it is not free.** About 150× faster than real time: a
  passage is instant, a whole 300-second piece at 0.5× takes ~2 s to prepare (and
  ~55 MB in memory), so the status bar says *"preparing a 0.50x version…"* rather than
  looking hung. Stretched audio is remembered, so a loop pays for it once.
* Changing the speed mid-passage does not stop the music: it carries on from where it
  had got to, at the new pace. The setting is the player's, so the Recording and Score
  tabs play at it too — and say so in the status bar (`playing bars 40-43 at 0.75x`),
  since the dial itself lives here.

**The reveal unit is the score's own.** `scoreview.page_reveal` works out which
metrical unit every notehead begins at — quarters in 3/4, eighths in 6/8, taken from
the score's durations, not guessed from the picture — so the curtain moves note group
by note group as the bar plays. The combo switches it to **every bar**, which is the
honest fallback and what happens on its own where a bar's durations do not add up.
Measured: **624 bar-and-beat positions across all 9 pages leave zero future noteheads
visible**, and the curtain only ever moves rightwards.

Four things this cost, each of which was a wrong version first:

* **A `<chord>` in MEI is an event, not a stack.** Reading it the way MusicXML's
  `<chord/>` works — "same onset as the note before" — gave every chord in a bar the
  same beat. Chords advance the clock; the notes *inside* one share its onset.
* **A note's unit is `floor`, not `round`.** The last third of a triplet in the third
  beat of a 3/4 bar begins at 2.67 quarters, which rounds to beat 4 — a beat that does
  not exist, and which let a note past the end of every such bar stay visible.
* **Notes belong to a system through their measure, not through their ink.** A high
  note is drawn above the staff box and a low one below it, so testing ink against the
  band drops precisely the ledger-line notes: 319 future noteheads stayed visible on
  four pages that way.
* **Covering wins over clipping.** The two hands disagree about x order — in bar 7 the
  left hand reaches beat 3 at x=203 while the right hand is still on beat 2 at x=188 —
  so a boundary placed after the last *played* note lets a future note show. The
  boundary is put a fixed distance before the next note instead (1.25 notehead heights,
  measured: that is how far left of a notehead its own accidentals are engraved), which
  can clip a notehead that has just been played: 54 of 78,258, five badly. That is the
  right way round — the feature exists so the next notes cannot be read.

### When the page and the music disagree

The clock is the **measure map's**: bars are timed by the recording, and the units
inside a bar are taken as evenly spaced across the time the map gives that bar. That is
the one estimate here — a performance does not announce its beats, so a fermata or a
big rallentando *inside* a bar can put the reveal a fraction of a beat out, which is
what the every-bar mode is for. Worth knowing about this score: twelve of its bars hold
three and a half quarters under a 3/4 signature (fermata bars — the MusicXML says so),
so those bars have a fourth unit rather than the three the signature implies.

**But the deeper limitation is the map, not the reveal.** A DTW map fits bar boundaries
to a real performance, and it says so itself when it struggled: on this score it warns
that the fit was erratic in **20 bars** (raw bar lengths ran 0.09–7.24 s) and that those
markers were clamped to something plausible, and eleven bars come back with a
confidence under 0.5 — bars 38, 46, 48, 67, 85, 107, 110, 115, 156, 188 and 204. The
page follows the map, so **those are the places the page can slip**, by up to about a
second, and no amount of care in the reveal can fix times that are themselves guesses.

So the two clocks are separated and can be lined up by hand:

* **page speed** (0.25–4.00×) scales how fast the page advances against the music — the
  correction for a map that is uniformly too slow, which is what "the music is running
  ahead of the page" looks like;
* **nudge** (±3 s) shifts the page in time, for a constant lag — a map that is late
  everywhere, or the small delay of a 40 ms position tick.

Both are anchored at the start of the passage being played, so turning a dial mid-flight
does not jump you to another bar, and the result is clamped to the passage: at 4× the
page reaches the last bar and waits there rather than running off the end. The status
line names the setting whenever it is not 1.00×/0 ms, and says **“map unsure here”** on
the bars the map has no confidence in — which is the honest answer to "why did the page
slip just there?".

Measured on bars 40–43 of this score, 3.0 s into the passage: 1.00× shows bar 42 beat 3,
1.15× shows bar 43 beat 1, and a −250 ms nudge pulls the same clock back to bar 42
beat 2.

The dial is a compensation, not a cure. Where the map is unsure the right fix is to drag
those markers in the **Recording** tab (or re-align) — the tab now tells you which bars
they are as you reach them.

The covering is **painted**, not masked in the SVG, and that is not a shortcut: the
cards need element masking because Verovio's own note box is too tight to hide a single
note with, while the question here is a *region* — everything after the playhead — and
a rectangle of paper is exactly that. It goes on last, over the bar highlight, so a
highlighted bar cannot give away how much music is coming. Stopping uncovers the page
again so it can be browsed, and leaving the tab stops covering but keeps the music
playing.

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
question is on **both sides** by default: on the back it sits under the picture (the
score stays where it was on the front, so nothing jumps on reveal), which matters when
the card is used with the sound off. Untick **say it again on the back** for a card
that ends on the answer — how the back is written is not part of a card's identity
either, so changing your mind rewrites the card you have.

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

### Keeping the dialog responsive

The first version of the question field made the dialog unusable, and the reason is
worth writing down because it is a cost that does not show up in the code:
**the preview ran on every keystroke, it drew both sides of the card, and drawing a
side engraved the score.** Engraving a page of this Chopin takes ~300 ms and
rasterising it ~530 ms, so a side cost ~840 ms and a preview ~1.7 s — on *every*
keystroke, checkbox and tap. Measured: **2.3 seconds per character typed**, 1.8 s per
tap.

Three things fixed it, in order of how much they mattered:

* **A text field does not reach the preview at all.** The question, the voice, the
  label, *speak it*, *say it again* and the audio side only change the card's text, so
  they are wired to `_text_changed`, which updates the two things they do decide —
  whether there is anything to export, and whether the card will be spoken. Wiring them
  to `_preview` meant that typing a question read the piece's question bank (to ask
  what the suggested question was), re-encoded both preview images to PNG and set them
  on their labels again. **130 characters now cost 5 ms in total**; a keystroke was
  2.3 seconds.
* **Engrave each page once.** `passage_images` takes the page as engraved, rasterises
  it for the back, then hides the masked elements *in the same SVG text* and
  rasterises that for the front. The front and the back used to be engraved
  separately — three engravings where one will do, and two independently measured
  crops that could disagree about where the music is. Engravings are also remembered
  across calls, which is sound because masking edits the returned SVG rather than
  needing a new one.
* **Draw the state the user settles on.** A 120 ms single-shot timer coalesces a spin
  box drag or a burst of taps into one drawing, the last few states are kept so a tap
  and its undo cost nothing the second time, and pictures already on screen are left
  alone. Six taps in a row measure 1.1 s, the same as one.

Opening the dialog went 3.9 s → 1.2 s and exporting a card 2.5 s → 1.7 s. What is left
is the work itself: about a second to redraw after a tap, because the rasteriser walks
the whole page whatever it is asked to keep. Two things were wrong rather than just
slow, and the same rework fixed them: the preview's two sides were cropped from
separately measured regions (so the score could shift by a pixel or two when a card
was revealed), and **the preview never engraved fingerings at all** — it drew the
plain score, so the digits you had just entered were invisible, and the front looked
exactly like the back. The preview now calls exactly what `build_card` calls.

Masking a passage that spans two pages also failed outright — "the masks do not match
this engraving (page 2)" — because each page was checked for masks that could only be
on one of them. The check now asks whether the masks matched the engraving *anywhere*,
which is what it was always trying to say.

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
    scoreview.py            MusicXML -> page images with a rectangle per measure,
                            and where each notehead begins for the reveal
    ui/
      main_window.py        the window
      waveform.py           envelope + draggable measure markers
      score_widget.py       the engraved score: click a bar, mask a note, curtain
      sync_tab.py           Sync playing: reveal the score at the pace of the music
      card_dialog.py        author cards: rules, taps, questions, preview, export
      player.py             sample-accurate playback of a bar range
      workers.py            download, alignment and engraving on background threads
    cards.py                Anki cards: masks, crops, audio, questions, decks
    speech.py               Kokoro text-to-speech for a card's question
    synth.py                practice tracks: the score split by staff, rendered
    server.py               the HTTP API for the phone client
  web/
    index.html              the phone client: one self-contained page
  tests/                    pytest; the store contract suite runs against every adapter
```

## Status

**M1-M7 are implemented**, with 410 tests, 408 of which run by default (two are
opt-in: one needs the speech server on the network, one needs an audio device).

* **M1** the window · **M2** the cached recording · **M3** the measure map
* **M4** the engraved score view · **M5a** the phone client
* **M6** storage behind ports a database can implement
* **M7** Anki cards for a selected passage, exported as `.apkg`
* practice tracks on the phone: the score whole, or one hand at a time
* **Sync playing** in the window: the score revealed only as far as the music has got,
  with play speed (pitch-preserving) and page-speed/nudge dials

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
  FluidSynth plus a soundfont are needed to render practice tracks (and for the
  alignment tests that render the score); the tests that need them skip without them.
* **"no reference MIDI"** — alignment needs a machine-readable performance of the
  score, which the question bank provides. Build it with
  `.venv/bin/python memorization/analyze.py <score.musicxml> --name <slug>`.
  Until then, place bars by hand from the average-tempo estimate.
* **Practice tracks take disk** — the three tracks of a four-minute piece are 78 MB
  (27 MB each: 22 kHz stereo PCM, about 5 s to build). They live in `cache/tracks` and
  are rebuilt on demand, so deleting that directory costs nothing but the render.
  Playing a whole track streams all 27 MB to the phone; a tapped passage streams only
  its bars.
* **Fetch fails** — check `yt-dlp --version` (it needs updating regularly as
  YouTube changes) and that `ffmpeg`/`ffprobe` are on `PATH`. The error message in
  the dialog says which of these it was.
* **Playback silent** — `player.py` needs a working audio device; the status bar
  reports what went wrong instead of failing quietly.
