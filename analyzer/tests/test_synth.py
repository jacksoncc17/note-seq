"""Practice tracks: the score whole, and one hand at a time.

The split is the part worth testing hard, because it is the one thing here that can be
silently wrong: a track that plays *mostly* the left hand sounds plausible and would
teach the wrong notes. So the tests check the note counts against the score's own
`<staff>` elements, that the halves add up to the whole, and that the audio is actually
one hand rather than a label on a full render.

Rendering needs FluidSynth and a soundfont; the tests that render skip without them.
"""

import json
import pathlib
import shutil
import xml.etree.ElementTree as ET

import pytest

from analyzer import align, library, synth
from analyzer.config import AnalyzerConfig

SCORE = pathlib.Path('/home/wei/sheet_music/chopin-waltz-in-a-minor-op-34-no-2.mxl')
SLUG = 'chopin-waltz-in-a-minor-op-34-no-2'
needs_real = pytest.mark.skipif(
    not (SCORE.is_file() and AnalyzerConfig().sync_path(SLUG).is_file()),
    reason='the Chopin score and map are needed')
needs_fluidsynth = pytest.mark.skipif(
    shutil.which('fluidsynth') is None or align.find_soundfont() is None,
    reason='rendering needs fluidsynth and a soundfont')


@pytest.fixture
def real(tmp_path):
  cfg = AnalyzerConfig()
  cfg.cache_dir = tmp_path / 'cache'
  cfg.output_dir = tmp_path / 'out'
  cfg.ensure_dirs()
  piece = library.find(cfg, SLUG)
  return cfg, piece


def writes_a_two_staff_score(path: pathlib.Path, bars: int = 2) -> pathlib.Path:
  """A small score whose hands are unambiguous: C4 on top, C3 below."""
  measures = []
  for _ in range(bars):
    measures.append("""
      <measure>
        <attributes><divisions>1</divisions><time><beats>4</beats><beat-type>4</beat-type></time>
          <staves>2</staves></attributes>
        <note><pitch><step>C</step><octave>5</octave></pitch><duration>4</duration>
          <voice>1</voice><type>whole</type><staff>1</staff></note>
        <backup><duration>4</duration></backup>
        <note><pitch><step>C</step><octave>3</octave></pitch><duration>4</duration>
          <voice>2</voice><type>whole</type><staff>2</staff></note>
      </measure>""")
  path.write_text(
      '<?xml version="1.0" encoding="UTF-8"?>\n'
      '<score-partwise version="3.1"><part-list><score-part id="P1">'
      '<part-name>Piano</part-name></score-part></part-list>'
      '<part id="P1">%s</part></score-partwise>' % ''.join(measures), encoding='utf-8')
  return path


# ---------------------------------------------------------------------------
# reading the hands out of the score
# ---------------------------------------------------------------------------

@needs_real
def test_the_score_says_which_staff_every_note_is_on():
  """Nothing can be split if the score does not say -- and this one says it for every
  note, rests included, which is what makes the split exact rather than guessed.

  The counts here are every `<note>` element (2128, of which 216 are rests), because
  that is what the split has to line up with; the 902 and 1010 pitched notes are
  counted from the rendered tracks in the test below.
  """
  staves = synth.staff_per_note(SCORE)
  counts = {}
  for measure in staves:
    for staff in measure:
      counts[staff] = counts.get(staff, 0) + 1
  assert counts == {1: 990, 2: 1138}, counts
  assert all(s is not None for measure in staves for s in measure)
  assert synth.has_hands(staves)


def test_a_single_staff_score_has_no_hands_to_separate(tmp_path):
  score = tmp_path / 'one-staff.musicxml'
  score.write_text(
      '<?xml version="1.0"?><score-partwise version="3.1"><part-list>'
      '<score-part id="P1"><part-name>Flute</part-name></score-part></part-list>'
      '<part id="P1"><measure><note><pitch><step>D</step><octave>5</octave></pitch>'
      '<duration>4</duration><type>whole</type><staff>1</staff></note></measure>'
      '</part></score-partwise>', encoding='utf-8')
  assert not synth.has_hands(synth.staff_per_note(score))


def test_a_score_without_staff_marks_says_so(tmp_path):
  """No `<staff>` anywhere: the split cannot be done by guessing, so it is refused."""
  score = tmp_path / 'no-staff.musicxml'
  score.write_text(
      '<?xml version="1.0"?><score-partwise version="3.1"><part-list>'
      '<score-part id="P1"><part-name>Piano</part-name></score-part></part-list>'
      '<part id="P1"><measure><note><pitch><step>C</step><octave>4</octave></pitch>'
      '<duration>4</duration><type>whole</type></note></measure>'
      '</part></score-partwise>', encoding='utf-8')
  staves = synth.staff_per_note(score)
  assert staves == [[None]], 'nothing is invented where the score is silent'
  assert not synth.has_hands(staves)


def test_a_pitch_split_would_be_wrong_on_real_piano_music():
  """Why this is done by staff: the hands overlap in the middle, so a middle-C rule
  misassigns hundreds of notes on the Chopin."""
  if not SCORE.is_file():
    pytest.skip('the Chopin score is needed')
  import zipfile
  with zipfile.ZipFile(SCORE) as archive:
    xml = archive.read('score.xml').decode('utf-8')
  root = ET.fromstring(xml)
  def local(tag): return tag.split('}')[-1]
  wrong = 0
  for note in root.iter():
    if local(note.tag) != 'note':
      continue
    staff = next((c for c in note if local(c.tag) == 'staff'), None)
    pitch = next((c for c in note if local(c.tag) == 'pitch'), None)
    if staff is None or pitch is None:
      continue
    step = next((c.text for c in pitch if local(c.tag) == 'step'), 'C')
    octave = int(next((c.text for c in pitch if local(c.tag) == 'octave'), '4'))
    alter = int(next((c.text for c in pitch if local(c.tag) == 'alter'), '0') or 0)
    midi = 12 * (octave + 1) + {'C': 0, 'D': 2, 'E': 4, 'F': 5, 'G': 7, 'A': 9, 'B': 11}[step] + alter
    if (midi < 60) != (int(staff.text) == 2):
      wrong += 1
  assert wrong > 200, 'expected the staves to overlap substantially, got %d' % wrong


# ---------------------------------------------------------------------------
# the tracks themselves
# ---------------------------------------------------------------------------

@needs_fluidsynth
@needs_real
def test_the_hands_add_up_to_the_whole_piece(real):
  """The load-bearing property: every note is in exactly one hand."""
  import mido
  cfg, piece = real
  counts = {}
  onsets = {}
  for track in synth.TRACKS:
    wav = synth.build_track(cfg, piece, track)
    midi = wav.with_suffix('.mid')
    file = mido.MidiFile(str(midi))
    notes = [m for m in mido.merge_tracks(file.tracks)
             if m.type == 'note_on' and m.velocity > 0]
    counts[track] = len(notes)
    onsets[track] = min(m.time for m in notes) if notes else None
  assert counts['left'] == 1010 and counts['right'] == 902, counts
  assert counts['both'] == counts['left'] + counts['right'] == 1912
  # and all three start together, so bar 1 is at the same instant in each
  assert all(t == 0 for t in onsets.values()), onsets


@needs_fluidsynth
@needs_real
def test_the_tracks_share_the_pieces_own_practice_tempo(real):
  """The tracks borrow the tempo of the reference MIDI the map was fitted against,
  rather than inventing a second one for the same piece."""
  import mido
  cfg, piece = real
  seconds_per_bar, beats, qpm = align.midi_grid(piece.reference_midi)
  wav = synth.build_track(cfg, piece, 'left')
  sidecar = json.loads(wav.with_suffix('.json').read_text(encoding='utf-8'))
  assert sidecar['qpm'] == pytest.approx(qpm, abs=0.01)
  assert sidecar['seconds_per_bar'] == pytest.approx(seconds_per_bar, abs=1e-4)
  assert sidecar['beats'] == beats
  assert sidecar['notes'] == 1010

  # and the same length as the reference render it borrows the tempo from
  import soundfile as sf
  info = sf.info(str(wav))
  assert info.duration == pytest.approx(mido.MidiFile(str(piece.reference_midi)).length, abs=0.5)


@needs_fluidsynth
@needs_real
def test_only_the_left_hand_sounds_in_the_left_track(real):
  """A track that plays most of the notes would still sound like the piece, so this
  checks the audio itself: the bass is there and the tune is not."""
  import numpy as np
  import soundfile as sf
  cfg, piece = real
  audio = {}
  for track in synth.TRACKS:
    data, rate = sf.read(str(synth.build_track(cfg, piece, track)), dtype='float32')
    audio[track] = (data.mean(axis=1) if data.ndim > 1 else data, rate)

  def low_fraction(mono, rate):
    frame, window = 2048, np.hanning(2048)
    freqs = np.fft.rfftfreq(frame, 1.0 / rate)
    total = low = 0.0
    for start in range(0, len(mono) - frame, 2048):
      power = np.abs(np.fft.rfft(mono[start:start + frame] * window)) ** 2
      total += power.sum()
      low += power[freqs < 261.6].sum()             # below middle C
    return low / max(total, 1e-12)

  low = {t: low_fraction(*audio[t]) for t in audio}
  assert low['left'] > 0.5, 'the left hand is mostly below middle C: %.3f' % low['left']
  assert low['right'] < 0.25, 'the right hand is mostly above it: %.3f' % low['right']
  assert low['left'] > low['both'] > low['right']


@needs_fluidsynth
@needs_real
def test_a_second_call_does_not_render_again(real):
  """A track is 300 seconds of audio; it must not be rebuilt per request."""
  cfg, piece = real
  wav = synth.build_track(cfg, piece, 'right')
  before = wav.stat().st_mtime_ns
  again = synth.build_track(cfg, piece, 'right')
  assert again == wav
  assert again.stat().st_mtime_ns == before, 'the cached track was rewritten'


@needs_real
def test_a_replaced_score_gets_fresh_tracks(real, tmp_path):
  """A track is named after the score it came from, so an edited score is never served
  as a stale render -- the old file is simply left behind."""
  import time
  cfg, piece = real
  copy = tmp_path / piece.musicxml.name            # a copy, so the real score is safe
  copy.write_bytes(piece.musicxml.read_bytes())
  piece.musicxml = copy
  first, _midi = synth.track_paths(cfg, piece, 'both')
  assert synth.track_paths(cfg, piece, 'both')[0] == first, 'stable while the score is'

  time.sleep(0.02)                                 # mtime is compared with a tolerance
  copy.write_bytes(copy.read_bytes() + b'\n<!-- edited -->\n')
  changed, _ = synth.track_paths(cfg, piece, 'both')
  assert changed != first, 'an edited score must not reuse the old track'
  assert changed.name.startswith(piece.slug + '-both-')


@needs_real
def test_the_grid_is_known_before_anything_is_rendered(real):
  """The phone has to know where the bars are to ask for one, so the grid is answered
  from the piece's reference MIDI without rendering anything."""
  cfg, piece = real
  grid = synth.grid(cfg, piece)
  assert grid and grid['source'] == 'reference MIDI'
  assert grid['beats'] == 3 and grid['first_bar'] == 1
  assert grid['seconds_per_bar'] == pytest.approx(1.4965, abs=1e-3)
  assert not list((cfg.tracks_dir).glob('*.wav')), 'nothing should have been rendered'


@needs_real
def test_the_grid_falls_back_to_the_score_when_there_is_no_bank(tmp_path):
  """A piece with no question bank behind it still needs bar positions."""
  cfg = AnalyzerConfig()
  cfg.cache_dir = tmp_path / 'cache'
  cfg.output_dir = tmp_path / 'out'
  cfg.ensure_dirs()
  score = writes_a_two_staff_score(tmp_path / 'plain.musicxml', bars=4)
  piece = library.Piece(slug='plain', musicxml=score, title='Plain')
  grid = synth.grid(cfg, piece)
  assert grid and grid['source'] == 'score'
  assert grid['beats'] == 4
  # four bars of 4/4 at whatever the average tempo is: seconds per bar is beats/tempo
  assert grid['seconds_per_bar'] == pytest.approx(4 * 60.0 / grid['qpm'])
  assert grid['qpm'] > 0


@needs_fluidsynth
def test_a_single_staff_piece_offers_only_the_whole(tmp_path):
  cfg = AnalyzerConfig()
  cfg.cache_dir = tmp_path / 'cache'
  cfg.output_dir = tmp_path / 'out'
  cfg.ensure_dirs()
  score = writes_a_two_staff_score(tmp_path / 'two.musicxml', bars=1)
  two = library.Piece(slug='two', musicxml=score, title='Two')
  assert synth.available(cfg, two)['tracks'] == ['both', 'left', 'right']

  one = tmp_path / 'one.musicxml'
  one.write_text(score.read_text(encoding='utf-8').replace('<staff>2</staff>', '<staff>1</staff>'),
                 encoding='utf-8')
  single = library.Piece(slug='one', musicxml=one, title='One')
  info = synth.available(cfg, single)
  assert info['tracks'] == ['both']
  assert 'one staff' in info['reason']


def test_a_piece_with_no_score_says_so(tmp_path):
  cfg = AnalyzerConfig()
  cfg.cache_dir = tmp_path / 'cache'
  cfg.output_dir = tmp_path / 'out'
  piece = library.Piece(slug='ghost', musicxml=None, title='Ghost')
  info = synth.available(cfg, piece)
  assert info['tracks'] == [] and 'no score' in info['reason']
  with pytest.raises(synth.SynthError):
    synth.build_track(cfg, piece, 'both')


@needs_real
def test_the_split_lines_up_measure_for_measure(real):
  """The split deletes notes from a parsed document, so the two must agree on the
  measures -- otherwise it would delete the wrong ones."""
  cfg, piece = real
  staves = synth.staff_per_note(piece.musicxml)
  from note_seq import musicxml_parser
  document = musicxml_parser.MusicXMLDocument(str(piece.musicxml))
  measures = document.parts[0].measures
  assert len(staves) == len(measures)
  assert all(len(a) == len(b.notes) for a, b in zip(staves, measures))
