"""The note-seq web service: upload a score and a recording, then read and hear it.

    service/app/
      config.py    paths, limits, which tools this machine has
      db.py        SQLite: the jobs table, the pieces table
      jobs.py      the queue, its leases, and what a worker may write
      pipeline.py  the analysis: recording -> reference MIDI -> bars -> align -> engrave
      pieces.py    what a finished analysis wrote, and how the API reads it back
      worker.py    claim a job and run it (in this process, or on its own)
      api.py       the HTTP API and the built web page

Run `python -m service.app --help` from the repository root, or `./service/run.sh`.
"""
