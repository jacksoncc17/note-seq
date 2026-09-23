"""Tests for finding pieces, their scores, their recordings and their maps."""

import json
import pathlib

import pytest

from analyzer.config import AnalyzerConfig
from analyzer import library


@pytest.fixture
def cfg(tmp_path):
  config = AnalyzerConfig(cache_dir=tmp_path / 'cache', output_dir=tmp_path / 'output',
                          bank_dir=tmp_path / 'banks',
                          legacy_bank=tmp_path / 'banks' / 'fingerprint.json',
                          sheet_music_dir=tmp_path / 'sheets')
  (tmp_path / 'sheets').mkdir()
  config.ensure_dirs()
  return config


def write_bank(cfg, slug, *, source_file='', bars=40, key='A minor',
               title='A Piece', measures=40):
  d = cfg.bank_dir / slug
  d.mkdir(parents=True)
  (d / 'fingerprint.json').write_text(json.dumps({
      'source_file': source_file,
      'global': {'num_measures': bars, 'key_from_score': key, 'title': title},
      'measures': [{}] * measures,
      'questions': [],
  }), encoding='utf-8')
  return d / 'fingerprint.json'


def test_slugify_matches_the_quiz_tools_convention():
  assert library.slugify('/home/wei/sheet_music/Intermezzo_left.mxl') == 'intermezzo-left'
  assert library.slugify('Waltz Op. 34 No. 2.MusicXML') == 'waltz-op-34-no-2'
  assert library.slugify('') == 'piece'


def test_discovers_a_banked_piece_with_its_score(cfg, tmp_path):
  score = tmp_path / 'sheets' / 'demo-piece.mxl'
  score.write_bytes(b'not really musicxml')
  write_bank(cfg, 'demo-piece', source_file=str(score), bars=42, key='B minor',
             title='Demo Piece')

  pieces = library.discover(cfg)
  assert [p.slug for p in pieces] == ['demo-piece']
  piece = pieces[0]
  assert piece.bars == 42 and piece.key == 'B minor'
  assert piece.title == 'Demo Piece'
  assert piece.musicxml == score
  assert piece.has_score and not piece.has_audio and not piece.has_map
  assert 'no audio' in piece.describe()


def test_finds_the_pdf_next_to_the_score(cfg, tmp_path):
  score = tmp_path / 'sheets' / 'demo-piece.mxl'
  score.write_bytes(b'x')
  (tmp_path / 'sheets' / 'demo-piece.pdf').write_bytes(b'%PDF-1.4')
  write_bank(cfg, 'demo-piece', source_file=str(score))
  piece = library.find(cfg, 'demo-piece')
  assert piece.pdf is not None and piece.pdf.name == 'demo-piece.pdf'


def test_notes_when_the_score_has_gone_missing(cfg, tmp_path):
  write_bank(cfg, 'orphan', source_file=str(tmp_path / 'sheets' / 'gone.mxl'))
  piece = library.find(cfg, 'orphan')
  assert piece is not None
  assert piece.has_score is False
  assert any('gone' in n for n in piece.notes)


def test_legacy_single_bank_is_listed_once(cfg, tmp_path):
  score = tmp_path / 'sheets' / 'legacy-piece.mxl'
  score.write_bytes(b'x')
  cfg.legacy_bank.parent.mkdir(parents=True, exist_ok=True)
  cfg.legacy_bank.write_text(json.dumps({
      'source_file': str(score),
      'global': {'num_measures': 7, 'key_inferred': 'C major', 'title': 'Legacy'},
      'measures': [{}] * 7}), encoding='utf-8')

  slugs = [p.slug for p in library.discover(cfg)]
  assert slugs == ['legacy-piece']
  piece = library.find(cfg, 'legacy-piece')
  assert piece.bars == 7 and piece.key == 'C major'


def test_piece_from_a_bare_score_needs_no_bank(cfg, tmp_path):
  """Opening a score with no question bank still gives a usable piece, and
  registers it, so it stays in the library (and on the phone) afterwards."""
  score = tmp_path / 'sheets' / 'unbanked.mxl'
  score.write_bytes(b'a score Verovio would reject, but the catalogue only stores it')
  piece = library.piece_from_musicxml(cfg, score)
  assert piece.slug == 'unbanked'
  assert piece.has_score and piece.title == ''      # display falls back to the slug
  assert piece.display_name == 'Unbanked'
  assert piece.record is not None, 'opening a score should catalogue it'
  assert [p.slug for p in library.discover(cfg)] == ['unbanked']
  assert library.find(cfg, 'unbanked').musicxml.read_bytes() == score.read_bytes()


def test_the_catalogue_outlives_a_score_that_is_moved_away(cfg, tmp_path):
  """The filesystem adapter references the score where it lives, so moving it away
  loses the score but not the piece: the entry stays, with an explanation attached.
  (A database adapter would have imported the bytes and kept both.)"""
  score = tmp_path / 'sheets' / 'movable.mxl'
  score.write_bytes(b'score bytes')
  library.piece_from_musicxml(cfg, score, register=True)
  assert library.find(cfg, 'movable').has_score is True

  score.unlink()
  piece = library.find(cfg, 'movable')
  assert piece is not None, 'a piece must not vanish because its score moved'
  assert piece.has_score is False
  assert any('gone' in note for note in piece.notes), piece.notes
  assert [p.slug for p in library.discover(cfg)] == ['movable'], 'still listed'


def test_piece_from_a_missing_score_says_so(cfg, tmp_path):
  piece = library.piece_from_musicxml(cfg, tmp_path / 'sheets' / 'nope.mxl')
  assert piece.has_score is False
  assert any('not found' in n for n in piece.notes)


def test_unreadable_bank_is_reported_not_fatal(cfg):
  d = cfg.bank_dir / 'bad'
  d.mkdir(parents=True)
  (d / 'fingerprint.json').write_text('{{{ broken', encoding='utf-8')
  piece = library.find(cfg, 'bad')
  assert piece is not None and piece.bars == 0
  assert any('could not be read' in n for n in piece.notes)


def test_ordering_prefers_pieces_with_audio(cfg, tmp_path, monkeypatch):
  pytest.importorskip('analyzer.ytaudio', reason='the downloader module is optional')
  import numpy as np
  import soundfile as sf

  score = tmp_path / 'sheets' / 'a-piece.mxl'
  score.write_bytes(b'x')
  write_bank(cfg, 'a-piece', source_file=str(score))
  write_bank(cfg, 'b-piece', source_file=str(score))

  from analyzer.schema import AudioSource
  wav = cfg.audio_path('b-piece')
  sf.write(str(wav), np.zeros(22050, dtype='float32'), 22050)      # a real, readable WAV
  cfg.audio_meta_path('b-piece').write_text(json.dumps(
      AudioSource(slug='b-piece', path=str(wav), duration_s=1.0,
                  sample_rate=22050, channels=1).to_dict()),
      encoding='utf-8')

  order = [p.slug for p in library.discover(cfg)]
  assert order[0] == 'b-piece', 'the piece that already has a recording comes first'


def test_real_pieces_on_this_machine(tmp_path):
  """The repos own banks resolve to their real scores, when present."""
  real = AnalyzerConfig()
  if not real.bank_dir.is_dir():
    pytest.skip('no question banks on this machine')
  pieces = library.discover(real)
  assert pieces, 'expected at least one piece'
  with_score = [p for p in pieces if p.has_score]
  if not with_score:
    pytest.skip('no bank records a score that still exists')
  for piece in with_score:
    assert piece.musicxml.suffix.lower() in ('.mxl', '.xml', '.musicxml')
    assert piece.bars > 0
