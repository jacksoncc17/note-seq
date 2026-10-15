"""Pieces on disk: what a finished analysis produced, and how to read it back.

One directory per piece, holding everything the web page needs and nothing it does not:

    data/pieces/<slug>/
      score.musicxml    the score as uploaded
      score.mid         the steady reference MIDI the alignment was measured against
      audio.wav         22050 Hz mono PCM -- the clock everything else is timed in
      map.json          the measure map: bar -> start/end seconds, with confidences
      meta.json         title, composer, bars, duration, how well it fitted, warnings
      geometry.json     per page: viewBox and every measure's box, in SVG units
      pages/0001.svg    the engraving, one file per page

`map.json` and `geometry.json` are the two files the browser is actually built on: the
first says *when* each bar sounds, the second says *where* it is on the page. Keeping
them as files rather than recomputing them per request means the score page is served
by reading two small JSON documents, whatever the piece's size.
"""

from __future__ import annotations

import hashlib
import json
import pathlib
import shutil
from typing import Any, Dict, List, Optional

from .config import Settings

# What an uploaded score may be called. Verovio reads all of them; `.mxl` is the zipped
# MusicXML that notation programs export by default.
SCORE_SUFFIXES = ('.musicxml', '.xml', '.mxl')
AUDIO_SUFFIXES = ('.mp3', '.wav', '.flac', '.m4a', '.aac', '.ogg', '.opus', '.wma', '.webm')


class PieceError(RuntimeError):
  """The upload is not something this service can analyse."""


def slugify(value: str) -> str:
  """A filename-safe slug, by the analyzer's rules so the two agree on names."""
  from analyzer import library as _library
  slug = _library.slugify(value)
  return slug or 'piece'


def fingerprint(path) -> str:
  """The first 16 hex digits of a file's sha256: enough to tell two uploads apart."""
  digest = hashlib.sha256()
  with open(path, 'rb') as handle:
    for block in iter(lambda: handle.read(1 << 20), b''):
      digest.update(block)
  return digest.hexdigest()[:16]


class PieceStore:
  """Read and write `data/pieces/`."""

  # Never a piece, whatever is inside: `.building` is one being assembled by `write` or by
  # an upload, `.previous` is one that was just replaced. Both hold a real `meta.json` --
  # written *before* the rename, so that a crash leaves something identifiable -- which is
  # why a listing cannot be filtered by "does it look like a piece". Going by that showed a
  # half-written piece in the list, under a name like `waltz.building`, for as long as an
  # analysis took. Found by a housekeeping test.
  STAGING_SUFFIXES = ('.building', '.previous')

  def __init__(self, settings: Settings):
    self.settings = settings

  # ------------------------------------------------------------------ paths

  def directory(self, slug: str) -> pathlib.Path:
    return self.settings.piece_dir(slug)

  def path(self, slug: str, name: str) -> pathlib.Path:
    return self.directory(slug) / name

  def page_path(self, slug: str, number: int) -> pathlib.Path:
    return self.directory(slug) / 'pages' / ('%04d.svg' % int(number))

  def exists(self, slug: str) -> bool:
    return self.path(slug, 'meta.json').is_file()

  def slugs(self) -> List[str]:
    if not self.settings.pieces_dir.is_dir():
      return []
    return sorted((p.name for p in self.settings.pieces_dir.iterdir()
                   if (p / 'meta.json').is_file()
                   and not p.name.endswith(self.STAGING_SUFFIXES)))

  # ------------------------------------------------------------------ reading

  def meta(self, slug: str) -> Optional[Dict[str, Any]]:
    return _read_json(self.path(slug, 'meta.json'))

  def map(self, slug: str) -> Optional[Dict[str, Any]]:
    return _read_json(self.path(slug, 'map.json'))

  def geometry(self, slug: str) -> Optional[Dict[str, Any]]:
    return _read_json(self.path(slug, 'geometry.json'))

  def audio(self, slug: str) -> pathlib.Path:
    return self.path(slug, 'audio.wav')

  def listing(self) -> List[Dict[str, Any]]:
    """Every piece, newest first, as the file list shows it -- cheap, from meta only."""
    out = []
    for slug in self.slugs():
      meta = self.meta(slug) or {}
      out.append({'slug': slug,
                  'title': meta.get('title') or slug,
                  'composer': meta.get('composer') or '',
                  'bars': meta.get('bars') or 0,
                  'duration_s': meta.get('duration_s') or 0.0,
                  'created_at': meta.get('created_at') or 0.0,
                  'pages': meta.get('pages') or 0,
                  'warnings': len(meta.get('warnings') or [])})
    out.sort(key=lambda row: row['created_at'], reverse=True)
    return out

  # ------------------------------------------------------------------ writing

  def claim_slug(self, base: str, score_fingerprint: str) -> str:
    """A slug for a new analysis, reusing it when the score is one already here.

    Re-uploading the same score with a better recording should replace that piece
    rather than pile up `-2`, `-3`, ... beside it; uploading a *different* score that
    happens to share a name must not silently overwrite the first one. The stored
    fingerprint is what tells the two apart.
    """
    base = slugify(base)
    if not self.exists(base):
      return base
    meta = self.meta(base) or {}
    stored = ((meta.get('score') or {}).get('sha256') or '')
    if stored and stored == score_fingerprint:
      return base
    for index in range(2, 100):
      candidate = '%s-%d' % (base, index)
      if not self.exists(candidate):
        return candidate
    raise PieceError('too many pieces named %r' % base)

  def write(self, slug: str, *, score: pathlib.Path, reference_midi: pathlib.Path,
            audio: pathlib.Path, map_dict: Dict[str, Any], geometry: Dict[str, Any],
            pages: List[Any], meta: Dict[str, Any]) -> pathlib.Path:
    """Store a finished analysis, replacing whatever was at `slug` before.

    The directory is built beside the target and moved into place at the end, so a
    failure half way through leaves the previous piece intact rather than a mixture of
    two attempts -- the web page can be open on it while a re-analysis runs.
    """
    target = self.directory(slug)
    staging = target.parent / (slug + '.building')
    if staging.exists():
      shutil.rmtree(staging)
    (staging / 'pages').mkdir(parents=True)
    shutil.copyfile(score, staging / 'score.musicxml')
    shutil.copyfile(reference_midi, staging / 'score.mid')
    shutil.copyfile(audio, staging / 'audio.wav')
    _write_json(staging / 'map.json', map_dict)
    _write_json(staging / 'geometry.json', geometry)
    _write_json(staging / 'meta.json', meta)
    for page in pages:
      (staging / 'pages' / ('%04d.svg' % page.number)).write_text(page.svg,
                                                                  encoding='utf-8')
    if target.exists():
      previous = target.parent / (slug + '.previous')
      if previous.exists():
        shutil.rmtree(previous)
      target.rename(previous)
      staging.rename(target)
      shutil.rmtree(previous, ignore_errors=True)
    else:
      staging.rename(target)
    return target

  def delete(self, slug: str) -> bool:
    target = self.directory(slug)
    if not target.is_dir():
      return False
    shutil.rmtree(target)
    return True

  def staging_path(self, slug: str) -> pathlib.Path:
    """Where a piece is assembled before it is moved into place.

    Same name `write` uses, so an interrupted upload leaves the same kind of debris as an
    interrupted analysis -- and `swab()` below can clear either.
    """
    return self.settings.pieces_dir / (slug + '.building')

  def install(self, slug: str, staging: pathlib.Path) -> pathlib.Path:
    """Move an assembled directory into place, replacing whatever was there.

    This is the other half of the piece-package upload (`uploads.py`): a worker sends the
    files, the API unpacks them into `staging_path(slug)`, and this swaps them in with the
    same two renames `write` uses. The page keeps serving the old piece until the new one
    is complete, and a crash in between leaves `.previous` behind rather than a piece that
    is half one analysis and half another.
    """
    staging = pathlib.Path(staging)
    if not (staging / 'meta.json').is_file():
      raise PieceError('nothing staged at %s' % staging)
    target = self.directory(slug)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
      previous = target.parent / (slug + '.previous')
      if previous.exists():
        shutil.rmtree(previous)
      target.rename(previous)
      staging.rename(target)
      shutil.rmtree(previous, ignore_errors=True)
    else:
      staging.rename(target)
    return target

  def swab_plan(self) -> List[Dict[str, Any]]:
    """What `swab()` would do, so a dry run can say it without doing it.

    Both kinds of debris come from the two-rename swap used by `write` and `install`:

    * `<slug>.building` -- an assembly that never completed: **remove**.
    * `<slug>.previous` -- the piece that was replaced, normally deleted immediately after
      the swap. If a crash landed between the two renames the target is *missing* and this
      is the only complete copy left, so it is **restored** rather than thrown away.

    A caller that removes these itself instead of asking would lose the second case, which
    is why this exists: `service.app.manage --scratch` reports and applies this plan rather
    than globbing the directory.
    """
    root = self.settings.pieces_dir
    plan: List[Dict[str, Any]] = []
    if not root.is_dir():
      return plan
    for path in sorted(root.iterdir()):
      if not path.is_dir():
        continue
      if path.name.endswith('.building'):
        plan.append({'action': 'remove', 'path': str(path), 'name': path.name,
                     'reason': 'an assembly that never finished'})
      elif path.name.endswith('.previous'):
        slug = path.name[:-len('.previous')]
        target = self.directory(slug)
        if (path / 'meta.json').is_file() and not (target / 'meta.json').is_file():
          plan.append({'action': 'restore', 'path': str(path), 'name': path.name,
                       'slug': slug,
                       'reason': 'the only complete copy of %s, left by a crash between '
                                 'the two renames' % slug})
        else:
          plan.append({'action': 'remove', 'path': str(path), 'name': path.name,
                       'reason': 'the piece it replaced is in place'})
    return plan

  def swab(self) -> List[str]:
    """Apply `swab_plan`: clear half-built pieces, restore one a crash displaced.

    Called once at startup rather than on a timer: these directories only appear when a
    process died mid-write, and a service that starts on top of one fills a disk quietly.
    """
    done: List[str] = []
    for step in self.swab_plan():
      path = pathlib.Path(step['path'])
      if step['action'] == 'restore':
        path.rename(self.directory(step['slug']))
        done.append('%s -> restored as %s' % (step['name'], step['slug']))
      else:
        shutil.rmtree(path, ignore_errors=True)
        done.append(step['name'])
    return done


def _read_json(path: pathlib.Path) -> Optional[Dict[str, Any]]:
  if not path.is_file():
    return None
  try:
    return json.loads(path.read_text(encoding='utf-8'))
  except (OSError, ValueError):
    return None


def _write_json(path: pathlib.Path, payload: Dict[str, Any]) -> None:
  path.write_text(json.dumps(payload, indent=1, sort_keys=True), encoding='utf-8')
