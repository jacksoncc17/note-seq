"""note_seq_align -- align a recording to a score, and hand back the timings, the passage and its sound.

This is the note-seq service as a plugin for the broker in
`selfCommentary/full-stack-fastapi-template`: the worker loads it from a plugin
directory, publishes its contract at startup (that is the whole registration), claims
jobs, and uploads whatever `run` returns.

    python3 worker.py --plugin-dir /home/wei/music_gen/note-seq/service/broker \\
        --service note_seq_align --broker $BROKER_URL --key $WORKER_KEY \\
        --name lan-note-seq

What it produces for a job, as downloadable artifacts:

| kind | what it is |
|---|---|
| `measure_map` | JSON: every bar with the seconds it starts and ends, how sure the fit is of each bar, and the fit's own numbers (match cost, how many bars had to be clamped) |
| `range_audio` | WAV: the recording trimmed to the bars asked for, with the same run-in and run-out the desktop app plays a passage with |
| `score_page` (and `score_page_2`, ...) | PNG: the engraved page a bar is on, with the requested bars highlighted |

The artifacts are files because that is what this broker hands a person: the jobs page
shows a download button per artifact, so a PNG of the passage, the WAV of it and the
JSON behind it are each useful on their own. The *interactive* page -- click bar 40,
click bar 83, hear it -- is not something an artifact can be; that is the standalone
service in `service/app`, which is a different front door to this same analysis.

The package directory and the plugin's `name` must both be `note_seq_align`: the loader
uses the directory name and refuses a mismatch, which is what turns "service silently
missing at runtime" into a startup error. The name is not `note_seq` because this
repository already has a package of that name (the melody library `memorization/` uses),
and a plugin that shadows it -- or is shadowed by it -- fails with a message that says
nothing about why.
"""

from .service import NoteSeqAlign, plugin

__all__ = ['NoteSeqAlign', 'plugin']
