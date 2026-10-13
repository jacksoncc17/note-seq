"""The database: one SQLite file, two tables, no server to run.

The reference service in `selfCommentary` uses Postgres, where the queue is a table
claimed with `FOR UPDATE SKIP LOCKED`. That is the right shape for a queue that several
machines pull from. Here there is one service on one box, so the same shape is kept --
jobs with a status, a lease and an attempts count -- on SQLite, which needs no daemon,
no credentials and no migration step to get started.

Two details matter for correctness rather than taste:

* **WAL.** The API reads jobs on every poll while the worker writes progress. In the
  default rollback journal those two block each other and the poll times out under a
  long alignment; WAL lets readers carry on.
* **`BEGIN IMMEDIATE` for claiming.** SQLite has no `SKIP LOCKED`, but it does have a
  single writer: taking the write lock *before* selecting the candidate makes claiming
  atomic between processes, which is the whole point of the exercise.
"""

from __future__ import annotations

import pathlib
import sqlite3
import time
from typing import Optional

# The schema, applied on every start. `CREATE TABLE IF NOT EXISTS` is enough here: this
# database holds jobs that can be re-run and pieces that can be re-analysed, so there is
# nothing in it that a schema change could destroy that is worth a migration tool.
SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
  id                TEXT PRIMARY KEY,
  kind              TEXT NOT NULL DEFAULT 'analyse',
  status            TEXT NOT NULL,
  progress          REAL NOT NULL DEFAULT 0,
  message           TEXT NOT NULL DEFAULT '',
  params            TEXT NOT NULL DEFAULT '{}',
  slug              TEXT,
  title             TEXT NOT NULL DEFAULT '',
  error             TEXT,
  log               TEXT NOT NULL DEFAULT '',
  attempts          INTEGER NOT NULL DEFAULT 0,
  worker            TEXT,
  lease_token       TEXT,
  lease_expires_at  REAL,
  created_at        REAL NOT NULL,
  started_at        REAL,
  finished_at       REAL
);
CREATE INDEX IF NOT EXISTS jobs_queue ON jobs(status, created_at);
CREATE INDEX IF NOT EXISTS jobs_lease ON jobs(status, lease_expires_at);

CREATE TABLE IF NOT EXISTS pieces (
  slug        TEXT PRIMARY KEY,
  job_id      TEXT,
  title       TEXT NOT NULL DEFAULT '',
  composer    TEXT NOT NULL DEFAULT '',
  bars        INTEGER NOT NULL DEFAULT 0,
  duration_s  REAL NOT NULL DEFAULT 0,
  method      TEXT NOT NULL DEFAULT '',
  created_at  REAL NOT NULL,
  meta        TEXT NOT NULL DEFAULT '{}'
);
"""

STATUSES = ('queued', 'running', 'done', 'failed')


def connect(path: pathlib.Path, timeout: float = 30.0) -> sqlite3.Connection:
  """Open the database, with the pragmas the service depends on."""
  path = pathlib.Path(path)
  path.parent.mkdir(parents=True, exist_ok=True)
  connection = sqlite3.connect(str(path), timeout=timeout, isolation_level=None,
                               check_same_thread=False)
  connection.row_factory = sqlite3.Row
  connection.execute('PRAGMA journal_mode=WAL')
  connection.execute('PRAGMA synchronous=NORMAL')
  connection.execute('PRAGMA foreign_keys=ON')
  connection.execute('PRAGMA busy_timeout=%d' % int(timeout * 1000))
  return connection


def initialize(connection: sqlite3.Connection) -> None:
  connection.executescript(SCHEMA)


def now() -> float:
  return time.time()


class Database:
  """A connection with the small amount of transaction handling this needs."""

  def __init__(self, path: pathlib.Path):
    self.path = pathlib.Path(path)
    self.connection = connect(self.path)
    initialize(self.connection)

  def immediate(self):
    """A context manager holding the write lock for the block.

    `BEGIN IMMEDIATE` takes the lock at the start rather than at the first write, which
    is what makes "find a queued job, then claim it" safe when two workers do it at
    once: the second one blocks until the first commits, then finds nothing to claim.
    """
    return _Immediate(self.connection)

  def close(self) -> None:
    try:
      self.connection.close()
    except sqlite3.Error:
      pass


class _Immediate:
  def __init__(self, connection: sqlite3.Connection):
    self.connection = connection

  def __enter__(self) -> sqlite3.Connection:
    self.connection.execute('BEGIN IMMEDIATE')
    return self.connection

  def __exit__(self, exc_type, exc, tb) -> bool:
    if exc_type is None:
      self.connection.execute('COMMIT')
    else:
      self.connection.execute('ROLLBACK')
    return False


def row_to_dict(row: Optional[sqlite3.Row]) -> Optional[dict]:
  return dict(row) if row is not None else None
