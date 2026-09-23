"""The piece as the app thinks of it: paths and a map, assembled from the stores.

This module is the *domain* layer, and it is deliberately thin. It knows nothing
about where data lives -- it asks the repository for a `PieceRecord` and turns it
into the thing the window and the HTTP server actually want: a score to engrave, a
recording to play, a map to follow, and whatever was worth warning about.

Keeping it thin is the whole point. Every caller already goes through `Piece`, so
moving the storage into a database means a new `store` adapter and no changes
here or above `here`.
"""

from __future__ import annotations

import pathlib
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .config import AnalyzerConfig
from .schema import AudioSource
from . import store
from .store import BlobRef, PieceQuery, PieceRecord, Repository


@dataclass
class Piece:
  """One piece, resolved for use: real paths, a real map, real warnings."""

  slug: str
  title: str = ''
  composer: str = ''
  bars: int = 0
  key: str = ''
  tags: List[str] = field(default_factory=list)
  notes: List[str] = field(default_factory=list)

  musicxml: Optional[pathlib.Path] = None
  pdf: Optional[pathlib.Path] = None
  reference_midi: Optional[pathlib.Path] = None
  audio: Optional[AudioSource] = None
  sync: Optional[Any] = None                    # syncmap.StoredSync

  record: Optional[PieceRecord] = None

  @property
  def display_name(self) -> str:
    """A human label: the title if there is one, else the slug prettied up."""
    if self.title:
      return self.title
    return self.slug.replace('-', ' ').strip().capitalize()

  @property
  def has_audio(self) -> bool:
    return bool(self.audio and self.audio.path and pathlib.Path(self.audio.path).is_file())

  @property
  def has_map(self) -> bool:
    return self.sync is not None

  @property
  def has_score(self) -> bool:
    return bool(self.musicxml and self.musicxml.is_file())

  @property
  def can_align(self) -> bool:
    """Alignment needs both a score and a reference performance of it."""
    return self.has_score and self.reference_midi is not None

  def describe(self) -> str:
    bits = ['%d bars' % self.bars if self.bars else 'bars unknown']
    if self.key:
      bits.append(self.key)
    bits.append('audio' if self.has_audio else 'no audio')
    if self.sync:
      bits.append(getattr(self.sync, 'method', 'saved') + ' map')
    return '%s (%s)' % (self.display_name, ', '.join(bits))

  def summary(self) -> Dict[str, Any]:
    """The handful of fields a piece list needs, for the HTTP API."""
    out: Dict[str, Any] = {
        'slug': self.slug, 'name': self.display_name, 'title': self.title,
        'composer': self.composer, 'bars': self.bars, 'key': self.key,
        'tags': list(self.tags),
        'has_audio': self.has_audio, 'has_map': self.has_map, 'has_score': self.has_score,
    }
    if self.audio is not None:
      out['duration_s'] = round(float(self.audio.duration_s or 0.0), 2)
    if self.sync is not None:
      out['method'] = getattr(self.sync, 'method', '')
      out['trusted'] = bool(getattr(self.sync, 'is_trusted', False))
      out['measures'] = len(self.sync.result.measures)
    return out


def slugify(source) -> str:
  """A filesystem-friendly name from a score filename.

  The rules live in `store` now, because the catalogue itself needs them: a slug
  the store cannot reproduce is a slug that will not be found again.
  """
  return store.slugify(source)


# ---------------------------------------------------------------------------
# turning records into pieces
# ---------------------------------------------------------------------------

def _path(repo: Repository, ref: Optional[BlobRef]) -> Optional[pathlib.Path]:
  """A local path for a blob, or None if the bytes are not there.

  A missing blob is a normal state here -- the user deleted their sheet music, or
  a background fetch has not finished -- so it becomes a note rather than an error.
  """
  if ref is None:
    return None
  try:
    return repo.blobs.local_path(ref)
  except KeyError:
    return None


def _audio(repo: Repository, record: PieceRecord) -> Optional[AudioSource]:
  path = _path(repo, record.audio)
  if path is None:
    return None
  return AudioSource(slug=record.slug, path=str(path), url=record.audio_url,
                     source_id=record.audio_source_id,
                     duration_s=record.audio_duration_s or _probe_duration(path),
                     title=record.title)


def _probe_duration(path: pathlib.Path) -> float:
  try:
    import soundfile as sf
    info = sf.info(str(path))
    return float(info.frames) / float(info.samplerate)
  except Exception:
    return 0.0


def piece_from_record(repo: Repository, record: PieceRecord) -> Piece:
  """Assemble the app's view of one catalogued piece."""
  piece = Piece(slug=record.slug, title=record.title, composer=record.composer,
                bars=record.bars, key=record.key, tags=list(record.tags),
                notes=list(record.notes), record=record)
  piece.musicxml = _path(repo, record.score)
  piece.pdf = _path(repo, record.pdf)
  piece.reference_midi = _path(repo, record.reference_midi)
  if record.score is not None and piece.musicxml is None:
    piece.notes.append('the score this bank was built from is gone: %s' % record.score.key)
  piece.audio = _audio(repo, record)
  piece.sync = repo.maps.get(record.slug)
  return piece


def discover(cfg: AnalyzerConfig, repo: Optional[Repository] = None,
             query: Optional[PieceQuery] = None) -> List[Piece]:
  """Every piece in the library, in the order the query asks for.

  Args:
    cfg: paths, used only to open a repository when none is given.
    repo: the storage to read; opened from `cfg` when omitted.
    query: filter and ordering. With no query, pieces that have a recording come
      first, because those are the ones you can actually practise.
  """
  repo = repo or store.open_repository(cfg)
  return [piece_from_record(repo, record) for record in repo.pieces.list(query)]


def find(cfg: AnalyzerConfig, slug: str,
         repo: Optional[Repository] = None) -> Optional[Piece]:
  """One piece by slug, or None."""
  repo = repo or store.open_repository(cfg)
  record = repo.pieces.get(slug)
  return piece_from_record(repo, record) if record else None


def find_many(cfg: AnalyzerConfig, slugs: List[str],
              repo: Optional[Repository] = None) -> List[Piece]:
  """Several pieces by slug, skipping the ones that are gone."""
  repo = repo or store.open_repository(cfg)
  out = []
  for slug in slugs:
    record = repo.pieces.get(slug)
    if record is not None:
      out.append(piece_from_record(repo, record))
  return out


def piece_from_musicxml(cfg: AnalyzerConfig, path, title: str = '',
                        repo: Optional[Repository] = None,
                        register: bool = True) -> Piece:
  """A piece around a score file, with or without a question bank.

  `register` is what "add to the library" means: the score is recorded in the
  catalogue, so a piece that was never analysed still appears in the lists (here
  and on the phone) until the user removes it.
  """
  repo = repo or store.open_repository(cfg)
  score = pathlib.Path(path).expanduser()
  slug = slugify(score)

  existing = repo.pieces.get(slug)
  if existing is not None and existing.has_score:
    piece = piece_from_record(repo, existing)
    if title and piece.title != title:
      existing.title = title
      repo.pieces.upsert(existing)
      piece.title = title
    if not piece.has_score:
      piece.notes.append('score not found at %s' % score)
    return piece

  record = PieceRecord(slug=slug, title=title)
  if score.is_file():
    record.score = repo.blobs.write(str(score), score.read_bytes(),
                                    store.media_type_of(score))
    pdf = score.with_suffix('.pdf')
    if pdf.is_file():
      record.pdf = repo.blobs.write(str(pdf), pdf.read_bytes(), 'application/pdf')
  else:
    record.notes.append('score not found at %s' % score)

  if register:
    record = repo.pieces.upsert(record)
  piece = piece_from_record(repo, record)
  if not piece.has_score:
    piece.notes.append('score not found at %s' % score)
  return piece


def remove(cfg: AnalyzerConfig, slug: str, repo: Optional[Repository] = None) -> bool:
  """Take a piece out of the library, with the artifacts this app made for it."""
  repo = repo or store.open_repository(cfg)
  return repo.pieces.remove(slug)
