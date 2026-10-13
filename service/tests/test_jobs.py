"""The queue: creating jobs, claiming them, and the lease that makes both safe.

These run against a real SQLite file in a temporary directory, because the things worth
testing here are the transaction and the lease, and those do not exist in a stub.
"""

from __future__ import annotations

import time

from service.app import jobs as jobs_module
from service.app.jobs import JobStore


def make(store: JobStore, **params) -> jobs_module.Job:
  return store.create(params or {'score': '/tmp/score.mxl'}, title='a piece')


def test_a_new_job_waits_for_a_worker(store):
  job = make(store)
  assert job.status == 'queued'
  assert job.row['progress'] == 0
  assert 'waiting' in job.row['message']
  assert store.stats()['queued'] == 1


def test_parameters_are_written_once_the_uploads_are_on_disk(store):
  job = make(store)
  store.set_params(job.id, {'score': '/tmp/a.mxl', 'audio': '/tmp/a.wav', 'job_id': job.id})
  again = store.get(job.id)
  assert again.params['score'] == '/tmp/a.mxl'
  assert again.params['audio'] == '/tmp/a.wav'


def test_claiming_takes_the_job_and_gives_it_a_lease(store):
  job = make(store)
  claimed = store.claim('worker-a')
  assert claimed is not None and claimed.id == job.id
  assert store.get(job.id).status == 'running'
  assert store.get(job.id).token, 'a claimed job must carry a lease token'
  assert store.claim('worker-b') is None, 'one job, one worker'


def test_the_oldest_queued_job_is_the_one_claimed(store):
  first = make(store)
  time.sleep(0.01)
  second = make(store)
  assert store.claim('worker').id == first.id
  assert store.claim('worker').id == second.id


def test_progress_from_a_worker_that_lost_its_lease_is_ignored(store):
  """The guard that stops a stalled worker from overwriting a finished job."""
  job = make(store)
  claimed = store.claim('worker-a')
  assert store.progress(job.id, claimed.token, 40.0, 'half way')
  assert store.get(job.id).row['progress'] == 40.0

  # The lease lapses and another worker takes the job.
  store.connection.execute('UPDATE jobs SET lease_expires_at = 0 WHERE id = ?', (job.id,))
  assert store.reap() == [job.id]
  second = store.claim('worker-b')
  assert second.id == job.id and second.token != claimed.token

  assert not store.progress(job.id, claimed.token, 99.0, 'stale'), \
      'the first worker must not be able to write any more'
  assert store.get(job.id).row['progress'] == 40.0, \
      'the stale 99% did not land; the job is where the worker that still holds it left it'
  assert store.progress(job.id, second.token, 10.0, 'starting over')


def test_finishing_records_the_piece_the_job_produced(store):
  job = make(store)
  claimed = store.claim('worker')
  assert store.finish(job.id, claimed.token, 'chopin', 'ready: Chopin')
  done = store.get(job.id)
  assert done.status == 'done' and done.slug == 'chopin'
  assert done.row['progress'] == 100
  assert done.row['finished_at'] >= done.row['started_at']
  assert store.stats()['done'] == 1
  assert not store.finish(job.id, claimed.token, 'again'), 'only a running job finishes'


def test_a_failure_keeps_the_message(store):
  job = make(store)
  claimed = store.claim('worker')
  assert store.fail(job.id, claimed.token, 'ffmpeg could not read that audio file')
  failed = store.get(job.id)
  assert failed.status == 'failed'
  assert 'ffmpeg' in failed.error
  assert failed.slug is None


def test_a_failed_job_can_be_queued_again(store):
  job = make(store)
  store.fail(job.id, store.claim('worker').token, 'boom')
  assert store.requeue(job.id, 'queued again by hand')
  again = store.get(job.id)
  assert again.status == 'queued' and again.row['error'] is None
  assert store.claim('worker').id == job.id, 'and it is claimable'
  assert not store.requeue(job.id), 'a running job is not failed'


def test_a_lapsed_lease_puts_the_job_back_in_the_queue(store):
  job = make(store)
  store.claim('worker')
  store.connection.execute('UPDATE jobs SET lease_expires_at = 0 WHERE id = ?', (job.id,))
  assert store.reap() == [job.id]
  assert store.get(job.id).status == 'queued'
  assert store.get(job.id).attempts == 1, 'the attempt is remembered across the retry'


def test_a_job_that_keeps_killing_its_worker_is_failed(store):
  """Otherwise a job that cannot succeed is claimed forever."""
  job = make(store)
  for _ in range(store.settings.job_attempts):
    store.claim('worker')
    store.connection.execute('UPDATE jobs SET lease_expires_at = 0 WHERE id = ?', (job.id,))
    store.reap()
  assert store.get(job.id).status == 'failed'
  assert 'stopped responding' in store.get(job.id).error


def test_a_live_lease_is_left_alone(store):
  job = make(store)
  store.claim('worker')
  assert store.reap() == []
  assert store.get(job.id).status == 'running'


def test_the_log_keeps_the_tail_when_it_grows(store):
  job = make(store)
  claimed = store.claim('worker')
  for index in range(600):
    store.append_log(job.id, claimed.token, 'line %d %s' % (index, 'x' * 60))
  log = store.get(job.id).row['log']
  assert 'line 599' in log
  assert len(log) <= jobs_module.LOG_LIMIT + 16, 'the log is capped, not unbounded'


def test_renaming_a_worker_does_not_lose_the_job(store):
  job = make(store)
  claimed = store.claim('worker-a')
  assert store.renew(job.id, claimed.token)
  assert store.holds(job.id, claimed.token)
  assert not store.holds(job.id, 'somebody-else')


# ---------------------------------------------------------------------------
# two writers, one database
# ---------------------------------------------------------------------------

def test_the_store_can_be_written_from_several_threads_at_once(store):
  """The API serves requests while the worker reports progress, so this is the normal
  case rather than an exotic one.

  It used to fail, and the failure is worth spelling out because the symptom was so far
  from the cause: both threads shared one SQLite connection, so their transactions
  interleaved on it and SQLite refused with `cannot start a transaction within a
  transaction` (or `cannot commit - no transaction is active`). Measured then: four
  threads produced four errors within a few hundred writes. The worker thread died on
  one of them, and the queue sat at "queued" forever with nothing said anywhere a person
  would look. Each thread now opens its own connection to the same WAL database.
  """
  import threading

  job = make(store)
  claimed = store.claim('worker')
  errors: list = []

  def write(name: str) -> None:
    try:
      for index in range(200):
        store.progress(job.id, claimed.token, index % 100, 'x' * 40, log='line %d' % index)
    except Exception as e:                      # noqa: BLE001 - the assertion is the point
      errors.append('%s: %s: %s' % (name, type(e).__name__, e))

  threads = [threading.Thread(target=write, args=('t%d' % n,)) for n in range(4)]
  for thread in threads:
    thread.start()
  for thread in threads:
    thread.join()
  assert not errors, errors[:3]
  assert store.get(job.id).row['progress'] == 99.0
  assert 'line 199' in store.get(job.id).row['log'], 'every writer got its lines in'


def test_a_failure_in_the_store_does_not_stop_the_worker(store, monkeypatch):
  """A worker that exits on one transient error silently stops taking work."""
  from service.app.worker import Worker

  worker = Worker(store.settings, store=store)
  calls = {'n': 0}

  def flaky_reap():
    calls['n'] += 1
    if calls['n'] == 1:
      raise RuntimeError('database is locked')
    return []

  monkeypatch.setattr(store, 'reap', flaky_reap)
  worker.loop(idle_seconds=0.2)                 # returns; does not raise
  assert calls['n'] > 1, 'the loop carried on after the failure'


def test_each_thread_gets_its_own_connection(store):
  """The reads and the writes must agree about which connection they are using.

  `JobStore.connection` used to be an attribute captured in `__init__`, so every thread
  was handed the one connection the store was built on -- which is the same bug as the
  test above, just moved from the transactions to the reads. Here it is asserted
  directly: the object a caller gets has to depend on who is asking.
  """
  import threading

  seen = {}

  def look(name: str) -> None:
    seen[name] = store.connection

  seen['main'] = store.connection
  thread = threading.Thread(target=look, args=('other',))
  thread.start()
  thread.join()
  assert seen['other'] is not seen['main'], 'one connection per thread, not one per store'
  assert store.connection is seen['main'], 'and the main thread keeps its own'
