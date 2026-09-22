"""Finding the pieces this app can open.

A *piece* is the same thing it is to the quiz and tutor tools: a question bank
under `memorization/output/pieces/<slug>/`, built from a score. The analyzer adds
two things to that idea -- the recording (cached locally) and the measure map --
and needs to find the score the bank was built from, which the bank records in
its own `source_file` field.

A score with no bank is still openable: `piece_for_musicxml` makes a piece out of
a bare MusicXML path, because mapping measures onto a recording does not need the
question bank at all.
"""

from __future__ import annotations

import json
import pathlib
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .config import AnalyzerConfig
from . import syncmap
from .schema import AudioSource


@dataclass
class Piece:
  """Everything the app knows about one piece, by reference -- not loaded."""

  slug: str
  title: str = ''
  bars: int = 0
  key: str = ''
  musicxml: Optional[pathlib.Path] = None
  bank: Optional[pathlib.Path] = None
  pdf: Optional[pathlib.Path] = None
  audio: Optional[AudioSource] = None
  sync: Optional[syncmap.StoredSync] = None
  notes: List[str] = field(default_factory=list)

  @property
  def display_name(self) -> str:
    """A human label: the title if the bank has one, else the slug prettied up."""
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
  def reference_midi(self) -> Optional[pathlib.Path]:
    """The steady-tempo render of the score, made by `memorization/analyze.py`.

    Alignment needs a machine-readable performance of the score, and the question
    bank already contains one as a by-product. Prefer the steady render: the
    expressive one carries the score's own tempo changes, which would be
    modelled twice.
    """
    if not self.bank:
      return None
    for name in ('fingerprint_steady.mid', 'fingerprint.mid'):
      candidate = self.bank.parent / name
      if candidate.is_file():
        return candidate
    return None

  @property
  def can_align(self) -> bool:
    return self.has_score and self.reference_midi is not None

  def describe(self) -> str:
    bits = ['%d bars' % self.bars if self.bars else 'bars unknown']
    if self.key:
      bits.append(self.key)
    bits.append('audio' if self.has_audio else 'no audio')
    if self.sync:
      bits.append(self.sync.method + ' map')
    return '%s (%s)' % (self.display_name, ', '.join(bits))


def slugify(source) -> str:
  """A filesystem-friendly name from a score filename.

  Deliberately identical to `memorization.pieces.slugify`, so the same score
  resolves to the same slug in the analyzer, the quiz and the tutor.
  """
  base = pathlib.Path(str(source or '')).name
  base = re.sub(r'\.(musicxml|xml|mxl|mid|midi)$', '', base, flags=re.I)
  slug = re.sub(r'[^a-z0-9]+', '-', base.lower()).strip('-')
  return slug or 'piece'


def _read_bank(path: pathlib.Path) -> Dict[str, Any]:
  try:
    data = json.loads(path.read_text(encoding='utf-8'))
  except (OSError, ValueError):
    return {}
  return data if isinstance(data, dict) else {}


def _pdf_beside(score: Optional[pathlib.Path]) -> Optional[pathlib.Path]:
  """The printed edition, if it sits next to the score with the same stem."""
  if not score:
    return None
  candidate = score.with_suffix('.pdf')
  return candidate if candidate.is_file() else None


def _cached_audio(cfg: AnalyzerConfig, slug: str) -> Optional[AudioSource]:
  """Cached recording for a piece, if the downloader module is available."""
  try:
    from . import ytaudio
  except ImportError:
    return None
  try:
    return ytaudio.load_cached(cfg, slug)
  except Exception:
    return None


def piece_from_musicxml(cfg: AnalyzerConfig, path, title: str = '') -> Piece:
  """Build a `Piece` around a bare score file (no bank required)."""
  score = pathlib.Path(path).expanduser()
  slug = slugify(score)
  piece = Piece(slug=slug, title=title, musicxml=score if score.is_file() else None,
                pdf=_pdf_beside(score))
  bank = cfg.bank_dir / slug / 'fingerprint.json'
  if bank.is_file():
    _apply_bank(piece, bank)
  if not piece.has_score:
    piece.notes.append('score not found at %s' % score)
  piece.audio = _cached_audio(cfg, slug)
  piece.sync = syncmap.load(cfg, slug)
  return piece


def _apply_bank(piece: Piece, bank: pathlib.Path) -> None:
  data = _read_bank(bank)
  if not data:
    piece.notes.append('question bank at %s could not be read' % bank)
    return
  piece.bank = bank
  glob = data.get('global') or {}
  piece.bars = int(glob.get('num_measures') or len(data.get('measures') or []))
  piece.key = (glob.get('key_from_score') or glob.get('key_inferred') or '')
  if not piece.title:
    piece.title = glob.get('title') or ''
  source = data.get('source_file')
  if source:
    score = pathlib.Path(source)
    if score.is_file():
      piece.musicxml = score
      piece.pdf = piece.pdf or _pdf_beside(score)
    else:
      piece.notes.append('the score this bank was built from is gone: %s' % source)


def discover(cfg: AnalyzerConfig) -> List[Piece]:
  """Every piece found on disk: banked pieces first, newest bank first.

  A piece is listed when it has a readable bank *or* a score, so a bank whose
  score has been moved away is still visible with a note explaining the problem.
  """
  cfg.ensure_dirs()
  pieces: Dict[str, Piece] = {}

  for bank in sorted(cfg.bank_dir.glob('*/fingerprint.json')):
    piece = Piece(slug=bank.parent.name)
    _apply_bank(piece, bank)
    pieces[piece.slug] = piece

  if cfg.legacy_bank.is_file():
    # The older single-file bank: one piece, its slug taken from its own source.
    data = _read_bank(cfg.legacy_bank)
    source = data.get('source_file') or ''
    slug = slugify(source) if source else 'piece'
    if slug not in pieces:
      piece = Piece(slug=slug, bank=cfg.legacy_bank)
      _apply_bank(piece, cfg.legacy_bank)
      pieces[slug] = piece

  for piece in pieces.values():
    piece.audio = _cached_audio(cfg, piece.slug)
    piece.sync = syncmap.load(cfg, piece.slug)

  def order(piece: Piece):
    # pieces with a recording first, then by bank freshness, then by name
    stamp = 0.0
    if piece.bank and piece.bank.is_file():
      stamp = piece.bank.stat().st_mtime
    return (0 if piece.has_audio else 1, -stamp, piece.slug)

  return sorted(pieces.values(), key=order)


def find(cfg: AnalyzerConfig, slug: str) -> Optional[Piece]:
  """Look up a single piece by slug."""
  for piece in discover(cfg):
    if piece.slug == slug:
      return piece
  return None
