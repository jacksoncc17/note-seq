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

from .config import Settings, apply_worker_env
from .jobs import JobStore
from .pieces import PieceStore
from .uploads import pack_piece

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
                      help='only jobs finished (or created) more than this many days ago; '
                           'with --evict, pieces untouched for longer than this '
                           '(default 15)')
  parser.add_argument('--keep', type=int, default=None,
                      help='keep this many of the newest jobs in the selection')
  parser.add_argument('--evict', action='store_true',
                      help='remove the data of pieces nobody has opened recently, keeping '
                           'their record, once the archive copy is verified')
  parser.add_argument('--keep-used', type=int, default=None, metavar='N',
                      help='with --evict: never evict the N most recently used pieces '
                           '(default 0)')
  parser.add_argument('--max-used-percent', type=float, default=None, metavar='P',
                      help='with --evict: only evict while this filesystem is at least P%% '
                           'full (default 80; pass 0 to evict regardless of free space)')
  parser.add_argument('--archive-all', action='store_true',
                      help='copy every piece in this store into the archive bucket (for '
                           'pieces published before an archive was configured)')
  parser.add_argument('--restore-from', default=None, metavar='BUCKET',
                      help='copy every archived piece into this store from an S3 bucket '
                           '(needs SERVICE_ARCHIVE_URL/_KEY/_SECRET)')
  parser.add_argument('--restore-url', default=None,
                      help='the archive endpoint, for a bucket that is not the configured one')
  parser.add_argument('--overwrite', action='store_true',
                      help='with --restore-from, replace pieces this store already has')
  parser.add_argument('--tools', action='store_true',
                      help='the external tools and their versions, and how to update the '
                           'one that goes stale (yt-dlp)')
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
  # worker.env is where the archive and publish settings live; read it here too, so
  # --archive-all and --restore-from work with the same file the worker uses.
  taken = apply_worker_env(settings)
  settings.ensure_dirs()
  store, pieces = JobStore(settings), PieceStore(settings)
  if taken:
    print('worker.env: took %s' % ', '.join(taken))

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

  if args.evict:
    return _evict(settings, pieces, store, args)

  if args.archive_all:
    return _archive_all(settings, pieces)

  if args.restore_from:
    return _restore(settings, pieces, args)

  if args.tools:
    _tools(settings)
    return 0

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

def _evict(settings: Settings, pieces: PieceStore, store: JobStore, args) -> int:
  """Remove the data of pieces nobody has opened, keeping their record.

  The policy is deliberately conditional rather than a plain age rule: pieces go only when the
  archive provably has them **and** the serving disk is actually under pressure. A 15-day rule
  alone would delete pieces that cost nothing to keep and then make the first access after that
  a slow rehydrate for no benefit; a watermark alone would never age anything out. Together they
  mean "reclaim space from what nobody is using, oldest first".

  What the record keeps is what the archive cannot be asked for quickly: the title, the map,
  the geometry. The recording and the pages are what the package restores.
  """
  from .access import AccessLog, usage_order
  from .pipeline import archive_piece
  from .s3 import archive_name

  # The same flag the job cleanup uses: "older than N days", applied to last access here.
  older_days = 15.0 if args.older_than is None else args.older_than
  keep_used = 0 if args.keep_used is None else args.keep_used
  watermark = 80.0 if args.max_used_percent is None else args.max_used_percent

  usage = usage_order(pieces, AccessLog(store.database))
  if not usage:
    print('no pieces')
    return 0

  used = shutil.disk_usage(settings.data_dir)
  used_percent = 100.0 * used.used / used.total if used.total else 0.0
  print('serving disk: %.1f%% used (%s free of %s), watermark %g%%'
        % (used_percent, _size(used.free), _size(used.total), watermark))
  if used_percent < watermark:
    print('below the watermark: nothing to reclaim')
    return 0

  cutoff = time.time() - older_days * 86400
  oldest_first = usage[:-keep_used] if keep_used else usage
  candidates = [row for row in oldest_first if row['last_access'] < cutoff]
  print('%d piece(s) untouched for more than %g days (of %d)'
        % (len(candidates), older_days, len(usage)))
  if not candidates:
    return 0

  if not settings.archive_enabled:
    print('the archive is not configured, so nothing can be evicted safely: set '
          'SERVICE_ARCHIVE_* (see deploy/garage)', file=sys.stderr)
    return 2
  client = settings.archive_client()

  planned: List[Dict[str, Any]] = []
  for row in candidates:
    slug = row['slug']
    if not pieces.complete(slug):
      print('  skip     %-40s already evicted (or incomplete)' % slug)
      continue
    try:
      payload = pack_piece(pieces.directory(slug))
    except Exception as e:                   # noqa: BLE001 - cannot package, cannot verify
      print('  skip     %-40s cannot be packaged: %s' % (slug, e), file=sys.stderr)
      continue
    try:
      remote = client.head(archive_name(slug))
    except Exception as e:                   # noqa: BLE001
      print('  skip     %-40s archive unreachable: %s' % (slug, e), file=sys.stderr)
      continue
    if remote is None:
      print('  skip     %-40s not in the archive -- refusing to remove the only copy' % slug)
      continue
    if remote['bytes'] != len(payload):
      # Same name, different bytes: the archive copy is not of this piece, so removing the
      # local one would leave a different recording behind the same slug.
      print('  skip     %-40s archive copy differs (%d vs %d bytes)'
            % (slug, remote['bytes'], len(payload)))
      continue
    planned.append({**row, 'bytes': len(payload)})

  if not planned:
    print('nothing evictable')
    return 0

  freed_total = sum(row['bytes'] for row in planned)
  print('\n%s:' % ('evicting' if args.delete else
                   'would evict (nothing removed; add --delete)'))
  freed = 0
  for row in planned:
    when = time.strftime('%Y-%m-%d', time.localtime(row['last_access']))
    print('  %-8s %-40s %6.1f MB   last opened %s%s'
          % ('evict' if args.delete else 'would', row['slug'], row['bytes'] / 1e6, when,
             '' if row['recorded'] else '  (no record: dated from creation)'))
    if args.delete:
      freed += pieces.evict(row['slug'])

  if args.delete:
    print('\n%d evicted, %.1f MB freed; %d kept for now. '
          'Their records remain, so they still list and their archive copy can restore them:'
          % (len(planned), freed / 1e6, len(usage) - len(planned)))
    print('  python -m service.app.manage --restore-from %s' % settings.archive_bucket)
  else:
    print('\n%d piece(s), %.1f MB would be freed' % (len(planned), freed_total / 1e6))
  return 0


def _archive_all(settings: Settings, pieces: PieceStore) -> int:
  """Backfill: copy every piece in this store into the archive bucket.

  The archive is written by `publish_remote`, so pieces analysed before an archive existed
  are not in it. This is the migration command -- and the recovery command, after an archive
  was lost -- and it reuses the same packer and the same "never fail the thing you are
  copying" rule: a piece that cannot be archived is reported and the rest continue.
  """
  from .pipeline import archive_piece

  if not settings.archive_enabled:
    print('the archive is not configured: set SERVICE_ARCHIVE_URL, _BUCKET, _KEY and _SECRET',
          file=sys.stderr)
    return 2

  slugs = pieces.slugs()
  if not slugs:
    print('no pieces in %s' % settings.pieces_dir)
    return 0
  print('archiving %d piece(s) from %s to %s/%s'
        % (len(slugs), settings.pieces_dir, settings.archive_url, settings.archive_bucket))

  archived = failed = 0
  written = 0
  for slug in slugs:
    result = archive_piece(settings, slug, log=lambda _m: None)
    if result.get('archived'):
      archived += 1
      written += int(result.get('bytes') or 0)
      print('  archived %-40s %6.1f MB' % (slug, (result.get('bytes') or 0) / 1e6))
    else:
      failed += 1
      print('  failed   %-40s %s' % (slug, result.get('reason', 'unknown')), file=sys.stderr)

  print('\n%d archived (%.1f MB), %d failed' % (archived, written / 1e6, failed))
  return 0 if archived else 1


def _restore(settings: Settings, pieces: PieceStore, args) -> int:
  """Refill this store from the archive bucket. The "the serving machine is disposable" bit.

  Expects the package format `publish_remote` archives: one `pieces/<slug>.zip` per piece,
  the same bytes the page's own upload endpoint accepts. A piece this store already has is
  skipped unless `--overwrite`, so re-running after a partial restore finishes the job
  rather than re-downloading everything.
  """
  import tempfile

  from .s3 import ARCHIVE_PREFIX, S3Error, slug_of
  from .uploads import PackageError, unpack_piece

  if args.restore_url:
    settings.archive_url = args.restore_url
  settings.archive_bucket = args.restore_from
  if not (settings.archive_url and settings.archive_key and settings.archive_secret):
    print('the archive is not configured: set SERVICE_ARCHIVE_URL, SERVICE_ARCHIVE_KEY and '
          'SERVICE_ARCHIVE_SECRET (or pass --restore-url)', file=sys.stderr)
    return 2
  try:
    client = settings.archive_client()
    listed = client.list(ARCHIVE_PREFIX)
  except S3Error as e:
    print('could not list %s: %s' % (settings.archive_bucket, e), file=sys.stderr)
    return 1

  names = [(slug_of(item['name']), item) for item in listed]
  names = [(slug, item) for slug, item in names if slug]
  print('archive %s/%s: %d piece(s)' % (settings.archive_url, settings.archive_bucket,
                                        len(names)))
  if not names:
    return 0

  restored = skipped = failed = 0
  for slug, item in sorted(names):
    # `complete`, not `exists`: an evicted piece has its record and its timings and none of
    # what the page draws, so it has to be restored like a missing one.
    if pieces.complete(slug) and not args.overwrite:
      skipped += 1
      print('  have     %-40s %s' % (slug, _size(item['bytes'])))
      continue
    try:
      payload = client.get(item['name'])
    except S3Error as e:
      failed += 1
      print('  failed   %-40s %s' % (slug, e), file=sys.stderr)
      continue
    staging = pieces.staging_path(slug)
    if staging.exists():
      shutil.rmtree(staging, ignore_errors=True)
    try:
      summary = unpack_piece(payload, staging, max_bytes=int(settings.max_piece_mb * (1 << 20)))
      pieces.install(slug, staging)
    except (PackageError, OSError) as e:
      shutil.rmtree(staging, ignore_errors=True)
      failed += 1
      print('  failed   %-40s %s' % (slug, e), file=sys.stderr)
      continue
    restored += 1
    print('  restored %-40s %-6s %d pages, %d bars'
          % (slug, _size(summary['bytes']), summary['pages'], summary['bars']))

  print('\n%d restored, %d already here, %d failed' % (restored, skipped, failed))
  return 0 if failed == 0 else 1


def _tools(settings: Settings) -> None:
  """What the pipeline shells out to, and how current it is.

  `yt-dlp` is the one that decays: YouTube changes how it serves video every few weeks and
  a fetch starts failing with something cryptic in the job log. Naming the version and the
  command to fix it is the difference between a five-minute update and an afternoon.
  """
  import importlib.metadata
  import subprocess

  def version(command: List[str]) -> str:
    try:
      out = subprocess.run(command, capture_output=True, text=True, timeout=20)
      first = (out.stdout or out.stderr or '').strip().splitlines()
      return first[0][:70] if first else 'no output'
    except Exception as e:                      # noqa: BLE001 - reporting, not doing
      return 'could not run: %s' % e

  print('tools:')
  print('  python    %s' % sys.version.split()[0])
  print('  ffmpeg    %s' % (version([settings.ffmpeg, '-version']) if settings.ffmpeg
                            else 'MISSING (needed to read every recording)'))
  print('  ffprobe   %s' % (version([settings.ffprobe, '-version']) if settings.ffprobe
                            else 'MISSING'))
  try:
    ytdlp = importlib.metadata.version('yt-dlp')
  except Exception:                              # noqa: BLE001
    ytdlp = None
  print('  yt-dlp    %s' % (ytdlp or 'MISSING (YouTube URLs will not work)'))
  if ytdlp:
    # Not the release date of the package -- the version string is the date for yt-dlp
    # (YYYY.MM.DD), which is the useful fact.
    print('            version %s is a date: update when it is more than a few weeks old'
          % ytdlp)
    print('            update: %s -m pip install -U yt-dlp' % sys.executable)
  print('  analyzer  %s' % ('present' if (settings.capabilities()['analyzer'])
                            else 'MISSING (nothing can be analysed on this machine)'))


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
