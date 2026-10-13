"""Jobs: the queue, its leases, and what a worker may write.

A job is one request to analyse a piece. It moves `queued -> running -> done|failed`
and carries its progress, its log and -- when it succeeds -- the slug of the piece it
produced, which is the link the UI shows.

The queue is the `jobs` table, claimed by taking a **lease**: a token plus an expiry
time, both written in the same transaction that takes the job out of `queued`. Every
later write from that worker is guarded by the token, so a worker that stalled past its
lease and lost the job to somebody else cannot come back and overwrite the result. That
guard is not decoration -- without it a slow alignment silently clobbers a finished job
with a stale one two minutes later.

Nothing here knows how to analyse anything. `pipeline` does the work; this decides who
runs it and records what happened.
"""

from __future__ import annotations

import json
import pathlib
import uuid
from typing import Any, Dict, List, Optional

from . import db
from .config import Settings

# How much log a job keeps. Long enough for the interesting part of a failure, short
# enough that polling a list of jobs never ships megabytes.
LOG_LIMIT = 20000


class JobError(RuntimeError):
  """The job cannot be created, or cannot be moved to the state asked for."""


class Job:
  """One row of `jobs`, with the few conveniences the worker wants."""

  def __init__(self, row: Dict[str, Any]):
    self.row = row
    self.id: str = row['id']
    self.status: str = row['status']
    self.params: Dict[str, Any] = json.loads(row.get('params') or '{}')
    self.slug: Optional[str] = row.get('slug')

  @property
  def token(self) -> Optional[str]:
    return self.row.get('lease_token')

  @property
  def attempts(self) -> int:
    return int(self.row.get('attempts') or 0)

  @property
  def error(self) -> Optional[str]:
    return self.row.get('error')

  @property
  def message(self) -> str:
    return self.row.get('message') or ''

  def __repr__(self) -> str:
    return '<Job %s %s %.0f%% %s>' % (self.id, self.status, self.row.get('progress') or 0,
                                      self.row.get('message') or '')


class JobStore:
  """Create jobs, claim them, report on them."""

  def __init__(self, settings: Settings, database: Optional[db.Database] = None):
    self.settings = settings
    self.database = database or db.Database(settings.database)

  @property
  def connection(self):
    """The calling thread's connection; see `db.Database`.

    A property rather than the attribute this used to capture in `__init__`, which pinned
    whichever thread built the store and handed that one connection to every other. The
    transactions were the part that broke loudly -- the reads looked fine only because
    WAL lets a reader work while somebody else writes.
    """
    return self.database.connection

  # ------------------------------------------------------------------ creating

  def create(self, params: Dict[str, Any], title: str = '', kind: str = 'analyse') -> Job:
    job_id = uuid.uuid4().hex[:12]
    with self.database.immediate() as connection:
      connection.execute(
          'INSERT INTO jobs (id, kind, status, progress, message, params, title, '
          'created_at) VALUES (?, ?, ?, 0, ?, ?, ?, ?)',
          (job_id, kind, 'queued', 'waiting for a worker',
           json.dumps(_jsonable(params)), title.strip(), db.now()))
    job = self.get(job_id)
    if job is None:                       # cannot happen; saying so beats returning None
      raise JobError('the job was not created')
    return job

  # ------------------------------------------------------------------ reading

  def get(self, job_id: str) -> Optional[Job]:
    row = self.connection.execute('SELECT * FROM jobs WHERE id = ?', (job_id,)).fetchone()
    return Job(db.row_to_dict(row)) if row is not None else None

  def list(self, limit: int = 50, status: Optional[str] = None) -> List[Job]:
    sql = 'SELECT * FROM jobs'
    args: List[Any] = []
    if status:
      sql += ' WHERE status = ?'
      args.append(status)
    sql += ' ORDER BY created_at DESC LIMIT ?'
    args.append(int(limit))
    rows = self.connection.execute(sql, args).fetchall()
    return [Job(db.row_to_dict(row)) for row in rows]

  def stats(self) -> Dict[str, int]:
    rows = self.connection.execute(
        'SELECT status, COUNT(*) AS n FROM jobs GROUP BY status').fetchall()
    counts = {status: 0 for status in db.STATUSES}
    for row in rows:
      counts[row['status']] = int(row['n'])
    return counts

  # ------------------------------------------------------------------ claiming

  def claim(self, worker: str = 'worker') -> Optional[Job]:
    """Take the oldest queued job, or None. Safe with several workers at once."""
    token = uuid.uuid4().hex
    expires = db.now() + self.settings.lease_seconds
    with self.database.immediate() as connection:
      row = connection.execute(
          "SELECT id FROM jobs WHERE status = 'queued' ORDER BY created_at LIMIT 1"
      ).fetchone()
      if row is None:
        return None
      updated = connection.execute(
          "UPDATE jobs SET status = 'running', worker = ?, lease_token = ?, "
          "lease_expires_at = ?, started_at = ?, attempts = attempts + 1, "
          "message = 'starting' WHERE id = ? AND status = 'queued'",
          (worker, token, expires, db.now(), row['id'])).rowcount
      if not updated:
        return None
    return self.get(row['id'])

  def renew(self, job_id: str, token: str) -> bool:
    """Push the lease out; a long alignment must not lose its job to a rival."""
    row = self.connection.execute(
        'UPDATE jobs SET lease_expires_at = ? WHERE id = ? AND lease_token = ?',
        (db.now() + self.settings.lease_seconds, job_id, token)).rowcount
    return bool(row)

  def holds(self, job_id: str, token: Optional[str]) -> bool:
    row = self.connection.execute(
        'SELECT lease_token FROM jobs WHERE id = ?', (job_id,)).fetchone()
    return row is not None and row['lease_token'] == token

  # ------------------------------------------------------------------ writing

  def set_params(self, job_id: str, params: Dict[str, Any]) -> bool:
    """Fill in a job's parameters once its uploads are on disk.

    The job is created before the upload is read -- so the id exists to name the
    directory with -- and the paths go in afterwards, in one write.
    """
    with self.database.immediate() as connection:
      row = connection.execute(
          'UPDATE jobs SET params = ? WHERE id = ?',
          (json.dumps(_jsonable(params)), job_id)).rowcount
      return bool(row)

  def progress(self, job_id: str, token: Optional[str], percent: float,
               message: str = '', log: Optional[str] = None) -> bool:
    """Record progress. Ignored (returns False) if the lease has moved on."""
    percent = max(0.0, min(100.0, float(percent)))
    with self.database.immediate() as connection:
      if log:
        row = connection.execute('SELECT lease_token, log FROM jobs WHERE id = ?',
                                 (job_id,)).fetchone()
        if row is None or row['lease_token'] != token:
          return False
        connection.execute('UPDATE jobs SET progress = ?, message = ?, log = ? '
                           'WHERE id = ? AND lease_token = ?',
                           (percent, message, _appended(row['log'], log), job_id, token))
        return True
      row = connection.execute(
          'UPDATE jobs SET progress = ?, message = ? WHERE id = ? AND lease_token = ?',
          (percent, message, job_id, token)).rowcount
      return bool(row)

  def append_log(self, job_id: str, token: Optional[str], line: str) -> bool:
    with self.database.immediate() as connection:
      row = connection.execute('SELECT lease_token, log FROM jobs WHERE id = ?',
                               (job_id,)).fetchone()
      if row is None or row['lease_token'] != token:
        return False
      connection.execute('UPDATE jobs SET log = ? WHERE id = ? AND lease_token = ?',
                         (_appended(row['log'], line), job_id, token))
      return True

  def finish(self, job_id: str, token: Optional[str], slug: str,
             message: str = 'done') -> bool:
    with self.database.immediate() as connection:
      row = connection.execute(
          "UPDATE jobs SET status = 'done', progress = 100, message = ?, slug = ?, "
          "finished_at = ?, lease_token = NULL, lease_expires_at = NULL "
          "WHERE id = ? AND lease_token = ? AND status = 'running'",
          (message, slug, db.now(), job_id, token)).rowcount
      return bool(row)

  def fail(self, job_id: str, token: Optional[str], error: str) -> bool:
    with self.database.immediate() as connection:
      row = connection.execute(
          "UPDATE jobs SET status = 'failed', message = ?, error = ?, finished_at = ?, "
          "lease_token = NULL, lease_expires_at = NULL "
          "WHERE id = ? AND lease_token = ? AND status = 'running'",
          (error, error, db.now(), job_id, token)).rowcount
      return bool(row)

  def requeue(self, job_id: str, reason: str = '') -> bool:
    """Put a failed job back in the queue, for the retry button."""
    with self.database.immediate() as connection:
      row = connection.execute(
          "UPDATE jobs SET status = 'queued', progress = 0, message = ?, error = NULL, "
          "lease_token = NULL, lease_expires_at = NULL, finished_at = NULL "
          "WHERE id = ? AND status = 'failed'",
          (reason or 'queued again', job_id)).rowcount
      return bool(row)

  # ------------------------------------------------------------------ housekeeping

  def reap(self) -> List[str]:
    """Recover jobs whose worker died, and give up on those that keep dying.

    A lapsed lease means the worker is gone -- that is the point of a lease rather than
    a lock. The job goes back in the queue, unless it has already been tried the
    configured number of times, in which case it is failed with the reason, because a
    job that kills every worker is not going to succeed on the fourth.
    """
    expired = db.now()
    reaped: List[str] = []
    with self.database.immediate() as connection:
      rows = connection.execute(
          "SELECT id, attempts FROM jobs WHERE status = 'running' "
          "AND lease_expires_at IS NOT NULL AND lease_expires_at < ?", (expired,)).fetchall()
      for row in rows:
        if int(row['attempts']) >= self.settings.job_attempts:
          connection.execute(
              "UPDATE jobs SET status = 'failed', error = ?, message = ?, "
              "finished_at = ?, lease_token = NULL, lease_expires_at = NULL "
              "WHERE id = ?",
              ('the worker stopped responding after %d attempt(s)' % row['attempts'],
               'the worker stopped responding', db.now(), row['id']))
        else:
          connection.execute(
              "UPDATE jobs SET status = 'queued', message = ?, lease_token = NULL, "
              "lease_expires_at = NULL WHERE id = ?",
              ('requeued: the previous worker stopped responding', row['id']))
        reaped.append(row['id'])
    return reaped


def _appended(current: Optional[str], line: str) -> str:
  """Append a line to a job log, keeping the tail if it grows too long."""
  text = (current or '') + (line if line.endswith('\n') else line + '\n')
  if len(text) > LOG_LIMIT:
    text = '...\n' + text[-LOG_LIMIT:]
  return text


def _jsonable(value: Any) -> Any:
  """Paths and other non-JSON types in params, made storable."""
  if isinstance(value, dict):
    return {str(k): _jsonable(v) for k, v in value.items()}
  if isinstance(value, (list, tuple)):
    return [_jsonable(v) for v in value]
  if isinstance(value, pathlib.Path):
    return str(value)
  if isinstance(value, (str, int, float, bool)) or value is None:
    return value
  return str(value)
