"""A finished piece as one file, so the machine that analysed it can hand it over.

The page reads exactly five things per piece -- `meta.json`, `map.json`, `geometry.json`,
`audio.wav` and `pages/NNNN.svg` -- so those are what a package carries, and nothing else.
In particular the score is *not* in it: the serving side never reads MusicXML (the engraving
happened on the worker, and what it produced is already SVG), and re-analysing is the
worker's job. That leaves ~16 MB per piece, nearly all of it the recording.

Two rules shape this file:

* **The slug is validated, not slugified.** `pieces.slugify` delegates to the analyzer's
  rules so the two agree on names, which means it imports the analyzer -- and the serving
  image deliberately has none (`test_serving_a_piece_needs_no_analyzer`). So a slug arriving
  over the wire is checked against a pattern instead: lowercase, digits and hyphens, which
  is the same alphabet the analyzer's slugify produces.
* **Nothing is trusted about the archive.** A zip is a list of names chosen by whoever built
  it, so every member is checked against the allowed set and written under a directory this
  code owns, which is what stops `../../etc/passwd` from being a piece.
"""

from __future__ import annotations

import io
import json
import pathlib
import re
import zipfile
from typing import Any, Dict, List, Optional

# What a package may contain. `CARRY` is what the packer writes when it is there;
# `REQUIRED` is what a piece cannot be served without.
CARRY = ('meta.json', 'map.json', 'geometry.json', 'audio.wav')
REQUIRED = ('meta.json', 'map.json', 'audio.wav')
PAGE_RE = re.compile(r'^pages/(\d{4})\.svg$')

# A slug is a directory name on the serving side, so this is a security boundary as much
# as a naming rule: ./ and .. and absolute paths cannot match it.
SLUG_RE = re.compile(r'^[a-z0-9][a-z0-9-]{0,63}$')

# A fixed timestamp for every member, so packing the same piece twice gives the same bytes.
# That is what makes "did this piece change?" answerable by comparing checksums.
EPOCH = (1980, 1, 1, 0, 0, 0)


class PackageError(ValueError):
  """Those bytes are not a piece package, and here is why."""


def valid_slug(slug: str) -> bool:
  """Is this a slug this service will write a directory for?"""
  return bool(SLUG_RE.match(slug or ''))


def pack_piece(directory: pathlib.Path) -> bytes:
  """The piece at `directory`, as one zip.

  Raises `PackageError` when the directory is not a servable piece, which is worth doing
  on the *sending* side: a worker that notices it has nothing to publish is easier to
  diagnose than a server that rejects an upload with the same information.
  """
  directory = pathlib.Path(directory)
  if not directory.is_dir():
    raise PackageError('no piece directory at %s' % directory)
  for name in REQUIRED:
    if not (directory / name).is_file():
      raise PackageError('%s is missing from %s' % (name, directory))

  members: List[str] = [name for name in CARRY if (directory / name).is_file()]
  members += sorted('pages/%s' % path.name for path in (directory / 'pages').glob('*.svg'))
  if not any(name.startswith('pages/') for name in members):
    raise PackageError('no engraved pages in %s' % directory)

  buffer = io.BytesIO()
  with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as archive:
    for name in members:
      info = zipfile.ZipInfo(name, date_time=EPOCH)
      info.compress_type = zipfile.ZIP_DEFLATED
      info.external_attr = 0o644 << 16
      archive.writestr(info, (directory / name).read_bytes())
  return buffer.getvalue()


def unpack_piece(payload: bytes, dest: pathlib.Path, *,
                 max_bytes: int) -> Dict[str, Any]:
  """Write a package into `dest` (which must not exist yet), or raise `PackageError`.

  Everything is checked before anything is written: a rejected package leaves no partial
  piece behind, because the caller installs `dest` by renaming it into place afterwards.
  """
  if not payload:
    raise PackageError('the upload arrived empty')
  try:
    archive = zipfile.ZipFile(io.BytesIO(payload))
  except zipfile.BadZipFile as e:
    raise PackageError('not a zip: %s' % e)

  infos = archive.infolist()
  if not infos:
    raise PackageError('the package is empty')
  names = set()
  total = 0
  for info in infos:
    name = info.filename
    if name.endswith('/'):
      continue
    if name.startswith('/') or '\\' in name or '..' in name.split('/'):
      raise PackageError('refusing a path that leaves the piece directory: %r' % name)
    if name not in CARRY and not PAGE_RE.match(name):
      raise PackageError('unexpected file in the package: %r (a piece carries %s and '
                         'pages/NNNN.svg)' % (name, ', '.join(CARRY)))
    # The declared size first, so a zip bomb is refused before it is inflated.
    total += info.file_size
    if total > max_bytes:
      raise PackageError('the package unpacks to more than %d MB' % (max_bytes >> 20))
    names.add(name)

  missing = [name for name in REQUIRED if name not in names]
  if missing:
    raise PackageError('the package is missing %s' % ', '.join(missing))
  if not any(name.startswith('pages/') for name in names):
    raise PackageError('the package carries no engraved pages')

  dest.mkdir(parents=True)
  written = 0
  for info in infos:
    if info.filename.endswith('/'):
      continue
    target = dest / info.filename
    target.parent.mkdir(parents=True, exist_ok=True)
    with archive.open(info) as source, open(target, 'wb') as handle:
      while True:
        block = source.read(1 << 20)
        if not block:
          break
        written += len(block)
        if written > max_bytes:
          # Cannot happen with the declared sizes checked above unless the archive lies
          # about them, which is exactly the case worth stopping on.
          raise PackageError('the package is larger than it declared: over %d MB'
                             % (max_bytes >> 20))
        handle.write(block)

  try:
    meta = json.loads((dest / 'meta.json').read_text(encoding='utf-8'))
  except (OSError, ValueError) as e:
    raise PackageError('meta.json is not readable JSON: %s' % e)
  if not isinstance(meta, dict):
    raise PackageError('meta.json is not an object')
  return {'title': meta.get('title') or '',
          'bars': meta.get('bars') or 0,
          'pages': sum(1 for name in names if name.startswith('pages/')),
          'bytes': written,
          'files': sorted(names),
          'score_sha256': ((meta.get('score') or {}).get('sha256') or ''),
          'meta': meta}


def package_url(base: str, slug: str) -> str:
  """Where a package for `slug` is PUT, given the *service root*.

  One place, so the two sides cannot disagree -- and it is the service root (`https://host`,
  or `https://host/note-seq` behind the broker's proxy) rather than the API base, because
  that is the URL somebody copies out of their browser and the same value
  `NOTE_SEQ_VIEWER_URL` takes. An address that already ends in `/api/v1` is accepted too:
  it is an easy thing to paste, and being strict about it would fail at the one moment
  nobody is watching, with a 405 that says nothing about the cause.
  """
  root = (base or '').rstrip('/')
  for suffix in ('/api/v1', '/api'):
    if root.endswith(suffix):
      root = root[:-len(suffix)]
  return '%s/api/v1/pieces/%s' % (root, slug)


def describe(directory: pathlib.Path) -> Optional[Dict[str, Any]]:
  """What a local piece directory carries, for a log line or a test."""
  directory = pathlib.Path(directory)
  if not (directory / 'meta.json').is_file():
    return None
  try:
    meta = json.loads((directory / 'meta.json').read_text(encoding='utf-8'))
  except (OSError, ValueError):
    return None
  return {'slug': meta.get('slug') or directory.name,
          'title': meta.get('title') or directory.name,
          'pages': len(list((directory / 'pages').glob('*.svg'))),
          'audio_bytes': (directory / 'audio.wav').stat().st_size
                         if (directory / 'audio.wav').is_file() else 0}
