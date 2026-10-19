"""Pieces that have not reached their page yet: the outbox.

The job runs where the analyzer is; the page is served from somewhere else. Making that
hop part of the job would be wrong in both directions: a job that fails because a droplet
was restarting throws away minutes of alignment, and a job that *waits* for the network
holds a lease the worker needs to renew, so a slow upload competes with the thing that
keeps the job.

So the hop is a queue instead. Publishing writes the piece locally and drops a marker
here; a push is attempted immediately, and anything that did not get through stays marked
until it does. Nothing needs to be running for that to be safe -- the next job drains the
backlog as it goes (`drain` is called before each publish), and `python -m service.app.publish`
drains it on demand or on a timer.

What is *not* in a marker: the token. The marker is a file on a disk, and the token is a
capability to publish pieces to the page -- it is read from the environment at push time,
so losing a disk does not hand anybody the ability to write to the site.
"""

from __future__ import annotations

import json
import pathlib
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

from .uploads import package_url

# How long a single push may take. Generous, because a piece is ~16 MB and the uplink may
# be a home connection; short of the broker's own lease, because a job must not be held.
PUSH_TIMEOUT_S = 120.0


def markers(settings) -> List[pathlib.Path]:
  """Every marker, oldest first -- `queued_at` is in the JSON, the name is the slug."""
  if not settings.outbox_dir.is_dir():
    return []
  found = list(settings.outbox_dir.glob('*.json'))
  return sorted(found, key=lambda path: path.stat().st_mtime)


def read_marker(path: pathlib.Path) -> Optional[Dict[str, Any]]:
  try:
    entry = json.loads(path.read_text(encoding='utf-8'))
  except (OSError, ValueError):
    return None
  if isinstance(entry, dict):
    entry['marker'] = str(path)
    return entry
  return None


def pending(settings) -> List[Dict[str, Any]]:
  """The pieces waiting to be pushed, oldest first, as the log and `/status` show them.

  Sorting by file mtime alone is not enough: two pieces queued in the same loop can land in
  the same timestamp tick, and `glob` then returns them in whatever order the directory
  happens to have -- so "oldest first" was not actually guaranteed, and a test that queued two
  pieces and drained them saw them come out reversed. `queued_at` (written by `enqueue`) with
  the file name as a tie-break makes it deterministic.
  """
  entries = [entry for entry in (read_marker(path) for path in markers(settings)) if entry]
  entries.sort(key=lambda entry: (float(entry.get('queued_at') or 0), entry.get('marker') or ''))
  return entries


def count(settings) -> int:
  return len(markers(settings))


def enqueue(settings, slug: str, payload: bytes, *, url: str,
            reason: str = '') -> pathlib.Path:
  """Write a package and its marker. The marker is written last, on purpose.

  A crash between the two leaves an orphan `.zip` and no marker: harmless, and cheaper to
  live with than a marker pointing at an archive that was never finished.
  """
  settings.ensure_dirs()
  settings.outbox_dir.mkdir(parents=True, exist_ok=True)
  (settings.outbox_dir / ('%s.zip' % slug)).write_bytes(payload)
  marker = settings.outbox_dir / ('%s.json' % slug)
  marker.write_text(json.dumps({'slug': slug, 'url': url, 'bytes': len(payload),
                                'queued_at': time.time(), 'attempts': 0,
                                'last_error': reason}, indent=1, sort_keys=True),
                    encoding='utf-8')
  return marker


def _connection_died(error: BaseException) -> bool:
  """Did the connection go away rather than the far side answering?

  `urllib` wraps the interesting failure: over TLS this is a `URLError` whose `reason` is an
  `ssl.SSLError`, and a refusal arrives as `SSL_ERROR_SYSCALL`, which Python reports with the
  memorable text `[SYS] unknown error`. Checked through the wrapper rather than by name only,
  because the name is exactly what makes this hard to read.
  """
  seen = [error, getattr(error, 'reason', None)]
  while seen:
    item = seen.pop()
    if isinstance(item, (BrokenPipeError, ConnectionResetError, ConnectionAbortedError,
                         TimeoutError, OSError)):
      return True
    if type(item).__module__ == 'ssl' or 'ssl' in type(item).__module__:
      return True
  return False


def _record(entry: Dict[str, Any], error: str) -> None:
  """Note a failed attempt, keeping everything else about the entry."""
  marker = pathlib.Path(entry['marker'])
  entry = dict(entry)
  entry.pop('marker', None)
  entry['attempts'] = int(entry.get('attempts') or 0) + 1
  entry['last_error'] = error[:400]
  entry['last_attempt'] = time.time()
  marker.write_text(json.dumps(entry, indent=1, sort_keys=True), encoding='utf-8')


def _discard(entry: Dict[str, Any]) -> None:
  pathlib.Path(entry['marker']).unlink(missing_ok=True)
  (pathlib.Path(entry['marker']).parent / ('%s.zip' % entry['slug'])).unlink(
      missing_ok=True)


def push(settings, entry: Dict[str, Any], *, timeout: float = PUSH_TIMEOUT_S,
         token: Optional[str] = None, opener=None,
         url: Optional[str] = None) -> Dict[str, Any]:
  """Send one marked piece. Returns `{'ok': bool, ...}`; never raises.

  Where it goes, in order: the `url` given here (an explicit one-off, which is what
  `service.app.publish --to` passes), the address recorded when the piece was queued, then
  `SERVICE_PUBLISH_URL`. Recording the address at enqueue time is what stops a later
  configuration change from sending yesterday's backlog to a page it was never meant for --
  and the explicit argument is what lets somebody override that on purpose.

  `opener` is there for the tests: a real push goes over the network, and a test that
  needs a server to fail in a specific way should not have to build one.
  """
  url = str(url or entry.get('url') or settings.publish_url or '').rstrip('/')
  slug = str(entry.get('slug') or '')
  if not url or not slug:
    return {'ok': False, 'reason': 'no address to send %r to' % slug, 'slug': slug}
  payload_path = pathlib.Path(entry['marker']).parent / ('%s.zip' % slug)
  if not payload_path.is_file():
    _discard(entry)                        # the package went away; the marker is a lie
    return {'ok': False, 'reason': 'the package for %s is missing; dropped' % slug,
            'slug': slug, 'dropped': True}

  target = package_url(url, slug)
  size = payload_path.stat().st_size
  request = urllib.request.Request(
      target, data=payload_path.read_bytes(), method='PUT',
      headers={'Content-Type': 'application/zip',
               'Content-Length': str(size),
               'Authorization': 'Bearer %s' % (token or settings.publish_token or ''),
               'User-Agent': 'note-seq-publish/1.0'})
  try:
    send = opener or urllib.request.urlopen
    with send(request, timeout=timeout) as response:
      status = getattr(response, 'status', 200)
      body = response.read(400).decode('utf-8', 'replace')
  except urllib.error.HTTPError as e:
    detail = ''
    try:
      detail = e.read(200).decode('utf-8', 'replace')
    except Exception:                      # noqa: BLE001 - a broken error body is not news
      pass
    reason = 'the page refused it: HTTP %s %s' % (e.code, detail or e.reason)
    _record(entry, reason)
    return {'ok': False, 'reason': reason, 'slug': slug, 'status': e.code}
  except Exception as e:                   # noqa: BLE001 - a network is allowed to fail
    reason = '%s: %s' % (type(e).__name__, e)
    if not (token or settings.publish_token) and _connection_died(e):
      # A page that refuses an upload answers *before reading it*, so a client still writing
      # 10 MB never sees the 401 -- it sees its connection go away, and how that is spelled
      # depends on the stack: `Broken pipe` locally, `URLError: [SYS] unknown error
      # (_ssl.c:2406)` over TLS. Both mean "the far side stopped listening", and the cause
      # is usually this. Measured: the same request with a 1 kB body answers a clean 401.
      reason += (' -- the page closed the connection while the package was being sent, '
                 'which is what a refused upload looks like, and no upload token is '
                 'configured (SERVICE_PUBLISH_TOKEN, or SERVICE_UPLOAD_TOKEN)')
    _record(entry, reason)
    return {'ok': False, 'reason': reason, 'slug': slug}

  if status >= 300:
    reason = 'the page answered HTTP %s %s' % (status, body)
    _record(entry, reason)
    return {'ok': False, 'reason': reason, 'slug': slug, 'status': status}

  _discard(entry)
  return {'ok': True, 'slug': slug, 'status': status, 'bytes': size, 'answer': body}


def drain(settings, *, limit: Optional[int] = None, timeout: float = PUSH_TIMEOUT_S,
          token: Optional[str] = None, log=None, opener=None,
          skip: Optional[set] = None, url: Optional[str] = None) -> List[Dict[str, Any]]:
  """Try every marked piece once, oldest first. Returns what happened to each.

  `skip` is for the caller that has already sent one piece itself and does not want to
  send it twice in the same job; `url` overrides where everything in this run goes.
  """
  results = []
  for entry in pending(settings):
    if skip and entry.get('slug') in skip:
      continue
    if limit is not None and len(results) >= limit:
      break
    outcome = push(settings, entry, timeout=timeout, token=token, opener=opener, url=url)
    outcome['title'] = entry.get('title') or entry.get('slug')
    results.append(outcome)
    if log:
      log('%-40s %s' % (entry.get('slug'), 'sent' if outcome['ok']
                        else 'waiting: %s' % outcome.get('reason')))
  return results
