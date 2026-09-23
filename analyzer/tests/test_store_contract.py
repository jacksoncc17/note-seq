"""The contract every storage adapter must satisfy, run against each of them.

This is the file that makes moving to a database cheap. A new adapter -- SQLite,
Postgres, a remote service -- is accepted by adding one line to `ADAPTERS` below;
if it passes here, every caller keeps working, because the callers only know these
behaviours.

The two adapters that ship today are deliberately different in kind: one stores
files in today's layout, the other keeps everything in memory and has to
materialise bytes to answer `local_path`. Anything that only works for the first
would fail here.
"""

import json
import pathlib

import pytest

from analyzer.config import AnalyzerConfig
from analyzer.schema import AudioSource, MeasureTime, SyncResult
from analyzer.scoreview import MeasureBox, ScorePage, ScoreRender
from analyzer import store, syncmap


# ---------------------------------------------------------------------------
# the piece each adapter holds
# ---------------------------------------------------------------------------

SCORE_TEXT = ('<?xml version="1.0"?><score-partwise version="4.0"><part-list>'
              '<score-part id="P1"><part-name>P</part-name></score-part></part-list>'
              '<part id="P1"><measure number="1"><attributes><divisions>1</divisions>'
              '<time><beats>4</beats><beat-type>4</beat-type></time></attributes>'
              '<note><pitch><step>C</step><octave>4</octave></pitch><duration>4</duration>'
              '<type>whole</type></note></measure></part></score-partwise>')


def write_tone(path, seconds=1.0) -> pathlib.Path:
    """A real WAV: `local_path` is only meaningful if the bytes are playable."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(tone_bytes(seconds))
    return path


def tone_bytes(seconds=1.0) -> bytes:
    import io
    import numpy as np
    import soundfile as sf
    t = np.arange(int(seconds * 22050)) / 22050
    buffer = io.BytesIO()
    sf.write(buffer, (0.3 * np.sin(2 * np.pi * 330 * t)).astype('float32'), 22050,
             format='WAV', subtype='PCM_16')
    return buffer.getvalue()


def fake_render(tmp_path, slug='demo', bars=4):
  """A render whose page images really exist, since one adapter checks for them."""
  from PIL import Image
  page_path = tmp_path / ('%s-page-1.png' % slug)
  Image.new('RGB', (200, 100), 'white').save(page_path)
  boxes = [MeasureBox(number=n, x0=10.0 * n, y0=10.0, x1=10.0 * n + 8, y1=40.0)
           for n in range(1, bars + 1)]
  return ScoreRender(slug=slug, source='demo.musicxml',
                     pages=[ScorePage(number=1, image=str(page_path), width=200,
                                      height=100, measures=boxes)])


def example_map(slug='demo', bars=4):
  measures = [MeasureTime(measure=n, play_index=n - 1, start_s=(n - 1) * 2.0,
                          end_s=n * 2.0, confidence=0.8) for n in range(1, bars + 1)]
  result = SyncResult(method=SyncResult.METHOD_DTW, measures=measures,
                      seconds_per_measure=2.0, audio_duration_s=bars * 2.0)
  result.quality['normalized_cost'] = 0.45
  return result


class FilesystemFixture:
  """A library on disk, in the layout the app already uses."""

  backend = 'filesystem'

  def __init__(self, tmp_path):
    self.tmp = tmp_path
    self.cfg = AnalyzerConfig(
        cache_dir=tmp_path / 'cache', output_dir=tmp_path / 'out',
        bank_dir=tmp_path / 'banks', legacy_bank=tmp_path / 'banks' / 'fingerprint.json',
        sheet_music_dir=tmp_path / 'sheets')
    (tmp_path / 'sheets').mkdir(parents=True, exist_ok=True)
    self.cfg.ensure_dirs()

    self.score = self.cfg.sheet_music_dir / 'demo.musicxml'
    self.score.write_text(SCORE_TEXT, encoding='utf-8')
    bank = self.cfg.bank_dir / 'demo'
    bank.mkdir(parents=True, exist_ok=True)
    (bank / 'fingerprint.json').write_text(json.dumps({
        'source_file': str(self.score),
        'global': {'num_measures': 4, 'key_from_score': 'C major', 'title': 'Bank Title'},
        'measures': [{}] * 4}), encoding='utf-8')

    self.wav = self.cfg.audio_path('demo')
    write_tone(self.wav)
    self.cfg.audio_meta_path('demo').write_text(json.dumps(AudioSource(
        slug='demo', path=str(self.wav), url='https://example.invalid/x',
        source_id='xyz', duration_s=8.0, sample_rate=22050, channels=1).to_dict()),
        encoding='utf-8')

    # a second piece, so listing and filtering have something to discriminate
    other = self.cfg.bank_dir / 'other'
    other.mkdir(parents=True, exist_ok=True)
    other_score = self.cfg.sheet_music_dir / 'other.musicxml'
    other_score.write_text(SCORE_TEXT, encoding='utf-8')
    (other / 'fingerprint.json').write_text(json.dumps({
        'source_file': str(other_score),
        'global': {'num_measures': 8, 'key_from_score': 'G major', 'title': 'Other Piece'},
        'measures': [{}] * 8}), encoding='utf-8')

  def open(self):
    return store.filesystem_repository(self.cfg)

  def score_bytes(self):
    return self.score.read_bytes()

  def render(self):
    return fake_render(self.tmp)

  def audio_blob(self, repo):
    record = repo.pieces.get('demo')
    return record.audio

  def cleanup(self):
    pass


class MemoryFixture:
  """The same library, held entirely in memory."""

  backend = 'memory'

  def __init__(self, tmp_path):
    self.tmp = tmp_path
    self.repo = store.memory_repository(scratch=tmp_path / 'blobs')
    score = self.repo.blobs.write('scores/demo.musicxml', SCORE_TEXT.encode('utf-8'),
                                  'application/vnd.recordare.musicxml+xml')
    audio = self.repo.blobs.write('audio/demo.wav', tone_bytes(), 'audio/wav')
    self.repo.pieces.upsert(store.PieceRecord(
        slug='demo', title='Bank Title', bars=4, key='C major', score=score, audio=audio,
        audio_duration_s=8.0, audio_url='https://example.invalid/x', audio_source_id='xyz'))
    other_score = self.repo.blobs.write('scores/other.musicxml', SCORE_TEXT.encode('utf-8'),
                                        'application/vnd.recordare.musicxml+xml')
    self.repo.pieces.upsert(store.PieceRecord(slug='other', title='Other Piece', bars=8,
                                              key='G major', score=other_score))

  def open(self):
    return self.repo

  def score_bytes(self):
    return SCORE_TEXT.encode('utf-8')

  def render(self):
    return fake_render(self.tmp, slug='demo')

  def audio_blob(self, repo):
    return repo.pieces.get('demo').audio

  def cleanup(self):
    self.repo.close()


ADAPTERS = {
    'filesystem': FilesystemFixture,
    'memory': MemoryFixture,
}


@pytest.fixture(params=sorted(ADAPTERS))
def library(request, tmp_path):
  """A library in each backend, so every test below runs against both."""
  fixture = ADAPTERS[request.param](tmp_path)
  yield fixture
  fixture.cleanup()


# ---------------------------------------------------------------------------
# the catalogue
# ---------------------------------------------------------------------------

def test_a_piece_can_be_found_by_slug(library):
  repo = library.open()
  record = repo.pieces.get('demo')
  assert record is not None
  assert record.slug == 'demo'
  assert record.bars == 4 and record.key == 'C major'
  assert record.has_score and record.has_audio
  assert repo.pieces.get('nope') is None


def test_listing_returns_every_piece(library):
  repo = library.open()
  slugs = sorted(r.slug for r in repo.pieces.list())
  assert slugs == ['demo', 'other']
  assert repo.pieces.count() == 2


def test_text_search_matches_title_and_slug(library):
  repo = library.open()
  assert [r.slug for r in repo.pieces.list(store.PieceQuery(text='other'))] == ['other']
  assert [r.slug for r in repo.pieces.list(store.PieceQuery(text='BANK TITLE'))] == ['demo']
  assert repo.pieces.list(store.PieceQuery(text='nothing like this')) == []


def test_filters_and_limits(library):
  repo = library.open()
  with_audio = repo.pieces.list(store.PieceQuery(has_audio=True))
  assert [r.slug for r in with_audio] == ['demo']
  assert [r.slug for r in repo.pieces.list(store.PieceQuery(has_audio=False))] == ['other']
  assert len(repo.pieces.list(store.PieceQuery(limit=1))) == 1
  # pieces you can practise sort first
  assert repo.pieces.list()[0].slug == 'demo'


def test_ordering_options(library):
  repo = library.open()
  assert [r.slug for r in repo.pieces.list(store.PieceQuery(order_by='title'))] == \
      ['demo', 'other']          # "Bank Title" before "Other Piece"
  assert [r.slug for r in repo.pieces.list(store.PieceQuery(order_by='slug'))] == \
      ['demo', 'other']


def test_upsert_persists_curated_fields(library):
  repo = library.open()
  record = repo.pieces.get('demo')
  record.title = 'Waltz in A minor'
  record.composer = 'Chopin'
  record.tags = ['waltz', 'exam']
  repo.pieces.upsert(record)

  again = repo.pieces.get('demo')
  assert again.title == 'Waltz in A minor'
  assert again.composer == 'Chopin'
  assert again.tags == ['waltz', 'exam']
  assert 'Chopin' in [r.composer for r in repo.pieces.list()]
  assert 'exam' in again.tags
  assert [r.slug for r in repo.pieces.list(store.PieceQuery(tag='exam'))] == ['demo']


def test_upsert_leaves_derived_facts_alone(library):
  """Bar count and key come from the score, not from whoever saved a title."""
  repo = library.open()
  impostor = store.PieceRecord(slug='demo', title='Renamed', bars=999, key='Z major')
  repo.pieces.upsert(impostor)
  record = repo.pieces.get('demo')
  assert record.title == 'Renamed'
  assert record.bars == 4, 'the bar count belongs to the score'
  assert record.key == 'C major'


def test_removing_a_piece_takes_it_out_of_the_lists(library):
  repo = library.open()
  assert repo.pieces.remove('demo') is True
  assert [r.slug for r in repo.pieces.list()] == ['other']
  hidden = repo.pieces.get('demo')
  assert hidden is not None and hidden.hidden is True
  assert repo.pieces.list(store.PieceQuery(include_hidden=True)) != []
  assert repo.pieces.remove('demo') is True          # idempotent
  assert repo.pieces.remove('never-existed') is False


# ---------------------------------------------------------------------------
# blobs
# ---------------------------------------------------------------------------

def test_blobs_round_trip(library):
  repo = library.open()
  ref = repo.blobs.write('notes/hello.txt', b'hello', 'text/plain')
  assert ref.key == 'notes/hello.txt' and ref.size == 5
  assert repo.blobs.exists(ref) and repo.blobs.size(ref) == 5
  assert repo.blobs.read(ref) == b'hello'
  assert repo.blobs.delete(ref) is True
  assert repo.blobs.exists(ref) is False


def test_reading_a_missing_blob_is_a_clear_error(library):
  repo = library.open()
  ghost = store.BlobRef(key='nothing/here.bin', media_type='application/octet-stream')
  with pytest.raises(KeyError):
    repo.blobs.read(ghost)


def test_asking_for_a_missing_blob_raises_keyerror_in_every_backend(library):
  """Callers catch KeyError; a filesystem backend must not leak FileNotFoundError."""
  repo = library.open()
  ghost = store.BlobRef(key='nothing/here.bin', media_type='application/octet-stream')
  with pytest.raises(KeyError):
    repo.blobs.read(ghost)
  with pytest.raises(KeyError):
    repo.blobs.local_path(ghost)
  assert repo.blobs.exists(ghost) is False
  assert repo.blobs.size(ghost) == 0
  assert repo.blobs.delete(ghost) is False


def test_local_path_hands_out_a_readable_file(library):
  """The point of the port: a non-file backend still answers with a real path,
  because soundfile, Qt and Verovio insist on one."""
  repo = library.open()
  blob = library.audio_blob(repo)
  assert blob is not None
  path = repo.blobs.local_path(blob)
  assert path.is_file()
  assert path.read_bytes() == repo.blobs.read(blob)
  import soundfile as sf
  info = sf.info(str(path))
  assert info.samplerate == 22050 and info.frames > 0, \
      'the path must point at audio a decoder can actually open'


def test_the_score_is_available_as_bytes_and_as_a_path(library):
  repo = library.open()
  record = repo.pieces.get('demo')
  assert repo.blobs.read(record.score) == library.score_bytes()
  assert repo.blobs.local_path(record.score).read_bytes() == library.score_bytes()


# ---------------------------------------------------------------------------
# maps
# ---------------------------------------------------------------------------

def test_maps_round_trip(library):
  repo = library.open()
  assert repo.maps.get('other') is None
  repo.maps.put('other', example_map('other'), source_score='other.musicxml',
                played=[1, 2, 3, 4])
  stored = repo.maps.get('other')
  assert stored is not None
  assert stored.method == SyncResult.METHOD_DTW
  assert [m.measure for m in stored.result.measures] == [1, 2, 3, 4]
  assert stored.result.quality['normalized_cost'] == 0.45
  assert stored.played == [1, 2, 3, 4]
  assert repo.maps.delete('other') is True
  assert repo.maps.get('other') is None


def test_a_map_makes_a_piece_report_that_it_has_one(library):
  """Both backends derive this from the map store, so `has_map` cannot drift."""
  repo = library.open()
  before = repo.pieces.get('other')
  assert before.has_map is False and before.map_method == ''
  repo.maps.put('other', example_map('other'))
  after = repo.pieces.get('other')
  assert after.has_map is True and after.map_measures == 4
  assert [r.slug for r in repo.pieces.list(store.PieceQuery(has_map=True))] == ['other']


def test_a_stored_map_survives_a_fresh_repository(library):
  """What "persisted" has to mean: reopen and it is still there."""
  repo = library.open()
  repo.maps.put('other', example_map('other'))
  reopened = library.open()
  assert reopened.maps.get('other') is not None


# ---------------------------------------------------------------------------
# renders
# ---------------------------------------------------------------------------

def test_renders_are_empty_until_stored(library):
  repo = library.open()
  assert repo.renders.get('demo') is None


def test_a_render_can_be_stored_and_read_back(library):
  repo = library.open()
  render = library.render()
  for page in render.pages:
    pathlib.Path(page.image).write_bytes(pathlib.Path(page.image).read_bytes())
  repo.renders.put('demo', render)
  again = repo.renders.get('demo')
  assert again is not None, 'the render should have been persisted'
  assert len(again.pages) == 1
  assert [b.number for b in again.pages[0].measures] == [1, 2, 3, 4]


def test_renders_can_be_dropped(library):
  repo = library.open()
  repo.renders.put('demo', library.render())
  assert repo.renders.delete('demo') is True
  assert repo.renders.get('demo') is None


# ---------------------------------------------------------------------------
# the factory, which is the one line a database would change
# ---------------------------------------------------------------------------

def test_the_factory_honours_the_configured_backend(tmp_path):
  cfg = AnalyzerConfig(cache_dir=tmp_path / 'c', output_dir=tmp_path / 'o')
  assert store.open_repository(cfg).backend == 'filesystem'
  assert store.open_repository(cfg, backend='memory').backend == 'memory'
  with pytest.raises(ValueError) as caught:
    store.open_repository(cfg, backend='postgres')
  assert 'postgres' in str(caught.value)


def test_an_unknown_backend_name_is_rejected_before_anything_else_happens(tmp_path):
  cfg = AnalyzerConfig(cache_dir=tmp_path / 'c', output_dir=tmp_path / 'o')
  cfg.backend = 'sqlite'                     # a name kept for the future
  with pytest.raises(ValueError):
    store.open_repository(cfg)


# ---------------------------------------------------------------------------
# the pure pieces both adapters share
# ---------------------------------------------------------------------------

def test_query_matching_is_shared_not_reimplemented():
  record = store.PieceRecord(slug='chopin-waltz', title='Waltz', composer='Chopin',
                             tags=['exam'], bars=204, key='A minor')
  assert store.matches(record, store.PieceQuery()) is True
  assert store.matches(record, store.PieceQuery(text='chop')) is True
  assert store.matches(record, store.PieceQuery(text='bach')) is False
  assert store.matches(record, store.PieceQuery(tag='exam')) is True
  assert store.matches(record, store.PieceQuery(tag='grade5')) is False
  assert store.matches(record, store.PieceQuery(has_audio=True)) is False

  record.audio = store.BlobRef(key='a.wav', media_type='audio/wav')
  assert store.matches(record, store.PieceQuery(has_audio=True)) is True
  record.hidden = True
  assert store.matches(record, store.PieceQuery()) is False
  assert store.matches(record, store.PieceQuery(include_hidden=True)) is True


def test_record_serialisation_round_trips():
  record = store.PieceRecord(slug='demo', title='T', composer='C', bars=4, key='C major',
                             tags=['a'], notes=['n'],
                             score=store.BlobRef(key='s.musicxml', media_type='application/xml',
                                                 size=10))
  again = store.PieceRecord.from_dict(json.loads(json.dumps(record.to_dict())))
  assert again.slug == record.slug and again.tags == ['a'] and again.notes == ['n']
  assert again.score == record.score


def test_a_blob_ref_reports_a_sensible_suffix():
  assert store.BlobRef(key='/x/y/score.musicxml',
                       media_type='application/vnd.recordare.musicxml+xml').suffix == '.musicxml'
  assert store.BlobRef(key='abc', media_type='audio/wav').suffix == '.wav'
  assert store.BlobRef(key='no-extension', media_type='application/octet-stream').suffix == ''
