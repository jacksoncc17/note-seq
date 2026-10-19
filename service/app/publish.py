"""Send the pieces that are waiting: `python -m service.app.publish`.

A job delivers its own piece as it finishes, and drains a little of the backlog with it
(see `app/pipeline.publish_remote`). This is for the times when no job is running:

    python -m service.app.publish                 # try everything once and exit
    python -m service.app.publish --watch         # keep trying, for a timer or a terminal
    python -m service.app.publish --status        # what is waiting, and why

Nothing here needs the analyzer: it reads `data/outbox/` and talks HTTP, so it can run
next to the service or beside the worker, and it is safe to run twice at once -- a push
that succeeds deletes its marker, and a push that fails only adds to a count.

Where the pieces go is `SERVICE_PUBLISH_URL`, and what they are sent with is
`SERVICE_PUBLISH_TOKEN` (or `SERVICE_UPLOAD_TOKEN`, which is the same secret from the
other end of the wire). `--to` overrides the address for a one-off, which is how you
point a backlog at a new host without editing the environment first.
"""

from __future__ import annotations

import argparse
import os
import pathlib
import sys
import time
import urllib.parse
from typing import List, Optional

from . import outbox
from .config import Settings, apply_worker_env


def main(argv: Optional[List[str]] = None) -> int:
  parser = argparse.ArgumentParser(description='deliver finished pieces to the page '
                                               'that serves them')
  parser.add_argument('--to', default=None,
                      help='the service root to send to (default SERVICE_PUBLISH_URL), '
                           'e.g. https://example.com/note-seq')
  parser.add_argument('--watch', action='store_true', help='keep running, not just once')
  parser.add_argument('--interval', type=float, default=60.0,
                      help='seconds between attempts in --watch (default 60)')
  parser.add_argument('--timeout', type=float, default=outbox.PUSH_TIMEOUT_S,
                      help='seconds for one push (default %g)' % outbox.PUSH_TIMEOUT_S)
  parser.add_argument('--limit', type=int, default=0,
                      help='send at most this many per attempt (0 = all)')
  parser.add_argument('--queue', metavar='SLUG', action='append', default=[],
                      help='also mark this local piece for delivery (repeatable). For '
                           'pieces analysed before an address was configured')
  parser.add_argument('--queue-all', action='store_true',
                      help='also mark every local piece for delivery')
  parser.add_argument('--owner', default=None,
                      help='stamp this owner on the pieces being queued (the broker\'s user '
                           'id), so the page shows them to that account')
  parser.add_argument('--status', action='store_true',
                      help='list what is waiting, send nothing')
  parser.add_argument('--token', default=None,
                      help='the upload token (default SERVICE_PUBLISH_TOKEN, then '
                           'SERVICE_UPLOAD_TOKEN)')
  args = parser.parse_args(argv)

  settings = Settings()
  if args.to:
    settings.publish_url = args.to.strip().rstrip('/')

  # The worker's own configuration file, if it is there. `run_worker.sh` already sources it,
  # and the alternative is the failure this was written after: a bare
  # `python -m service.app.publish --queue-all --to https://…` with no token in the
  # environment, which the page refuses *before* reading the body -- so what comes back is
  # `URLError: [SYS] unknown error (_ssl.c:2406)` rather than a 401, and nothing says the
  # token is what is missing. Anything already in the environment wins.
  from_worker_env = apply_worker_env(settings)
  if from_worker_env:
    print('worker.env: took %s' % ', '.join(from_worker_env))

  if args.queue or args.queue_all:
    # Pieces that were analysed before there was anywhere to send them live only in the
    # local store, and re-running the analysis to publish them would be absurd. This marks
    # them for delivery instead, through exactly the same path a fresh job uses.
    queued = _queue(settings, args)
    print('marked %d piece(s) for delivery: %s'
          % (len(queued), ', '.join(queued) or '(nothing to mark)'))

  waiting = outbox.pending(settings)
  print('outbox %s: %d waiting' % (settings.outbox_dir, len(waiting)))
  for entry in waiting:
    print('  %-40s %6.1f MB  attempts %d%s'
          % (entry.get('slug'), (entry.get('bytes') or 0) / 1e6,
             int(entry.get('attempts') or 0),
             ('  last: %s' % entry['last_error']) if entry.get('last_error') else ''))
  if args.status:
    return 0
  if not waiting:
    return 0
  if not settings.publish_url:
    print('nothing to send to: set SERVICE_PUBLISH_URL, or pass --to', file=sys.stderr)
    return 2

  if not settings.publish_token and not _is_loopback(settings.publish_url):
    print('no upload token is configured, and %s is not this machine: the page will '
          'refuse every one of these before reading it (set SERVICE_PUBLISH_TOKEN, or '
          'put it in service/broker/worker.env)' % settings.publish_url, file=sys.stderr)

  print('sending to %s' % settings.publish_url)
  while True:
    # `url` is passed as well as set: a marker remembers the address it was queued for, and
    # `--to` is somebody saying "not that one, this one".
    results = outbox.drain(settings, timeout=args.timeout, token=args.token,
                           limit=args.limit or None, url=settings.publish_url,
                           log=lambda line: print('  %s' % line))
    sent = sum(1 for r in results if r.get('ok'))
    left = outbox.count(settings)
    print('%d sent, %d still waiting' % (sent, left))
    if not args.watch:
      return 0 if left == 0 else 1
    if left:
      # The address or the credential is usually the thing that is wrong, and both make
      # every retry fail the same way: say so once rather than once per interval.
      first = next((r for r in results if not r.get('ok')), None)
      if first:
        print('  still failing: %s' % first.get('reason'))
    time.sleep(max(5.0, args.interval))


def _is_loopback(url: str) -> bool:
  """Is this address this machine? A token is pointless on loopback and required off it."""
  parsed = urllib.parse.urlsplit(url or '')
  return parsed.hostname in ('localhost', '127.0.0.1', '::1', '[::1]') or not parsed.hostname


def _queue(settings: Settings, args) -> List[str]:
  """Mark local pieces for delivery, skipping anything already waiting.

  Packing happens here rather than at send time so that a piece which cannot be packaged
  -- half-written, or missing its pages -- is reported now, while somebody is watching,
  instead of turning into a marker that fails forever.
  """
  from .pieces import PieceStore
  from .uploads import PackageError, pack_piece

  store = PieceStore(settings)
  wanted = list(args.queue)
  if args.queue_all:
    wanted = [slug for slug in store.slugs() if slug not in wanted]
  if not settings.publish_url:
    print('nowhere to send them: set SERVICE_PUBLISH_URL, or pass --to', file=sys.stderr)
    raise SystemExit(2)

  waiting = {entry['slug'] for entry in outbox.pending(settings)}
  queued = []
  for slug in wanted:
    if slug in waiting:
      continue
    if args.owner:
      # Before packing, because the owner travels *inside* the package -- that is what
      # makes it a statement by the sender rather than something the far side has to look
      # up, and it is why an unowned piece can be fixed here and not only in the store.
      store.adopt(slug, args.owner)
    try:
      payload = pack_piece(store.directory(slug))
    except PackageError as e:
      print('  %-40s cannot be packaged: %s' % (slug, e), file=sys.stderr)
      continue
    outbox.enqueue(settings, slug, payload, url=settings.publish_url)
    print('  %-40s %6.1f MB' % (slug, len(payload) / 1e6))
    queued.append(slug)
  return queued


if __name__ == '__main__':                   # pragma: no cover - a command
  os.environ.setdefault('PYTHONUNBUFFERED', '1')
  raise SystemExit(main())