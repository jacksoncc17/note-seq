"""Housekeeping: `python -m service.app.manage`.

A service that analyses music fills a disk, and none of it cleans itself up. Two things
grow: **jobs** (a row with its log, plus a directory holding the score and the recording
that were uploaded -- commonly 10-50 MB each) and **pieces** (~16 MB each: the recording
plus the engraved pages). A third, quieter one is `data/output/`, where the analyzer's
renderer leaves derived audio that nothing reads once the piece is written.

    python -m service.app.manage                          # what is on disk. Deletes nothing.
    python -m service.app.manage --jobs done,failed --older-than 14 --delete
    python -m service.app.manage --jobs all --keep 5 --delete
    python -m service.app.manage --pieces serenade-3 --delete
    python -m service.app.manage --keepers 3 --delete      # keep the three newest pieces
    python -m service.app.manage --renders --delete        # derived renders, regenerable
    python -m service.app.manage --scratch --delete        # debris from killed processes
    python -m service.app.manage --outbox --delete         # queued sends: see the warning

**Nothing is deleted without `--delete`**, and the summary it prints first is the same
information either way. That is deliberate: this is the one tool here that destroys work,
and a dry run that costs nothing is worth more than a confirmation prompt that gets typed
through. A job that is `queued` or `running` is refused unless `--force`, because deleting
one out from under a worker leaves it uploading artifacts for a job that no longer exists.

What survives everything: a piece whose files are gone can be re-analysed from the score
and the recording -- unless those were in a job directory that was deleted, which is the
one genuinely irreversible thing here. `--older-than` is your friend.
"""

from __future__ import annotations

import argparse
import pathlib
import shutil
import sys
import time
from typing import Any, Dict, List, Optional

from .config import Settings
from .jobs import JobStore
from .pieces import PieceStore

DAY = 86400.0
LIVE = ('queued', 'running')


def main(argv: Optional[List[str]] = None) -> int:
  parser = argparse.ArgumentParser(
      description='what this service has accumulated, and how to get rid of it',
      formatter_class=argparse.RawDescriptionHelpFormatter,
      epilog='Nothing is removed unless --delete is given.')
  parser.add_argument('--data', default=None, help='data directory (default SERVICE_DATA)')
  parser.add_argument('--jobs', default=None,
                      help='job statuses to consider: any of queued, running, done, '
                           'failed, or "all"')
  parser.add_argument('--pieces', default=None,
                      help='piece slugs to remove, comma separated')
  parser.add_argument('--keepers', type=int, default=None,
                      help='keep this many of the newest pieces, remove the rest')
  parser.add_argument('--older-than', type=float, default=None, metavar='DAYS',
                      help='only jobs finished (or created) more than this many days ago')
  parser.add_argument('--keep', type=int, default=None,
                      help='keep this many of the newest jobs in the selection')
  parser.add_argument('--renders', action='store_true',
                      help='the analyzer\'s derived output (data/output/*)')
  parser.add_argument('--scratch', action='store_true',
                      help='half-built pieces, replaced pieces, and job directories with '
                           'no row')
  parser.add_argument('--outbox', action='store_true',
                      help='pieces queued for a page that has not taken them')
  parser.add_argument('--adopt', default=None,
                      help='give these piece slugs an owner (comma separated), so the page '
                           'shows them to that account')
  parser.add_argument('--adopt-unowned', action='store_true',
                      help='give every piece with no owner one -- for pieces published '
                           'before ownership was recorded')
  parser.add_argument('--owner', default=None,
                      help="the owner to adopt pieces for (the broker's user id)")
  parser.add_argument('--delete', action='store_true',
                      help='actually remove what the summary listed')
  parser.add_argument('--force', action='store_true',
                      help='allow deleting a queued or running job, and renders while '
                           'a job is running')
  args = parser.parse_args(argv)

  if args.data:
    import os
    os.environ['SERVICE_DATA'] = args.data
  settings = Settings()
  settings.ensure_dirs()
  store, pieces = JobStore(settings), PieceStore(settings)

  running = store.stats().get('running', 0) + store.stats().get('queued', 0)

  # ------------------------------------------------------------------ what is here
  jobs = _all_jobs(store)
  by_status: Dict[str, int] = {}
  for job in jobs:
    by_status[job['status']] = by_status.get(job['status'], 0) + 1
  piece_rows = pieces.listing()
  print('data directory %s' % settings.data_dir)
  print('  jobs      %-4d %s   (%s on disk)'
        % (len(jobs), by_status or '', _size(_directory_bytes(settings.jobs_dir))))
  unowned = [row['slug'] for row in piece_rows if not pieces.owner(row['slug'])]
  print('  pieces    %-4d %s%s'
        % (len(piece_rows), _size(_directory_bytes(settings.pieces_dir)),
           '' if not unowned else '   (%d with no owner: only an operator can see those)'
           % len(unowned)))
  print('  renders   %s' % _size(_directory_bytes(settings.data_dir / 'output')))
  print('  outbox    %-4d %s' % (len(_outbox_markers(settings)),
                                 _size(_directory_bytes(settings.outbox_dir))))
  print('  database  %s' % _size(_file_bytes(settings.database)))

  removals: List[Dict[str, Any]] = []

  # ------------------------------------------------------------------ jobs
  if args.jobs:
    wanted = [s.strip() for s in args.jobs.split(',') if s.strip()]
    if wanted == ['all']:
      wanted = ['queued', 'running', 'done', 'failed']
    unknown = [s for s in wanted if s not in ('queued', 'running', 'done', 'failed')]
    if unknown:
      print('unknown status: %s' % ', '.join(unknown), file=sys.stderr)
      return 2
    chosen = [job for job in jobs if job['status'] in wanted]
    if args.keep:
      chosen = chosen[args.keep:]
    if args.older_than is not None:
      cutoff = time.time() - args.older_than * DAY
      chosen = [job for job in chosen
                if (job['finished_at'] or job['created_at'] or 0) < cutoff]
    for job in chosen:
      blockers = []
      if job['status'] in LIVE and not args.force:
        blockers.append('%s: it is %s, and its worker would keep going'
                        % (job['id'][:8], job['status']))
      removals.append({'what': 'job', 'id': job['id'], 'label': '%s  %-7s %s  %s'
                       % (job['id'][:8], job['status'], job['title'][:40],
                          _size(_directory_bytes(settings.job_dir(job['id'])))),
                       'blockers': blockers})

  # ------------------------------------------------------------------ pieces
  targets: List[str] = []
  if args.pieces:
    targets += [s.strip() for s in args.pieces.split(',') if s.strip()]
  if args.keepers is not None:
    ordered = [row['slug'] for row in piece_rows]        # newest first
    targets += ordered[args.keepers:]
  for slug in dict.fromkeys(targets):
    if slug not in [row['slug'] for row in piece_rows]:
      print('no such piece: %s' % slug, file=sys.stderr)
      return 2
    removals.append({'what': 'piece', 'id': slug,
                     'label': '%-40s %s' % (slug, _size(_directory_bytes(
                         settings.piece_dir(slug)))),
                     'blockers': []})

  # ------------------------------------------------------------------ ownership
  if args.adopt or args.adopt_unowned:
    # A write, not a deletion, so it does not wait for `--delete`: it adds one field to a
    # piece's meta and changes nothing about the music. It is here rather than in a script
    # of its own because it is the same question this tool already answers -- what is in
    # this store, and what should stop being.
    if not args.owner:
      print('say who owns them: --owner <subject>, which is the broker\'s user id',
            file=sys.stderr)
      return 2
    wanted = [s.strip() for s in (args.adopt or '').split(',') if s.strip()]
    if args.adopt_unowned:
      wanted += [row['slug'] for row in piece_rows if not pieces.owner(row['slug'])]
    adopted = 0
    for slug in dict.fromkeys(wanted):
      if slug not in [row['slug'] for row in piece_rows]:
        print('no such piece: %s' % slug, file=sys.stderr)
        return 2
      before = pieces.owner(slug) or '(nobody)'
      if pieces.adopt(slug, args.owner):
        adopted += 1
        print('  owner    %-40s %s -> %s' % (slug, before, args.owner))
    print('%d piece(s) adopted by %s%s' % (adopted, args.owner,
                                           '' if adopted else ' (nothing was unowned)'))

  # ------------------------------------------------------------------ outbox
  if args.outbox:
    for marker in _outbox_markers(settings):
      slug = marker.stem
      removals.append({'what': 'queued piece', 'id': slug,
                       'label': '%s  (not delivered; the piece itself stays in pieces/)'
                                % slug, 'blockers': []})

  # ------------------------------------------------------------------ scratch
  if args.scratch:
    # Through `swab_plan` rather than a glob of the directory: one of those cases is a
    # `.previous` that is the *only* complete copy of a piece, and a tool that deletes it
    # because of its name destroys work that a crash left recoverable.
    for step in pieces.swab_plan():
      path = pathlib.Path(step['path'])
      removals.append({'what': 'scratch', 'id': step['path'], 'action': step['action'],
                       'label': '%-38s %-8s %s  (%s)'
                                % (step['name'], _size(_directory_bytes(path)),
                                   step['action'], step['reason']),
                       'blockers': []})
    known = {job['id'] for job in jobs}
    for path in (sorted(settings.jobs_dir.iterdir())
                 if settings.jobs_dir.is_dir() else []):
      if path.is_dir() and path.name not in known:
        removals.append({'what': 'orphan job directory', 'id': path.name,
                         'label': '%s  %s' % (path.name[:8],
                                              _size(_directory_bytes(path))),
                         'blockers': []})

  # ------------------------------------------------------------------ renders
  if args.renders:
    output = settings.data_dir / 'output'
    blockers = (['a job is queued or running and may be writing here']
                if running and not args.force else [])
    for path in (sorted(output.iterdir()) if output.is_dir() else []):
      removals.append({'what': 'derived output', 'id': path.name, 'label': path.name,
                       'blockers': blockers})

  # ------------------------------------------------------------------ say, then do
  if not removals:
    print('\nnothing selected. --jobs, --pieces, --keepers, --outbox, --scratch, '
          '--renders pick something; --delete removes it.')
    return 0

  print('\n%s:' % ('removing' if args.delete else 'would remove (nothing has been '
                                                  'deleted; add --delete)'))
  blocked = 0
  removed = 0
  for item in removals:
    if item['blockers']:
      blocked += 1
      print('  skipped  %s  -- %s' % (item['label'], '; '.join(item['blockers'])))
      continue
    print('  %-8s %s' % ('remove' if args.delete else 'would', item['label']))
    if not args.delete:
      continue
    try:
      _remove(settings, store, pieces, item)
      removed += 1
    except OSError as e:
      print('    failed: %s' % e, file=sys.stderr)

  if args.delete:
    print('\n%d removed, %d skipped%s' % (removed, blocked,
                                          ''  if not blocked else
                                          ' (use --force to include the skipped ones)'))
  return 0


# ------------------------------------------------------------------------- helpers

def _all_jobs(store: JobStore) -> List[Dict[str, Any]]:
  """Every job row, newest first, as plain dicts."""
  rows = []
  for status in ('queued', 'running', 'done', 'failed'):
    rows += [dict(job.row) for job in store.list(limit=None, status=status)]
  rows.sort(key=lambda job: job['created_at'] or 0, reverse=True)
  return rows


def _remove(settings: Settings, store: JobStore, pieces: PieceStore, item: Dict) -> None:
  kind = item['what']
  if kind == 'job':
    store.forget(item['id'])
    shutil.rmtree(settings.job_dir(item['id']), ignore_errors=True)
  elif kind == 'piece':
    pieces.delete(item['id'])
  elif kind == 'queued piece':
    settings.outbox_dir.joinpath('%s.json' % item['id']).unlink(missing_ok=True)
    settings.outbox_dir.joinpath('%s.zip' % item['id']).unlink(missing_ok=True)
  elif kind == 'scratch':
    path = pathlib.Path(item['id'])
    if item.get('action') == 'restore':
      slug = path.name[:-len('.previous')]
      path.rename(pieces.directory(slug))
    else:
      shutil.rmtree(path, ignore_errors=True)
  elif kind == 'orphan job directory':
    shutil.rmtree(settings.jobs_dir / item['id'], ignore_errors=True)
  elif kind == 'derived output':
    path = settings.data_dir / 'output' / item['id']
    if path.is_dir():
      shutil.rmtree(path)
    else:
      path.unlink(missing_ok=True)


def _outbox_markers(settings: Settings) -> List[pathlib.Path]:
  if not settings.outbox_dir.is_dir():
    return []
  return sorted(settings.outbox_dir.glob('*.json'))


def _directory_bytes(path: pathlib.Path) -> int:
  if not path.is_dir():
    return 0
  total = 0
  for item in path.rglob('*'):
    try:
      if item.is_file():
        total += item.stat().st_size
    except OSError:
      pass
  return total


def _file_bytes(path: pathlib.Path) -> int:
  try:
    return path.stat().st_size
  except OSError:
    return 0


def _size(count: int) -> str:
  if count >= 1 << 30:
    return '%.1f GB' % (count / (1 << 30))
  if count >= 1 << 20:
    return '%.1f MB' % (count / (1 << 20))
  if count >= 1 << 10:
    return '%.0f kB' % (count / (1 << 10))
  return '%d B' % count


if __name__ == '__main__':                   # pragma: no cover - a command
  raise SystemExit(main())
