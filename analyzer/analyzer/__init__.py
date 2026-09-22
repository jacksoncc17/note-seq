"""Analyzer: a Qt5 desktop app for mapping a score's measures onto a recording.

Three inputs, one output:

  MusicXML  -> the authoritative measure list (see `measures.py`)
  audio     -> a cached local WAV, fetched once (see `ytaudio.py`)
  PDF       -> the printed edition, shown for reference (see `ui/`)

and the output is a *sync map*: for every measure in the performance, the time
range in the recording where it is played (see `syncmap.py`, `align.py`).

The pieces themselves live in `memorization/output/pieces/<slug>/`, built by
`memorization/analyze.py`, so a piece is shared with the quiz and tutor tools.
"""
