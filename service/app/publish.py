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
import sys
import time
from typing import List, Optional

from . import outbox
from .config import Settings


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
  parser.add_argument('--status', action='store_true',
                      help='list what is waiting, send nothing')
  parser.add_argument('--token', default=None,
                      help='the upload token (default SERVICE_PUBLISH_TOKEN, then '
                           'SERVICE_UPLOAD_TOKEN)')
  args = parser.parse_args(argv)

  settings = Settings()
  if args.to:
    settings.publish_url = args.to.strip().rstrip('/')

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


if __name__ == '__main__':                   # pragma: no cover - a command
  os.environ.setdefault('PYTHONUNBUFFERED', '1')
  raise SystemExit(main())
