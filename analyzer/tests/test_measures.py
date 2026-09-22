"""Tests for the performance-order expansion of a score.

The MusicXML here is built in the test rather than checked in: playback order is
about *structure* (repeat barlines, endings, jumps), so a six-measure synthetic
score expresses the rule far more clearly than a real piece would -- and it means
these tests run on a fresh clone with no fixtures.
"""

import pathlib
import zipfile

import pytest

from analyzer import measures


# ---------------------------------------------------------------------------
# a tiny MusicXML builder
# ---------------------------------------------------------------------------

def _measure(number, *, forward=False, backward=False, times=None,
             ending_start=None, ending_stop=None, sound='', segno=False,
             coda=False, time_only=None, implicit=False):
  left, right, body = [], [], []
  if forward:
    left.append('<repeat direction="forward"/>')
  if ending_start:
    left.append('<ending number="%s" type="start"/>' % ending_start)
  if backward:
    right.append('<repeat direction="backward"%s/>'
                 % (' times="%d"' % times if times else ''))
  if ending_stop:
    right.append('<ending number="%s" type="stop"/>' % ending_stop)
  if segno:
    body.append('<direction><segno/></direction>')
  if coda:
    body.append('<direction><coda/></direction>')
  if sound:
    body.append('<direction><sound %s/></direction>' % sound)
  if time_only:
    body.append('<direction><sound time-only="%s"/></direction>' % time_only)

  out = '<measure number="%s"%s>' % (number, ' implicit="yes"' if implicit else '')
  if left:
    out += '<barline location="left">%s</barline>' % ''.join(left)
  out += ''.join(body)
  out += ('<note><pitch><step>C</step><octave>4</octave></pitch>'
          '<duration>1</duration><type>quarter</type></note>')
  if right:
    out += '<barline location="right">%s</barline>' % ''.join(right)
  return out + '</measure>'


def make_score(specs, part_count=1):
  """Build a score from a list of per-measure dicts (see `_measure`)."""
  body = ''.join(_measure(i + 1, **spec) for i, spec in enumerate(specs))
  parts = ''.join('<part id="P%d">%s</part>' % (n + 1, body) for n in range(part_count))
  part_list = ''.join('<score-part id="P%d"><part-name>Piano</part-name></score-part>' % (n + 1)
                      for n in range(part_count))
  return ('<?xml version="1.0" encoding="UTF-8"?>'
          '<score-partwise version="4.0"><part-list>%s</part-list>%s</score-partwise>'
          % (part_list, parts))


@pytest.fixture
def write_score(tmp_path):
  def _write(specs, name='piece.musicxml', **kwargs):
    path = tmp_path / name
    path.write_text(make_score(specs, **kwargs), encoding='utf-8')
    return path
  return _write


# ---------------------------------------------------------------------------
# the simple cases
# ---------------------------------------------------------------------------

def test_plain_score_plays_in_order(write_score):
  path = write_score([{} for _ in range(4)])
  expansion = measures.expand_measures(path)
  assert expansion.played_numbers == [1, 2, 3, 4]
  assert expansion.written == 4
  assert expansion.is_expanded is False
  assert expansion.summary() == '4 measures, no repeats'
  assert expansion.warnings == []


def test_simple_repeat_is_played_twice(write_score):
  path = write_score([{}, {'forward': True}, {'backward': True}, {}])
  expansion = measures.expand_measures(path)
  assert expansion.played_numbers == [1, 2, 3, 2, 3, 4]
  assert expansion.written == 4
  assert expansion.is_expanded is True
  assert '(+2' in expansion.summary()


def test_repeat_times_attribute(write_score):
  path = write_score([{}, {'forward': True}, {'backward': True, 'times': 3}, {}])
  assert measures.played_measures(path) == [1, 2, 3, 2, 3, 2, 3, 4]


def test_repeat_pass_numbers_are_recorded(write_score):
  path = write_score([{}, {'forward': True}, {'backward': True}, {}])
  expansion = measures.expand_measures(path)
  passes = {e.play_index: e.pass_no for e in expansion.events}
  assert passes[1] == 1 and passes[4] == 2          # measure 2 first, measure 3 second
  assert [e.is_repeat_pass for e in expansion.events] == \
      [False, False, False, True, True, False]
  assert expansion.play_index_of(2, pass_no=1) == 1
  assert expansion.play_index_of(2, pass_no=2) == 3
  assert expansion.play_index_of(9) is None


def test_two_separate_repeats(write_score):
  path = write_score([{'forward': True}, {'backward': True},
                      {'forward': True}, {'backward': True}])
  assert measures.played_measures(path) == [1, 2, 1, 2, 3, 4, 3, 4]


def test_nested_repeats(write_score):
  # outer repeat 1-4, inner repeat 2-3: 1 2 3 2 3 4 | 1 2 3 2 3 4
  path = write_score([{'forward': True}, {'forward': True},
                      {'backward': True}, {'backward': True}])
  assert measures.played_measures(path) == [1, 2, 3, 2, 3, 4, 1, 2, 3, 2, 3, 4]


# ---------------------------------------------------------------------------
# endings and jumps
# ---------------------------------------------------------------------------

def test_first_and_second_endings(write_score):
  # measures 1-2 repeated, ending 1 is measure 3 (which carries the repeat),
  # ending 2 is measure 4: 1 2 3 | 1 2 4
  path = write_score([{'forward': True}, {},
                      {'backward': True, 'ending_start': '1', 'ending_stop': '1'},
                      {'ending_start': '2', 'ending_stop': '2'}])
  assert measures.played_measures(path) == [1, 2, 3, 1, 2, 4]


def test_dacapo_al_fine(write_score):
  path = write_score([{}, {'sound': 'fine="yes"'}, {},
                      {'sound': 'dacapo="yes"'}])
  # play through, jump back to the start, stop *on* the fine
  assert measures.played_measures(path) == [1, 2, 3, 4, 1, 2]


def test_dal_segno_al_coda(write_score):
  path = write_score([{}, {'segno': True}, {'sound': 'tocoda="coda"'}, {},
                      {'coda': True}, {'sound': 'dalsegno="segno"'}])
  assert measures.played_measures(path) == [1, 2, 3, 4, 5, 6, 2, 3, 5, 6]


def test_dal_segno_without_segno_warns(write_score):
  path = write_score([{}, {}, {'sound': 'dalsegno="segno"'}])
  expansion = measures.expand_measures(path)
  assert any('segno' in w for w in expansion.warnings)
  assert expansion.played_numbers == [1, 2, 3]      # the jump is ignored, not guessed


# ---------------------------------------------------------------------------
# oddities that must be flagged rather than guessed at
# ---------------------------------------------------------------------------

def test_time_only_repeat_warns(write_score):
  path = write_score([{}, {'forward': True, 'time_only': '1,3'},
                      {'backward': True}, {}])
  expansion = measures.expand_measures(path)
  assert any('time-only' in w for w in expansion.warnings)


def test_split_measure_numbers_are_renumbered_with_a_warning(tmp_path):
  specs = [{} for _ in range(3)]
  body = ''.join(_measure(n, **spec) for n, spec in
                 zip(['1', '2a', '2b'], specs))
  path = tmp_path / 'split.musicxml'
  path.write_text('<?xml version="1.0"?><score-partwise version="4.0">'
                  '<part-list><score-part id="P1"><part-name>P</part-name>'
                  '</score-part></part-list><part id="P1">%s</part></score-partwise>' % body,
                  encoding='utf-8')
  expansion = measures.expand_measures(path)
  assert expansion.played_numbers == [1, 2, 3]
  assert any('not plain numbers' in w for w in expansion.warnings)


def test_pickup_measure_is_flagged(write_score):
  path = write_score([{'implicit': True}, {}, {}])
  expansion = measures.expand_measures(path)
  assert [e.is_pickup for e in expansion.events] == [True, False, False]


def test_nonterminating_repeat_is_stopped(write_score):
  # a backward repeat with no forward barline: plays once through, no hang
  path = write_score([{}, {'backward': True}, {}])
  assert measures.played_measures(path) == [1, 2, 3]


# ---------------------------------------------------------------------------
# file formats
# ---------------------------------------------------------------------------

def test_reads_a_compressed_mxl(tmp_path):
  xml = make_score([{}, {'forward': True}, {'backward': True}])
  path = tmp_path / 'packed.mxl'
  with zipfile.ZipFile(path, 'w') as z:
    z.writestr('META-INF/container.xml',
               '<?xml version="1.0"?><container><rootfiles>'
               '<rootfile full-path="score.xml"/></rootfiles></container>')
    z.writestr('score.xml', xml)
  assert measures.played_measures(path) == [1, 2, 3, 2, 3]


def test_multi_part_score_uses_the_measures_it_has(write_score):
  path = write_score([{}, {'forward': True}, {'backward': True}], part_count=2)
  assert measures.played_measures(path) == [1, 2, 3, 2, 3]


def test_missing_file_raises_score_error(tmp_path):
  with pytest.raises(measures.ScoreError):
    measures.expand_measures(tmp_path / 'nope.musicxml')


def test_unparsable_file_raises_score_error(tmp_path):
  path = tmp_path / 'junk.musicxml'
  path.write_text('this is not xml at all', encoding='utf-8')
  with pytest.raises(measures.ScoreError):
    measures.expand_measures(path)


# ---------------------------------------------------------------------------
# the real pieces, when they are on this machine
# ---------------------------------------------------------------------------

CHOPIN = pathlib.Path('/home/wei/sheet_music/chopin-waltz-in-a-minor-op-34-no-2.mxl')
INTERMEZZO = pathlib.Path('/home/wei/sheet_music/Intermezzo_left.mxl')


@pytest.mark.skipif(not CHOPIN.is_file(), reason='Chopin score not on this machine')
def test_chopin_is_through_composed():
  """The waltz is written out flat: 204 measures, no repeat barlines at all."""
  expansion = measures.expand_measures(CHOPIN)
  assert expansion.written == 204
  assert expansion.played_numbers == list(range(1, 205))
  assert expansion.is_expanded is False
  assert 'repeat' not in ' '.join(expansion.warnings).lower()


@pytest.mark.skipif(not INTERMEZZO.is_file(), reason='intermezzo score not on this machine')
def test_intermezzo_expansion():
  expansion = measures.expand_measures(INTERMEZZO)
  assert expansion.written == 70
  assert expansion.played_numbers == list(range(1, 71))
