"""The worker: claim a job, run it, report on it.

Two ways to run it, and the difference is only where the thread lives:

* **embedded** (the default) -- the API starts this loop on a daemon thread at startup,
  so `./run.sh` is one process and one command. Good for a laptop and for trying it.
* **separate** -- `python -m service.app.worker` claims jobs from the same database
  while the API stays free to serve. Good when an alignment takes long enough that you
  would rather the web server did not share its process with FluidSynth and DTW.

Both are the same code; the lease in `jobs` is what makes running both at once safe.
"""

from __future__ import annotations

import argparse
import os
import socket
import sys
import threading
import time
import traceback
from typing import Optional

from . import pipeline
from .config import Settings
from .jobs import Job, JobStore

# How often a running job pushes its lease out. Well under the lease itself (600 s by
# default), because the point is to renew long before it lapses -- a renewal that races
# the expiry is not a renewal.
RENEW_SECONDS = 30.0


class Worker:
  """Runs jobs one at a time, from the queue in the database."""

  def __init__(self, settings: Settings, name: Optional[str] = None,
               store: Optional[JobStore] = None):
    self.settings = settings
    self.name = name or ('%s-%d' % (socket.gethostname(), os.getpid()))
    self.store = store or JobStore(settings)
    self.last_renew = 0.0
    self.running = False

  # ------------------------------------------------------------------ one job

  def run_once(self, block: bool = False) -> bool:
    """Claim and run the oldest job. Returns True if there was one to run."""
    job = self.store.claim(self.name)
    if job is None:
      if block:
        time.sleep(self.settings.poll_seconds)
      return False
    self.running = True
    try:
      self.execute(job)
    finally:
      self.running = False
    return True

  def execute(self, job: Job) -> None:
    """Run one claimed job, recording its progress, its log and its outcome."""
    job_dir = self.settings.job_dir(job.id)
    self._log(job, 'claimed by %s (attempt %d)' % (self.name, job.attempts))
    self._report(job, 0.0, 'starting')

    def report(percent: float, message: str = '') -> None:
      self._report(job, percent, message)

    def log(line: str) -> None:
      self._log(job, line)

    try:
      outcome = pipeline.analyze(self.settings, job_dir, dict(job.params),
                                 report=report, log=log)
    except Exception as e:
      detail = '%s: %s' % (type(e).__name__, e) if not isinstance(
          e, pipeline.PipelineError) else str(e)
      log('failed: %s' % detail)
      log(traceback.format_exc(limit=6))
      self.store.fail(job.id, job.token, detail)
      return
    self.store.finish(job.id, job.token, outcome['slug'],
                      'ready: %s' % (outcome['meta'].get('title') or outcome['slug']))

  # ------------------------------------------------------------------ writing

  def _report(self, job: Job, percent: float, message: str = '') -> None:
    """Progress, plus a lease renewal every so often.

    A renewal that fails means the job has been taken from this worker -- the lease
    lapsed and somebody else is on it. There is nothing useful to do about that from
    inside the run, so it is noted in the log and the run continues; the guard in
    `jobs.progress` is what stops this worker's numbers from landing on the other
    worker's job.
    """
    if time.monotonic() - self.last_renew > RENEW_SECONDS:
      self.last_renew = time.monotonic()
      if not self.store.renew(job.id, job.token):
        self._log(job, 'this worker no longer holds the job (its lease lapsed)')
    self.store.progress(job.id, job.token, percent, message)

  def _log(self, job: Job, line: str) -> None:
    stamp = time.strftime('%H:%M:%S')
    self.store.append_log(job.id, job.token, '[%s] %s' % (stamp, line))

  # ------------------------------------------------------------------ the loop

  def loop(self, stop: Optional[threading.Event] = None,
           idle_seconds: float = 0.0) -> None:
    """Work until stopped; `idle_seconds` ends the loop once the queue stays empty.

    Reaping runs before every claim, so a job whose worker died is picked up by the next
    worker to come past rather than waiting for a human to notice.

    Every iteration is guarded, and a failure is reported and stepped over rather than
    ending the loop. A worker that exits on one transient error is worse than useless: it
    silently stops taking work, and the queue looks stalled for no visible reason. That
    is not hypothetical -- a shared database connection used to raise here under load,
    and the symptom was a job sitting at "queued" for as long as anyone cared to wait.
    """
    idle_since: Optional[float] = None
    while stop is None or not stop.is_set():
      try:
        self.store.reap()
        ran = self.run_once()
      except Exception as e:                  # noqa: BLE001 - the loop must outlive it
        print('[worker %s] %s: %s' % (self.name, type(e).__name__, e), file=sys.stderr,
              flush=True)
        traceback.print_exc()
        time.sleep(max(1.0, self.settings.poll_seconds))
        continue
      if ran:
        idle_since = None
        continue
      idle_since = idle_since or time.monotonic()
      if idle_seconds and (time.monotonic() - idle_since) > idle_seconds:
        return
      time.sleep(self.settings.poll_seconds)


class WorkerThread(threading.Thread):
  """The embedded worker: a daemon thread that dies with the API process."""

  def __init__(self, settings: Settings, store: Optional[JobStore] = None):
    super().__init__(name='service-worker', daemon=True)
    self.worker = Worker(settings, store=store)
    self.stop = threading.Event()

  def run(self) -> None:                    # pragma: no cover - a thread's own loop
    try:
      self.worker.loop(self.stop)
    except Exception:                       # a dead worker must not kill the server
      traceback.print_exc()

  def shutdown(self) -> None:
    self.stop.set()


def main(argv: Optional[list] = None) -> int:
  parser = argparse.ArgumentParser(description='Run the analysis worker on its own.')
  parser.add_argument('--name', default=None, help='worker name shown on the jobs')
  parser.add_argument('--once', action='store_true',
                      help='run at most one job, for tests and scripts')
  parser.add_argument('--idle-seconds', type=float, default=0.0,
                      help='exit after the queue has been empty this long')
  parser.add_argument('--data', default=None, help='data directory (default SERVICE_DATA)')
  args = parser.parse_args(argv)

  settings = Settings(data_dir=args.data)
  settings.ensure_dirs()
  store = JobStore(settings)
  worker = Worker(settings, name=args.name, store=store)
  print('worker %s: data=%s' % (worker.name, settings.data_dir), flush=True)
  if args.once:
    if not worker.run_once():
      print('nothing queued', flush=True)
    return 0
  try:
    worker.loop(idle_seconds=args.idle_seconds)
  except KeyboardInterrupt:
    print('\nstopped', flush=True)
  return 0


if __name__ == '__main__':                  # pragma: no cover
  raise SystemExit(main())
