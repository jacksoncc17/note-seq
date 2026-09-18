# Piano memorization drill (experiment)

A guided memorization experiment built on top of this repo's MusicXML parser.
Outside the note-seq library itself: everything here is throwaway experiment
code and does not modify the library.

## The idea

You hand over a piano piece as MusicXML. The tools parse it into a
`NoteSequence`, compute a ground-truth "fingerprint" of the piece (measures,
chords, melody, rhythm, dynamics, form), and generate a graduated question
bank. Then you get quizzed tier by tier — skeleton first, details later,
performance last — with weak spots re-asked.

| Tier | Focus | Example question |
|------|-------|------------------|
| 0 | Structure | "How many measures? Where does section B start? Where's the climax?" |
| 1 | Harmony | "What chord is in measure 7? What's the opening progression?" |
| 2 | Surface | "First note? Rhythm of measure 12? Does bar 5 rise or fall?" |
| 3 | Performance | "Play from measure 9, cold, to the end of its phrase." |

## Workflow

```bash
# 1. Analyze the piece -> output/fingerprint.json (+ a reference .mid to listen to)
.venv/bin/python memorization/analyze.py path/to/piece.musicxml

# 2. Read the map of the piece (add --measures for the full per-bar table)
.venv/bin/python memorization/quiz.py sheet --measures

# 3. Sample questions of a tier (answers shown; this is what drives the chat quiz)
.venv/bin/python memorization/quiz.py drill --tier 1 --count 8

# 4. Track how it's going
.venv/bin/python memorization/quiz.py record --id q012 --result wrong
.venv/bin/python memorization/quiz.py stats
.venv/bin/python memorization/quiz.py misses
```

Useful flags for `analyze.py`:

* `--spq 8` quantization resolution (steps per quarter note)
* `--repeat-threshold 0.95` bar similarity needed to extend a repeat (0.95+
  means "essentially the same music"; lower it to catch freer returns)
* `--repeat-min-len 4` fewest bars for two passages to count as a repeat
* `--key-window 8` bars per window for the tonal-area heuristic fallback
* `--steady-qpm 100` steady practice tempo (default: the piece's average
  tempo, which playback-oriented scores inflate)
* `--midi-only` rewrite just the reference MIDI files and exit (seconds, not
  minutes)
* `--no-performance` omit tier-3 piano tasks
* `--out <path>` fingerprint destination
* `--no-midi` skip writing the reference MIDI files

And for the study document:

```bash
.venv/bin/python memorization/quiz.py sheet --markdown output/study_map.md
```

## What is computed, and how

* **Bar numbers come from the score.** The parser flattens a score into timed
  notes and loses bar numbers, and its own time signatures are unreliable: for
  measures whose durations do not add up it inserts "fractional" meters (a
  Chopin waltz yielded `1439/1920`). So bars are read from the score's own
  `<measure>` elements and notes are aligned by counting note elements per bar
  (the parser emits exactly one note per non-rest element and never reorders
  them). Result: "measure 57" in a quiz means printed bar 57, with no tempo
  drift. If the counts fail to line up, the tool falls back to a tempo-derived
  grid and says so.
* **Chords** come from `chord_inference.infer_chords_for_sequence`, one bar at
  a time (each bar is re-notated as a tiny sequence with a constant tempo and
  the score's meter, so exactly one chord covers exactly one bar). Per-bar
  inference is used because whole-piece inference cannot run at all on scores
  with tempo or meter changes.
* **Rhythm** is described as inter-onset intervals in beat units (`Q Q E E`),
  taken from the score's own divisions. Onsets are grouped (a chord or three
  voices striking together count once), tied continuations are excluded, and
  the pattern always sums to one bar. Raw note durations cannot do this: in a
  multi-voice piano score they spill past the barline via ties and held voices.
* **Melody** is the highest sounding pitch per bar; **contour** compares the
  mean register of the first half of a bar's onsets with the second half.
* **Key** is estimated with Krumhansl-Schmuckler profiles over
  duration-weighted pitch classes, because engraved scores often omit
  `<mode>`: the waltz file above claims "C major" (no mode given) while the
  music is plainly A minor.
* **Form** is two-layered. Key areas are found by windowed key estimation and
  merged into sections (the waltz: A minor → A major → E minor → … → coda).
  Separately, repeated passages are found by comparing every pair of bars on
  harmony, bass motion, onset rhythm and melody pitch classes, then keeping
  maximal diagonal runs of similar bars. A 4-bar minimum and a 0.88 similarity
  threshold catch literal returns (e.g. m1-4 ≡ m9-12) without false positives.
* **Reference MIDI** is written twice: one file keeps every expressive tempo
  change, the other is flattened to a single steady tempo (total beats ÷ total
  time) for practising against a pulse. Time signatures and key signatures are
  sanitised in both, because the parser's invented meters cannot be expressed
  in MIDI.

## Known limitations

* Chord inference is a Viterbi guess over pitch content: reliable for real
  polyphonic piano writing, weak for monophonic or very chromatic passages. It
  reports one chord per bar, so a bar with two harmonies gets the first one.
* Repeat signs and voltas are not unrolled: the analysis covers the music as
  written, so bar numbers match the printed page but a performance with repeats
  takes longer.
* Ornaments, pedal marks and fingering are not interpreted.
* Percussion/unpitched MusicXML is not parseable (library limitation).
* Chord inference costs roughly 0.7 s per bar, so a 200-bar piece takes about
  two and a half minutes to analyse.
