"""Where a piece's data lives, behind interfaces rather than paths.

Today everything is files: question banks under `memorization/output/pieces/`, a
recording per piece in `analyzer/cache/audio/`, a map in `analyzer/output/sync/`
and engraved pages in `analyzer/output/render/`. That works, but the *callers* --
the window, the HTTP API, the phone client -- should not know it. They ask a
repository for piece records, blob handles and maps, and this module decides what
that means on disk.

The point of the exercise is the future: if the library outgrows files (hundreds
of pieces, a search box, tags, several machines), switching to SQLite or Postgres
should mean writing one adapter and changing one line, not editing callers.

Four ports, and one thing worth knowing about each:

* ``PieceStore``  -- the catalogue: list/get/upsert/remove, with a query object
  rather than a path convention, so ``list()`` can become a SQL WHERE clause.
* ``BlobStore``   -- bytes with an opaque key. ``local_path()`` exists because
  soundfile, Qt and Verovio want a file *path*; a database adapter answers it by
  materialising the blob into a scratch directory, which is the only place a
  non-file backend needs to touch disk.
* ``MapStore``    -- the measure -> time map, the app's most valuable data.
* ``RenderStore`` -- engraved pages, which are reproducible and so cacheable.

Two adapters ship here on purpose. ``filesystem_repository`` is the real one and
reads today's layout untouched -- no migration, existing caches keep working.
``memory_repository`` exists to keep the ports honest: if the filesystem adapter
were the only implementation, filesystem assumptions would creep into the
interfaces unnoticed. Both run the same contract tests in
`tests/test_store_contract.py`, and a database adapter is expected to be dropped
into that same suite.
"""

from __future__ import annotations

import abc
import dataclasses
import json
import os
import pathlib
import re
import shutil
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

from .config import AnalyzerConfig
from .schema import AudioSource, SyncResult


# ---------------------------------------------------------------------------
# records: what callers see
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class BlobRef:
  """A handle to some bytes, with an opaque key.

  The key means nothing to callers; only the store that issued it can resolve it.
  The filesystem adapter uses paths, a database adapter would use row ids or
  content hashes -- and callers must not care which.
  """

  key: str
  media_type: str = 'application/octet-stream'
  size: int = 0

  @property
  def suffix(self) -> str:
    """A sensible file extension, for code that insists on one."""
    known = {'.musicxml': 'application/vnd.recordare.musicxml+xml',
             '.mxl': 'application/vnd.recordare.musicxml',
             '.xml': 'application/xml',
             '.pdf': 'application/pdf',
             '.wav': 'audio/wav',
             '.mid': 'audio/midi',
             '.png': 'image/png'}
    for suffix, media in known.items():
      if self.media_type == media:
        return suffix
    return pathlib.PurePosixPath(self.key).suffix or ''

  def to_dict(self) -> Dict[str, Any]:
    return {'key': self.key, 'media_type': self.media_type, 'size': self.size}

  @classmethod
  def from_dict(cls, data: Dict[str, Any]) -> 'BlobRef':
    return cls(key=str(data.get('key', '')), media_type=str(data.get('media_type', '')),
               size=int(data.get('size') or 0))


@dataclass
class PieceRecord:
  """One piece in the library: derived facts plus a little curation.

  `bars`, `key` and the reference MIDI come from the question bank (built from the
  score by `memorization/analyze.py`); `title`, `composer` and `tags` are what a
  person chose, so they live in the catalogue rather than in the bank -- which is
  exactly the split a database would bake into columns.
  """

  slug: str
  title: str = ''
  composer: str = ''
  bars: int = 0
  key: str = ''
  tags: List[str] = field(default_factory=list)
  notes: List[str] = field(default_factory=list)

  score: Optional[BlobRef] = None
  pdf: Optional[BlobRef] = None
  audio: Optional[BlobRef] = None
  reference_midi: Optional[BlobRef] = None

  audio_duration_s: float = 0.0
  audio_url: str = ''
  audio_source_id: str = ''

  map_method: str = ''
  map_measures: int = 0

  hidden: bool = False
  updated_at: str = ''

  @property
  def has_score(self) -> bool:
    return self.score is not None

  @property
  def has_audio(self) -> bool:
    return self.audio is not None

  @property
  def has_map(self) -> bool:
    return bool(self.map_method) and self.map_measures > 0

  @property
  def display_name(self) -> str:
    if self.title:
      return self.title
    return self.slug.replace('-', ' ').strip().capitalize()

  def describe(self) -> str:
    bits = ['%d bars' % self.bars if self.bars else 'bars unknown']
    if self.key:
      bits.append(self.key)
    bits.append('audio' if self.has_audio else 'no audio')
    if self.has_map:
      bits.append(self.map_method + ' map')
    return '%s (%s)' % (self.display_name, ', '.join(bits))

  def to_dict(self) -> Dict[str, Any]:
    out = dataclasses.asdict(self)
    for name in ('score', 'pdf', 'audio', 'reference_midi'):
      ref = getattr(self, name)
      out[name] = ref.to_dict() if ref else None
    return out

  @classmethod
  def from_dict(cls, data: Dict[str, Any]) -> 'PieceRecord':
    known = {f.name for f in dataclasses.fields(cls)}
    kwargs = {k: v for k, v in data.items() if k in known and k not in
              ('score', 'pdf', 'audio', 'reference_midi')}
    for name in ('score', 'pdf', 'audio', 'reference_midi'):
      raw = data.get(name)
      kwargs[name] = BlobRef.from_dict(raw) if isinstance(raw, dict) else None
    kwargs.setdefault('tags', [])
    kwargs.setdefault('notes', [])
    return cls(**kwargs)


@dataclass(frozen=True)
class PieceQuery:
  """What to list. A query object rather than a path pattern, so a database can
  compile it to SQL later without changing any caller."""

  text: str = ''                      # matched against title, composer, slug, tags
  has_audio: Optional[bool] = None
  has_map: Optional[bool] = None
  tag: str = ''
  include_hidden: bool = False
  order_by: str = 'audio_first'
  limit: int = 0                      # 0 means no limit

  ORDER_AUDIO_FIRST = 'audio_first'
  ORDER_TITLE = 'title'
  ORDER_UPDATED = 'updated'
  ORDER_SLUG = 'slug'
  ORDER_ADDED = 'added'


def matches(record: PieceRecord, query: PieceQuery) -> bool:
  """The filter both adapters share, so they cannot disagree about a query."""
  if record.hidden and not query.include_hidden:
    return False
  if query.has_audio is not None and record.has_audio != query.has_audio:
    return False
  if query.has_map is not None and record.has_map != query.has_map:
    return False
  if query.tag and query.tag not in record.tags:
    return False
  if query.text:
    needle = query.text.strip().lower()
    haystack = ' '.join([record.title, record.composer, record.slug, record.key]
                        + list(record.tags)).lower()
    if needle not in haystack:
      return False
  return True


def sort_records(records: List[PieceRecord], order_by: str) -> List[PieceRecord]:
  """The ordering both adapters share."""
  if order_by == PieceQuery.ORDER_TITLE:
    return sorted(records, key=lambda r: (r.title or r.slug).lower())
  if order_by == PieceQuery.ORDER_UPDATED:
    return sorted(records, key=lambda r: r.updated_at, reverse=True)
  if order_by == PieceQuery.ORDER_SLUG:
    return sorted(records, key=lambda r: r.slug)
  # default: the pieces you can actually practise come first
  return sorted(records, key=lambda r: (0 if r.has_audio else 1, 0 if r.has_map else 1,
                                        (r.title or r.slug).lower()))


# ---------------------------------------------------------------------------
# ports
# ---------------------------------------------------------------------------

class BlobStore(abc.ABC):
  """Bytes, addressed by an opaque key."""

  @abc.abstractmethod
  def read(self, ref: BlobRef) -> bytes:
    """The whole blob.

    Raises:
      KeyError: when the blob is not in the store.
    """

  @abc.abstractmethod
  def write(self, key: str, data: bytes, media_type: str = 'application/octet-stream') -> BlobRef:
    """Store bytes under `key` (replacing anything already there)."""

  @abc.abstractmethod
  def local_path(self, ref: BlobRef) -> pathlib.Path:
    """A real path to the blob's bytes.

    Needed by everything that is not Python: soundfile, Qt's pixmap loader and
    Verovio all want a filename. A store that keeps bytes in a database answers
    this by extracting to a scratch directory, which callers may treat as
    read-only and short-lived.
    """

  @abc.abstractmethod
  def exists(self, ref: BlobRef) -> bool: ...

  @abc.abstractmethod
  def size(self, ref: BlobRef) -> int: ...

  @abc.abstractmethod
  def delete(self, ref: BlobRef) -> bool: ...


class PieceStore(abc.ABC):
  """The catalogue."""

  @abc.abstractmethod
  def list(self, query: Optional[PieceQuery] = None) -> List[PieceRecord]: ...

  @abc.abstractmethod
  def get(self, slug: str) -> Optional[PieceRecord]: ...

  @abc.abstractmethod
  def upsert(self, record: PieceRecord) -> PieceRecord:
    """Add or update a record's curated fields (title, composer, tags, hidden).

    Derived facts -- bar count, key, the audio's duration -- are owned by whatever
    produced them and are not overwritten by an upsert.
    """

  @abc.abstractmethod
  def remove(self, slug: str) -> bool:
    """Take a piece out of the library, along with the artifacts this app made
    for it. Returns True when something was there to remove."""

  def count(self, query: Optional[PieceQuery] = None) -> int:
    return len(self.list(query))


class MapStore(abc.ABC):
  """Measure -> time maps, the data that is expensive to produce."""

  @abc.abstractmethod
  def get(self, slug: str): ...                     # -> Optional[syncmap.StoredSync]

  @abc.abstractmethod
  def put(self, slug: str, result: SyncResult, *, audio: Optional[AudioSource] = None,
          source_score: str = '', played: Optional[Sequence[int]] = None) -> None: ...

  @abc.abstractmethod
  def delete(self, slug: str) -> bool: ...


class RenderStore(abc.ABC):
  """Engraved pages. Reproducible from the score, so a cache rather than a truth."""

  @abc.abstractmethod
  def get(self, slug: str): ...                     # -> Optional[ScoreRender]

  @abc.abstractmethod
  def put(self, slug: str, render, images: Optional[Mapping[int, bytes]] = None) -> None: ...

  @abc.abstractmethod
  def delete(self, slug: str) -> bool: ...


# ---------------------------------------------------------------------------
# the facade callers actually hold
# ---------------------------------------------------------------------------

@dataclass
class Repository:
  """The four stores for one library, plus which backend is behind them."""

  pieces: PieceStore
  blobs: BlobStore
  maps: MapStore
  renders: RenderStore
  backend: str = 'filesystem'

  def close(self) -> None:
    """Release anything the backend holds open (a database connection, say)."""
    for store in (self.pieces, self.blobs, self.maps, self.renders):
      closer = getattr(store, 'close', None)
      if callable(closer):
        closer()


def open_repository(cfg: Optional[AnalyzerConfig] = None,
                    backend: Optional[str] = None) -> Repository:
  """Open the library.

  This is the one line that would change to move to a database:

      repo = store.open_repository(cfg)          # backend from cfg.backend

  Add an adapter, register it here, and every caller -- window, HTTP server,
  phone client -- follows without being edited.
  """
  cfg = cfg or AnalyzerConfig()
  name = (backend or getattr(cfg, 'backend', 'filesystem') or 'filesystem').lower()
  if name in ('filesystem', 'fs', 'files', 'json'):
    return filesystem_repository(cfg)
  if name in ('memory', 'ram', 'test'):
    return memory_repository()
  raise ValueError('unknown storage backend: %r (known: filesystem, memory)' % name)


# ---------------------------------------------------------------------------
# adapter one: the filesystem, reading today's layout
# ---------------------------------------------------------------------------

_MEDIA = {'.musicxml': 'application/vnd.recordare.musicxml+xml',
          '.mxl': 'application/vnd.recordare.musicxml',
          '.xml': 'application/xml',
          '.pdf': 'application/pdf',
          '.wav': 'audio/wav',
          '.mid': 'audio/midi',
          '.png': 'image/png'}


def media_type_of(path) -> str:
  return _MEDIA.get(pathlib.Path(path).suffix.lower(), 'application/octet-stream')


def slugify(source) -> str:
  """A stable, filesystem-friendly key from a score path or filename.

  Deliberately identical to what `memorization.pieces.slugify` produces, so the
  same score resolves to the same slug in the analyzer, the quiz and the tutor --
  and public here because the catalogue has to reproduce its own keys.
  """
  base = pathlib.Path(str(source or '')).name
  base = re.sub(r'\.(musicxml|xml|mxl|mid|midi)$', '', base, flags=re.I)
  return re.sub(r'[^a-z0-9]+', '-', base.lower()).strip('-') or 'piece'


def _blob(path) -> Optional[BlobRef]:
  path = pathlib.Path(path)
  if not path.is_file():
    return None
  try:
    size = path.stat().st_size
  except OSError:
    return None
  return BlobRef(key=str(path), media_type=media_type_of(path), size=size)


def _now() -> str:
  return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())


class _FilesystemBlobs(BlobStore):
  """Blobs are just files; the key is the path.

  A missing file is reported as `KeyError`, like every other backend, rather than
  letting `FileNotFoundError` escape: callers are written against the port, not
  against the filesystem's exceptions.
  """

  def read(self, ref: BlobRef) -> bytes:
    try:
      return pathlib.Path(ref.key).read_bytes()
    except OSError as e:
      raise KeyError(ref.key) from e

  def write(self, key: str, data: bytes, media_type: str = 'application/octet-stream') -> BlobRef:
    path = pathlib.Path(key)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + '.tmp')
    temp.write_bytes(data)
    os.replace(temp, path)
    return BlobRef(key=str(path), media_type=media_type, size=len(data))

  def local_path(self, ref: BlobRef) -> pathlib.Path:
    path = pathlib.Path(ref.key)
    if not path.is_file():
      raise KeyError(ref.key)
    return path

  def exists(self, ref: BlobRef) -> bool:
    return pathlib.Path(ref.key).is_file()

  def size(self, ref: BlobRef) -> int:
    try:
      return pathlib.Path(ref.key).stat().st_size
    except OSError:
      return 0

  def delete(self, ref: BlobRef) -> bool:
    try:
      pathlib.Path(ref.key).unlink()
      return True
    except OSError:
      return False


class _FilesystemMaps(MapStore):
  """Delegates to `syncmap`, which already owns the JSON format and the
  staleness fingerprints. No second implementation of that logic."""

  def __init__(self, cfg: AnalyzerConfig):
    self.cfg = cfg

  def get(self, slug: str):
    from . import syncmap
    return syncmap.load(self.cfg, slug)

  def put(self, slug: str, result: SyncResult, *, audio: Optional[AudioSource] = None,
          source_score: str = '', played: Optional[Sequence[int]] = None) -> None:
    from . import syncmap
    syncmap.save(self.cfg, slug, result, audio=audio,
                 source_musicxml=source_score or None,
                 played=list(played) if played else None)

  def delete(self, slug: str) -> bool:
    from . import syncmap
    return syncmap.delete(self.cfg, slug)


class _FilesystemRenders(RenderStore):
  """Engraved pages under `output/render/score-<slug>/`.

  The cache is keyed by the *score's* size and mtime rather than by the slug, so
  this store asks the piece store where the score is instead of taking a path from
  its caller -- a path parameter here would leak the filesystem into the port.
  """

  def __init__(self, cfg: AnalyzerConfig, pieces: 'PieceStore'):
    self.cfg = cfg
    self.pieces = pieces

  def _score_path(self, slug: str) -> Optional[pathlib.Path]:
    record = self.pieces.get(slug)
    if record is None or record.score is None:
      return None
    return pathlib.Path(record.score.key)

  def get(self, slug: str):
    from . import scoreview
    score = self._score_path(slug)
    if score is None:
      return None
    return scoreview.cached_render(self.cfg, score)

  def put(self, slug: str, render, images: Optional[Mapping[int, bytes]] = None) -> None:
    from . import scoreview
    if images:
      for page in render.pages:
        provided = images.get(page.number)
        target = pathlib.Path(page.image)
        if provided is not None and not target.is_file():
          target.parent.mkdir(parents=True, exist_ok=True)
          target.write_bytes(provided)
    score = self._score_path(slug)
    if score is not None:
      scoreview.write_cache(self.cfg, score, render)
    else:
      # a render for a piece whose score has gone: keep the manifest anyway
      directory = self.cfg.render_dir / ('score-%s' % slug)
      directory.mkdir(parents=True, exist_ok=True)
      (directory / 'pages.json').write_text(json.dumps(render.to_dict()), encoding='utf-8')

  def delete(self, slug: str) -> bool:
    directory = self.cfg.render_dir / ('score-%s' % slug)
    if directory.is_dir():
      shutil.rmtree(directory, ignore_errors=True)
      return True
    return False


class _FilesystemPieces(PieceStore):
  """Reads the question banks and the analyzer's own sidecars.

  One behaviour worth knowing, because it is the biggest visible difference from a
  database backend: this adapter **references the score where it already lives**
  (usually the user's own sheet-music folder) instead of copying it. Registering a
  piece therefore records a *reference*, and if that file is later moved or
  deleted the piece stays in the catalogue but reports that its score is gone. A
  database adapter would import the bytes at upsert time, and callers see only the
  difference between "has a score" and "does not" -- which is why this choice sits
  behind the port rather than in a caller.

  Two sources, deliberately kept apart:

  * the **bank** (`fingerprint.json`, written by `memorization/analyze.py`) owns
    derived facts -- bar count, key, the score it came from, the reference MIDI;
  * an analyzer **sidecar** (`output/library/<slug>.json`) owns curated facts --
    a title override, composer, tags, hidden. That is the row a database would
    hold, and it is what `upsert` writes.
  """

  def __init__(self, cfg: AnalyzerConfig, maps: MapStore):
    self.cfg = cfg
    self.maps = maps

  # -- reading -------------------------------------------------------------

  def sidecar_path(self, slug: str) -> pathlib.Path:
    return self.cfg.library_dir / ('%s.json' % slug)

  def _sidecar(self, slug: str) -> Dict[str, Any]:
    path = self.sidecar_path(slug)
    try:
      data = json.loads(path.read_text(encoding='utf-8'))
      return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
      return {}

  def _bank_paths(self) -> List[tuple]:
    """Every bank on disk, as (path, slug hint).

    The hint is the directory name for a per-piece bank, and None for the older
    single-file bank, whose slug has to come from the score it names. Deciding by
    filename alone got this wrong: both are called `fingerprint.json`.
    """
    found: List[tuple] = []
    if self.cfg.bank_dir.is_dir():
      found.extend((path, path.parent.name)
                   for path in sorted(self.cfg.bank_dir.glob('*/fingerprint.json')))
    if self.cfg.legacy_bank.is_file():
      found.append((self.cfg.legacy_bank, None))
    return found

  def _from_bank(self, bank: pathlib.Path, slug_hint: Optional[str] = None) -> Optional[PieceRecord]:
    try:
      data = json.loads(bank.read_text(encoding='utf-8'))
    except (OSError, ValueError):
      data = None
    if not isinstance(data, dict):
      # A bank we cannot read must not make its piece disappear silently: keep it
      # listed, with a note, so the user sees what is wrong.
      if slug_hint:
        return PieceRecord(slug=slug_hint,
                           notes=['question bank at %s could not be read' % bank])
      return None
    glob = data.get('global') or {}
    source = data.get('source_file') or ''
    slug = slug_hint or slugify(source)
    score = _blob(source) if source else None
    record = PieceRecord(
        slug=slug,
        title=str(glob.get('title') or ''),
        bars=int(glob.get('num_measures') or len(data.get('measures') or [])),
        key=str(glob.get('key_from_score') or glob.get('key_inferred') or ''),
        score=score,
        pdf=_blob(pathlib.Path(source).with_suffix('.pdf')) if source else None,
        reference_midi=(_blob(bank.parent / 'fingerprint_steady.mid')
                        or _blob(bank.parent / 'fingerprint.mid')))
    if source and score is None:
      record.notes.append('the score this bank was built from is gone: %s' % source)
    record.notes.extend(str(w) for w in (data.get('warnings') or []))

    audio_path = self.cfg.audio_path(slug)
    audio = _blob(audio_path) if audio_path.is_file() and audio_path.stat().st_size > 44 else None
    if audio is not None:
      record.audio = audio
      meta = self._audio_meta(slug)
      record.audio_duration_s = float(meta.get('duration_s') or 0.0)
      record.audio_url = str(meta.get('url') or '')
      record.audio_source_id = str(meta.get('source_id') or '')

    stored = self.maps.get(slug)
    if stored is not None:
      record.map_method = stored.method
      record.map_measures = len(stored.result.measures)
      record.updated_at = stored.updated_at
    return record

  def _audio_meta(self, slug: str) -> Dict[str, Any]:
    path = self.cfg.audio_meta_path(slug)
    try:
      data = json.loads(path.read_text(encoding='utf-8'))
      return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
      return {}

  def _curate(self, record: PieceRecord) -> PieceRecord:
    sidecar = self._sidecar(record.slug)
    if not sidecar:
      return record
    if sidecar.get('title'):
      record.title = str(sidecar['title'])
    if sidecar.get('composer'):
      record.composer = str(sidecar['composer'])
    if sidecar.get('tags'):
      record.tags = [str(t) for t in sidecar['tags']]
    record.hidden = bool(sidecar.get('hidden'))
    return record

  def get(self, slug: str) -> Optional[PieceRecord]:
    for bank, hint in self._bank_paths():
      try:
        record = self._from_bank(bank, hint)
      except (OSError, ValueError):
        continue
      if record is not None and record.slug == slug:
        return self._curate(record)
    # a piece can exist without a bank: it was opened from a score directly
    sidecar = self._sidecar(slug)
    if sidecar:
      record = PieceRecord.from_dict(sidecar.get('record') or {})
      record.slug = slug
      return self._curate(record)
    return None

  def list(self, query: Optional[PieceQuery] = None) -> List[PieceRecord]:
    query = query or PieceQuery()
    records: Dict[str, PieceRecord] = {}
    for bank, hint in self._bank_paths():
      record = self._from_bank(bank, hint)
      if record is None:
        continue
      records.setdefault(record.slug, self._curate(record))
    for path in sorted(self.cfg.library_dir.glob('*.json')) if self.cfg.library_dir.is_dir() else []:
      slug = path.stem
      if slug in records:
        continue
      data = self._sidecar(slug)
      if data.get('record'):
        records[slug] = self._curate(PieceRecord.from_dict(data['record']))
    kept = [r for r in records.values() if matches(r, query)]
    ordered = sort_records(kept, query.order_by)
    return ordered[:query.limit] if query.limit else ordered

  # -- writing -------------------------------------------------------------

  def upsert(self, record: PieceRecord) -> PieceRecord:
    """Persist the curated fields; derived facts stay with their producer."""
    self.cfg.ensure_dirs()
    current = self.get(record.slug)
    curated = {
        'title': record.title,
        'composer': record.composer,
        'tags': list(record.tags),
        'hidden': bool(record.hidden),
        'updated_at': _now(),
        # a piece with no bank still needs its own facts remembered
        'record': record.to_dict() if current is None else None,
    }
    path = self.sidecar_path(record.slug)
    temp = path.with_suffix('.json.tmp')
    temp.write_text(json.dumps(curated, indent=2), encoding='utf-8')
    os.replace(temp, path)
    return self.get(record.slug) or record

  def remove(self, slug: str) -> bool:
    """Hide the piece and drop the analyzer's own artifacts for it.

    The question bank belongs to the quiz and tutor tools, so it is left alone;
    `hidden` is what keeps the piece out of this app's lists.
    """
    existing = self.get(slug)
    if existing is None:
      return False
    record = PieceRecord(slug=slug, title=existing.title, composer=existing.composer,
                         tags=existing.tags, hidden=True)
    self.upsert(record)
    if existing.audio is not None:
      _FilesystemBlobs().delete(existing.audio)
      pathlib.Path(self.cfg.audio_meta_path(slug)).unlink(missing_ok=True)
    self.maps.delete(slug)
    _FilesystemRenders(self.cfg, self).delete(slug)
    return True


class FilesystemRepository(Repository):
  """The shipping backend: today's files, today's layout, no migration."""

  def __init__(self, cfg: AnalyzerConfig):
    self.cfg = cfg
    maps = _FilesystemMaps(cfg)
    pieces = _FilesystemPieces(cfg, maps)
    super().__init__(pieces=pieces, blobs=_FilesystemBlobs(), maps=maps,
                     renders=_FilesystemRenders(cfg, pieces), backend='filesystem')


def filesystem_repository(cfg: Optional[AnalyzerConfig] = None) -> Repository:
  return FilesystemRepository(cfg or AnalyzerConfig())


# ---------------------------------------------------------------------------
# adapter two: memory, which keeps the ports honest
# ---------------------------------------------------------------------------

class _MemoryBlobs(BlobStore):
  """Bytes in a dict. `local_path` has to write somewhere, so it uses a scratch
  directory and says so -- exactly what a database adapter would do."""

  def __init__(self, scratch: pathlib.Path):
    self._data: Dict[str, bytes] = {}
    self._types: Dict[str, str] = {}
    self.scratch = scratch

  def read(self, ref: BlobRef) -> bytes:
    if ref.key not in self._data:
      raise KeyError(ref.key)
    return self._data[ref.key]

  def write(self, key: str, data: bytes, media_type: str = 'application/octet-stream') -> BlobRef:
    self._data[key] = bytes(data)
    self._types[key] = media_type
    return BlobRef(key=key, media_type=media_type, size=len(data))

  def local_path(self, ref: BlobRef) -> pathlib.Path:
    if ref.key not in self._data:
      raise KeyError(ref.key)
    self.scratch.mkdir(parents=True, exist_ok=True)
    suffix = ref.suffix
    path = self.scratch / ('%s%s' % (re.sub(r'\W+', '_', ref.key), suffix))
    if not path.is_file() or path.stat().st_size != len(self._data[ref.key]):
      path.write_bytes(self._data[ref.key])
    return path

  def exists(self, ref: BlobRef) -> bool:
    return ref.key in self._data

  def size(self, ref: BlobRef) -> int:
    return len(self._data.get(ref.key, b''))

  def delete(self, ref: BlobRef) -> bool:
    return self._data.pop(ref.key, None) is not None


class _MemoryPieces(PieceStore):
  """Records in a dict.

  Map state is filled in from the MapStore on every read, the same way the
  filesystem adapter derives it from the map files: `has_map` must not mean one
  thing in one backend and something else in another.
  """

  def __init__(self, maps: Optional[MapStore] = None):
    self._records: Dict[str, PieceRecord] = {}
    self.maps = maps

  def _with_map(self, record: PieceRecord) -> PieceRecord:
    stored = self.maps.get(record.slug) if self.maps else None
    if stored is None:
      record.map_method, record.map_measures = '', 0
    else:
      record.map_method = stored.method
      record.map_measures = len(stored.result.measures)
    return record

  def list(self, query: Optional[PieceQuery] = None) -> List[PieceRecord]:
    query = query or PieceQuery()
    kept = [self._with_map(r) for r in self._records.values() if matches(self._with_map(r), query)]
    ordered = sort_records(kept, query.order_by)
    return ordered[:query.limit] if query.limit else ordered

  def get(self, slug: str) -> Optional[PieceRecord]:
    record = self._records.get(slug)
    return self._with_map(record) if record else None

  def upsert(self, record: PieceRecord) -> PieceRecord:
    current = self._records.get(record.slug)
    stored = dataclasses.replace(record)
    if current is not None:
      # derived facts belong to their producer, not to the catalogue
      stored.bars, stored.key = current.bars, current.key
      stored.score, stored.pdf = current.score, current.pdf
      stored.audio, stored.reference_midi = current.audio, current.reference_midi
      stored.audio_duration_s = current.audio_duration_s
      stored.map_method, stored.map_measures = current.map_method, current.map_measures
    stored.updated_at = _now()
    self._records[record.slug] = stored
    return stored

  def remove(self, slug: str) -> bool:
    record = self._records.get(slug)
    if record is None:
      return False
    record.hidden = True
    return True


class _MemoryMaps(MapStore):

  def __init__(self):
    self._maps: Dict[str, Any] = {}

  def get(self, slug: str):
    return self._maps.get(slug)

  def put(self, slug: str, result: SyncResult, *, audio: Optional[AudioSource] = None,
          source_score: str = '', played: Optional[Sequence[int]] = None) -> None:
    from . import syncmap
    stored = syncmap.StoredSync(slug=slug, result=result,
                                audio=audio.to_dict() if audio else {},
                                source_musicxml=source_score,
                                played=list(played or []), updated_at=_now())
    self._maps[slug] = stored

  def delete(self, slug: str) -> bool:
    return self._maps.pop(slug, None) is not None


class _MemoryRenders(RenderStore):

  def __init__(self):
    self._renders: Dict[str, Any] = {}

  def get(self, slug: str):
    return self._renders.get(slug)

  def put(self, slug: str, render, images: Optional[Mapping[int, bytes]] = None) -> None:
    self._renders[slug] = render

  def delete(self, slug: str) -> bool:
    return self._renders.pop(slug, None) is not None


def memory_repository(scratch=None) -> Repository:
  """An in-memory library, for tests and for proving the ports are not paths.

  `scratch` is where `BlobStore.local_path` materialises bytes, since even a
  memory-backed library has to hand a filename to soundfile, Qt and Verovio.
  """
  import tempfile
  where = pathlib.Path(scratch) if scratch else pathlib.Path(tempfile.mkdtemp(prefix='analyzer-blobs-'))
  maps = _MemoryMaps()
  return Repository(pieces=_MemoryPieces(maps), blobs=_MemoryBlobs(where),
                    maps=maps, renders=_MemoryRenders(), backend='memory')
