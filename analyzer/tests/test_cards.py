"""Tests for the Anki card tool.

Two kinds of test here. The pure ones pin the rules and the identity logic, which
is what makes a deck re-exportable instead of duplicating. The rest build a real
card from the real Chopin -- its bank, its recording, its map, its engraving -- and
then check the *pixels*, because "the note is masked" is a claim about an image and
nothing else can check it. I cannot see the cards, so the checks are arithmetic:
what changed inside the masked note's rectangle, and what changed anywhere else.
"""

import json
import pathlib
import sqlite3
import zipfile

import pytest

from analyzer.config import AnalyzerConfig
from analyzer.scoreview import GlyphBox, MeasureBox, ScorePage, ScoreRender
from analyzer import cards, library, scoreview

CHOPIN = pathlib.Path('/home/wei/sheet_music/chopin-waltz-in-a-minor-op-34-no-2.mxl')
SLUG = 'chopin-waltz-in-a-minor-op-34-no-2'
needs_real = pytest.mark.skipif(
    not (CHOPIN.is_file() and AnalyzerConfig().sync_path(SLUG).is_file()),
    reason='the Chopin score, recording and map are needed')


def glyph(measure, staff, index, kind, x0, y0, size=20.0):
  return GlyphBox(measure=measure, staff=staff, index=index, kind=kind,
                  x0=x0, y0=y0, x1=x0 + size, y1=y0 + size)


# ---------------------------------------------------------------------------
# rules
# ---------------------------------------------------------------------------

def sample_glyphs():
  """Two bars, two staves: an RH melody over a chord, and an LH bass."""
  return [
      glyph(1, 1, 0, 'note', 100, 100),      # RH, onset 1, top of the chord
      glyph(1, 1, 1, 'note', 102, 130),      # RH, onset 1, lower note
      glyph(1, 2, 0, 'note', 100, 200),      # LH, onset 1
      glyph(1, 1, 2, 'note', 160, 105),      # RH, onset 2
      glyph(1, 2, 1, 'note', 160, 205),      # LH, onset 2
      glyph(1, 1, 3, 'accid', 90, 95),
      glyph(2, 1, 0, 'note', 220, 100),      # bar 2, onset 1
      glyph(2, 1, 1, 'note', 280, 110),
      glyph(2, 2, 2, 'note', 220, 200),
      glyph(2, 1, 4, 'rest', 300, 100),
  ]


def test_every_rule_resolves_to_the_expected_notes():
  glyphs = sample_glyphs()
  keys = lambda rule: sorted(cards.resolve_rule(rule, glyphs))

  assert keys('everything') == ['1:1:note:0', '1:1:note:1', '1:1:note:2',
                                '1:2:note:0', '1:2:note:1', '2:1:note:0',
                                '2:1:note:1', '2:2:note:2']
  assert keys('right_hand') == ['1:1:note:0', '1:1:note:1', '1:1:note:2',
                                '2:1:note:0', '2:1:note:1']
  assert keys('left_hand') == ['1:2:note:0', '1:2:note:1', '2:2:note:2']
  assert keys('accidentals') == ['1:1:accid:3']


def test_the_melody_rule_takes_the_highest_note_of_each_onset():
  glyphs = sample_glyphs()
  melody = sorted(cards.resolve_rule('melody', glyphs))
  # a higher pitch is a smaller y; the onset at x=100 spans both staves
  assert melody == ['1:1:note:0', '1:1:note:2', '2:1:note:0', '2:1:note:1']
  inner = sorted(cards.resolve_rule('inner_voices', glyphs))
  assert '1:1:note:0' not in inner and '1:1:note:1' in inner


def test_the_first_onset_rule_takes_the_opening_of_each_bar():
  glyphs = sample_glyphs()
  assert sorted(cards.resolve_rule('first_onset', glyphs)) == \
      ['1:1:note:0', '1:1:note:1', '1:2:note:0', '2:1:note:0', '2:2:note:2']


def test_an_unknown_rule_is_an_error_not_a_silent_no_op():
  with pytest.raises(cards.CardError) as caught:
    cards.resolve_rule('left_elbow', sample_glyphs())
  assert 'left_elbow' in str(caught.value)


def test_taps_and_rules_combine_without_duplicates():
  glyphs = sample_glyphs()
  spec = cards.CardSpec(slug='x', first_bar=1, last_bar=2,
                        masks=['1:2:note:0', 'not-a-real-key'],
                        rules=['left_hand'])
  hidden = cards.masked_keys(spec, glyphs)
  assert hidden == ['1:2:note:0', '1:2:note:1', '2:2:note:2']
  assert 'not-a-real-key' not in hidden, 'a stale key must be dropped, not crash'


# ---------------------------------------------------------------------------
# identity: what makes a deck re-exportable
# ---------------------------------------------------------------------------

def test_the_signature_depends_on_the_choices_not_their_order():
  a = cards.CardSpec(slug='p', first_bar=4, last_bar=6, masks=['x', 'y'], rules=['melody'])
  b = cards.CardSpec(slug='p', first_bar=6, last_bar=4, masks=['y', 'x'], rules=['melody'])
  assert a.signature() == b.signature()
  c = cards.CardSpec(slug='p', first_bar=4, last_bar=6, masks=['x'], rules=['melody'])
  assert c.signature() != a.signature()
  d = cards.CardSpec(slug='other', first_bar=4, last_bar=6, masks=['x', 'y'], rules=['melody'])
  assert d.signature() != a.signature()


def test_the_guid_is_stable_for_the_same_card():
  spec = cards.CardSpec(slug='p', first_bar=4, last_bar=6, rules=['melody'])
  assert cards._guid_for(spec) == cards._guid_for(cards.CardSpec(
      slug='p', first_bar=4, last_bar=6, rules=['melody']))
  other = cards.CardSpec(slug='p', first_bar=4, last_bar=7, rules=['melody'])
  assert cards._guid_for(other) != cards._guid_for(spec)


def test_a_spec_round_trips_through_json():
  spec = cards.CardSpec(slug='p', first_bar=1, last_bar=2, title='T', masks=['a'],
                        rules=['melody'], fields={'extra': 'x'}, tags=['exam'],
                        audio_side='front', play_index=3)
  again = cards.CardSpec.from_dict(json.loads(json.dumps(spec.to_dict())))
  assert again == spec


# ---------------------------------------------------------------------------
# the real piece: cropping and masking, checked in pixels
# ---------------------------------------------------------------------------

@pytest.fixture(scope='module')
def real():
  cfg = AnalyzerConfig()
  piece = library.find(cfg, SLUG)
  if piece is None or piece.musicxml is None:
    pytest.skip('the Chopin piece is not in the library')
  render = scoreview.render_score(cfg, piece.musicxml)
  return cfg, piece, render


@needs_real
def test_a_passage_is_cropped_one_strip_per_system(real):
  cfg, piece, render = real
  strips = cards.passage_strips(render, 40, 83)
  assert len(strips) >= 3, '44 bars cannot be one line of music'
  pages = [page for page, _rect in strips]
  assert pages == sorted(pages), 'strips must come in reading order'
  systems_per_page = len(strips) / max(1, len(set(pages)))
  assert systems_per_page >= 1, 'a page holds several systems'

  # every bar of the passage must be inside exactly one strip
  covered = 0
  for number in range(40, 84):
    for page_number, (x0, y0, x1, y1) in strips:
      page = next(p for p in render.pages if p.number == page_number)
      box = next((b for b in page.measures if b.number == number), None)
      if box is not None and x0 - 1 <= box.x0 and box.x1 <= x1 + 1 \
          and y0 - 1 <= box.y0 and box.y1 <= y1 + 1:
        covered += 1
        break
  assert covered == 44, 'only %d of 44 bars are inside a strip' % covered
  for page_number, (x0, y0, x1, y1) in strips:
    page = next(p for p in render.pages if p.number == page_number)
    assert 0 <= x0 < x1 <= page.width and 0 <= y0 < y1 <= page.height


@needs_real
def test_a_card_is_built_with_a_picture_a_clip_and_filled_fields(real, tmp_path):
  import soundfile as sf
  cfg, piece, render = real
  spec = cards.CardSpec(slug=SLUG, first_bar=40, last_bar=43, rules=['left_hand'],
                        tags=['section-B'])
  built = cards.build_card(cfg, piece, render, spec, tmp_path)

  assert built.front_png.is_file() and built.back_png.is_file()
  assert built.front_png.stat().st_size > 1000
  assert built.audio is not None, 'the piece has a recording, so there should be a clip'
  info = sf.info(str(built.audio))
  assert 4.0 < info.frames / info.samplerate < 12.0, 'four bars plus the rolls'

  assert built.fields['range'] == 'bars 40-43'
  assert built.fields['piece'], 'the back should name the piece'
  assert 'chords' in built.fields, 'the bank knows the chords'
  assert 'notehead' in built.fields['masked']


@needs_real
def test_the_back_shows_the_notes_the_front_hides(real, tmp_path):
  import numpy as np
  cfg, piece, render = real
  import PIL.Image
  spec = cards.CardSpec(slug=SLUG, first_bar=53, last_bar=53, rules=['staves_only'])
  built = cards.build_card(cfg, piece, render, spec, tmp_path)
  front = np.asarray(PIL.Image.open(str(built.front_png)).convert('L'))
  back = np.asarray(PIL.Image.open(str(built.back_png)).convert('L'))
  assert (back < 128).sum() > (front < 128).sum(), \
      'the back must show what the front hides'


# ---------------------------------------------------------------------------
# the deck
# ---------------------------------------------------------------------------

@needs_real
def test_a_deck_is_packaged_with_its_media(real, tmp_path):
  cfg, piece, render = real
  deck = cards.DeckSpec(slug=SLUG, name='Chopin Op.34/2 — first page', cards=[
      cards.CardSpec(slug=SLUG, first_bar=1, last_bar=4, rules=['melody'], tags=['A']),
      cards.CardSpec(slug=SLUG, first_bar=40, last_bar=43, rules=['left_hand'], tags=['B']),
  ])
  package = cards.build_deck(cfg, piece, deck, workspace=tmp_path)
  assert package.is_file() and package.suffix == '.apkg'

  with zipfile.ZipFile(package) as z:
    names = z.namelist()
    assert 'collection.anki2' in names and 'media' in names
    media = json.loads(z.read('media'))
    assert len(media) == 6, 'two cards x (front, back, clip)'
    assert sum(1 for name in media.values() if name.endswith('.png')) == 4
    assert sum(1 for name in media.values() if name.endswith(('.mp3', '.wav'))) == 2

    db_path = tmp_path / 'collection.anki2'
    db_path.write_bytes(z.read('collection.anki2'))
    db = sqlite3.connect(str(db_path))
    rows = db.execute('select flds, guid from notes').fetchall()
    assert len(rows) == 2
    for fields, guid in rows:
      assert '<img src="front-' in fields and '<img src="back-' in fields
      assert '[sound:passage-' in fields, 'the clip belongs on the card'
      assert 'bars ' in fields, 'the back should carry the bar range'
    db.close()

  # one deck, two distinct notes, and the guids match what we would ask for again
  specs = deck.cards
  assert len({cards._guid_for(s) for s in specs}) == 2
  assert {guid for _fields, guid in rows} == {cards._guid_for(s) for s in specs}


@needs_real
def test_rebuilding_a_deck_reuses_the_same_guids(real, tmp_path):
  """Otherwise every export would duplicate the user's collection."""
  cfg, piece, render = real
  def guids():
    deck = cards.DeckSpec(slug=SLUG, name='same', cards=[
        cards.CardSpec(slug=SLUG, first_bar=1, last_bar=4, rules=['melody'])])
    package = cards.build_deck(cfg, piece, deck, workspace=tmp_path / 'again')
    with zipfile.ZipFile(package) as z:
      path = tmp_path / 'again-collection.anki2'
      path.write_bytes(z.read('collection.anki2'))
    db = sqlite3.connect(str(path))
    out = {g for (_f, g) in db.execute('select flds, guid from notes')}
    db.close()
    return out
  assert guids() == guids()


@needs_real
def test_fields_come_from_the_bank_not_from_the_user(real, tmp_path):
  cfg, piece, render = real
  fields = cards.memorisation_fields(piece, 53, 53)
  assert fields['range'] == 'bars 53-53'
  assert fields.get('chords'), 'the chord should be filled in automatically'
  assert fields.get('section'), 'the section should be filled in automatically'
  assert 'notes' in fields


@needs_real
def test_an_impossible_passage_is_reported(real, tmp_path):
  cfg, piece, render = real
  spec = cards.CardSpec(slug=SLUG, first_bar=900, last_bar=904)
  with pytest.raises(cards.CardError) as caught:
    cards.build_card(cfg, piece, render, spec, tmp_path)
  assert 'not in the engraving' in str(caught.value)

  deck = cards.DeckSpec(slug=SLUG, name='nothing', cards=[spec])
  with pytest.raises(cards.CardError):
    cards.build_deck(cfg, piece, deck, workspace=tmp_path)
@needs_real
def test_masking_one_note_changes_a_small_local_area(real):
  """What can honestly be asserted about one masked notehead.

  Not "inside the note's bounding box": Verovio's per-note box is tighter than the
  drawn glyph and sits off it (measured on this score), so a box-based assertion
  tests the box, not the masking. What is asserted instead is the *shape* of the
  change -- small, local, and a strict subset of what masking everything changes --
  which holds however the engraver chose to draw the note.
  """
  import numpy as np
  cfg, piece, render = real
  spec = cards.CardSpec(slug=SLUG, first_bar=53, last_bar=53)
  glyphs = render.glyphs_in_measures(53, 53)
  target = next(g for g in glyphs if g.kind == 'note' and g.staff == 1)

  back = cards.passage_image(cfg, piece, render, spec, [target.key], hide=False)
  front = cards.passage_image(cfg, piece, render, spec, [target.key], hide=True)
  a = np.asarray(back.convert('L')).astype(int)
  b = np.asarray(front.convert('L')).astype(int)
  changed = np.argwhere(a != b)
  assert len(changed) > 0, 'hiding the note changed nothing'

  # local: the change fits in a box a small fraction of the card
  height = changed[:, 0].max() - changed[:, 0].min() + 1
  width = changed[:, 1].max() - changed[:, 1].min() + 1
  assert height * width < 0.10 * a.size, 'the change covers too much of the card'
  assert height < 120 and width < 60, 'one note and its stem, not a region'

  # and it is a subset of masking the whole bar: nothing appears that is not also
  # removed when everything goes
  everything = cards.resolve_rule('everything', glyphs)
  all_front = cards.passage_image(cfg, piece, render, spec, everything, hide=True)
  c = np.asarray(all_front.convert('L')).astype(int)
  extra = (a != b) & (a == c)
  assert extra.sum() == 0, '%d pixels changed that masking everything leaves alone' % extra.sum()


@needs_real
def test_masking_more_notes_hides_more(real):
  """Monotonic and additive: the rule selections behave like sets of notes."""
  import numpy as np
  cfg, piece, render = real
  spec = cards.CardSpec(slug=SLUG, first_bar=53, last_bar=53)
  glyphs = render.glyphs_in_measures(53, 53)
  back = cards.passage_image(cfg, piece, render, spec, [], hide=False)
  grey = np.asarray(back.convert('L')).astype(int)

  def ink(keys):
    image = cards.passage_image(cfg, piece, render, spec, keys, hide=True)
    return float((np.asarray(image.convert('L')) < 128).mean())

  right = cards.resolve_rule('right_hand', glyphs)
  left = cards.resolve_rule('left_hand', glyphs)
  both = sorted(set(right) | set(left))
  assert ink(right) < ink([]) > ink(left), 'either hand alone must remove ink'
  assert ink(both) < ink(right) and ink(both) < ink(left), 'both hands remove the most'
  assert ink(both) == pytest.approx(ink(cards.resolve_rule('everything', glyphs)))


@needs_real
def test_the_staves_only_rule_leaves_an_empty_bar(real):
  """The stronger mask: pitches, rhythm and accidentals go; the staves stay."""
  import numpy as np
  cfg, piece, render = real
  spec = cards.CardSpec(slug=SLUG, first_bar=53, last_bar=53)
  glyphs = render.glyphs_in_measures(53, 53)
  hidden = cards.resolve_rule('staves_only', glyphs)
  assert len(hidden) > len(cards.resolve_rule('everything', glyphs)), \
      'staves_only should hide more than the noteheads alone'

  back = cards.passage_image(cfg, piece, render, spec, hidden, hide=False)
  front = cards.passage_image(cfg, piece, render, spec, hidden, hide=True)

  def ink(image):
    return float((np.asarray(image.convert('L')) < 128).mean())
  assert ink(front) < ink(back) - 0.01, \
      'an empty bar should be measurably emptier (%.4f -> %.4f)' % (ink(back), ink(front))
  assert ink(front) > 0.04, 'but the staves must remain, or the card is unreadable'


def _glyph_rect(render, image, glyph, bar: int, cfg=None, piece=None, spec=None):
  """A glyph's rectangle in the pixels of a passage image.

  The crop is measured from the ink (`passage_regions`), so the test has to ask for
  the same region the card used -- otherwise it checks the wrong part of the picture.
  """
  regions = cards.passage_regions(cfg, piece, render, spec) if cfg is not None else None
  strips = cards.passage_strips(render, bar, bar)
  page_number = strips[0][0]
  page = next(p for p in render.pages if p.number == page_number)
  ratio = cards.CARDS_SCALE / scoreview.RASTER_SCALE
  page_size = (page.width * ratio, page.height * ratio)
  rect = regions[page_number] if regions and page_number in regions else \
      tuple(v * ratio for v in strips[0][1])
  placed = cards.strip_offsets([(page_number, rect)], page_size)
  home = placed[0]
  top = int(glyph.y0 * ratio - home['source_top'] + home['paste_y'])
  bottom = int(glyph.y1 * ratio - home['source_top'] + home['paste_y'])
  left = int(glyph.x0 * ratio - home['source_left'] + home['paste_x'])
  right = int(glyph.x1 * ratio - home['source_left'] + home['paste_x'])
  return top, bottom, left, right




@needs_real
def test_a_crop_includes_the_music_outside_the_staves(real):
  """The bug this pins: cropping from the staff boxes cut through the music, so the
  back of the card showed bars with their top and bottom sliced off. Measured on
  every passage tried, ink ran off the top or bottom edge."""
  import numpy as np
  cfg, piece, render = real
  for low, high in ((53, 53), (1, 4), (100, 103), (200, 204)):
    spec = cards.CardSpec(slug=SLUG, first_bar=low, last_bar=high)
    image = cards.passage_image(cfg, piece, render, spec, [], hide=False)
    grey = np.asarray(image.convert('L'))
    dark = grey < 128

    # taller than the staves, because leger lines, dynamics and tempo text live
    # outside them
    ratio = cards.CARDS_SCALE / scoreview.RASTER_SCALE
    staff_height = sum((r[1][3] - r[1][1]) for r in cards.passage_strips(render, low, high)) * ratio
    assert image.height > staff_height * 1.3, \
        'bars %d-%d: the crop is no taller than the staves, so it clips' % (low, high)
    assert image.height < staff_height * 3.0, \
        'bars %d-%d: the crop swallowed the neighbouring system' % (low, high)

    # Clipping through a staff or a system shows up as a dense band along an edge,
    # because a staff line is as wide as the crop. A barline at the left or right
    # edge is normal -- that is where a measure ends -- so only the horizontal
    # edges are judged, and by density rather than a raw count.
    for edge, name in ((dark[:2, :], 'top'), (dark[-2:, :], 'bottom')):
      density = float(edge.mean())
      assert density < 0.3, \
          'bars %d-%d: %.0f%% of the %s edge is ink -- the crop cuts through music' % (
              low, high, 100 * density, name)


@needs_real
def test_both_sides_of_a_card_show_the_same_region(real, tmp_path):
  """The front and the back must line up, so the crop is measured once."""
  import numpy as np
  cfg, piece, render = real
  spec = cards.CardSpec(slug=SLUG, first_bar=53, last_bar=53, rules=['staves_only'])
  built = cards.build_card(cfg, piece, render, spec, tmp_path)
  import PIL.Image
  front = PIL.Image.open(str(built.front_png))
  back = PIL.Image.open(str(built.back_png))
  assert front.size == back.size, 'the two sides differ: %s vs %s' % (front.size, back.size)
  a = np.asarray(front.convert('L')).astype(int)
  b = np.asarray(back.convert('L')).astype(int)
  assert (a != b).any(), 'the back should differ from the front (it shows the notes)'


@needs_real
def test_the_back_does_not_repeat_the_front(real, tmp_path):
  """`{{FrontSide}}` on the answer side renders the question again, so a card showed
  the masked passage above the answer as well as the full one."""
  import json
  import sqlite3
  import zipfile
  cfg, piece, render = real
  deck = cards.DeckSpec(slug=SLUG, name='probe', cards=[
      cards.CardSpec(slug=SLUG, first_bar=53, last_bar=53, rules=['everything'])])
  package = cards.build_deck(cfg, piece, deck, workspace=tmp_path)

  with zipfile.ZipFile(package) as z:
    db_path = tmp_path / 'probe.anki2'
    db_path.write_bytes(z.read('collection.anki2'))
  db = sqlite3.connect(str(db_path))
  models = json.loads(db.execute('select models from col').fetchone()[0])
  template = list(models.values())[0]['tmpls'][0]
  assert '{{FrontSide}}' not in template['afmt'], \
      'the answer side must not re-render the question'
  assert '{{Back}}' in template['afmt']
  assert '{{Front}}' in template['qfmt']

  fields = db.execute('select flds from notes').fetchone()[0]
  front_field, back_field = fields.split('\x1f')
  assert 'front-' in front_field and 'back-' not in front_field
  assert 'back-' in back_field and 'front-' not in back_field, \
      'each side carries its own image'
  db.close()


# ---------------------------------------------------------------------------
# fingering, engraved rather than painted
# ---------------------------------------------------------------------------

@needs_real
def test_adding_fingering_does_not_change_the_music(real):
  """The one place the tool writes score content, so it is checked hardest: the
  measures, the notes and every pitch must come back identical."""
  import xml.etree.ElementTree as ET
  cfg, piece, render = real
  from analyzer.scoreview import _score_xml
  original = _score_xml(pathlib.Path(piece.musicxml))

  def shape(text):
    root = ET.fromstring(text)
    local = lambda t: t.rsplit('}', 1)[-1]
    notes = [e for e in root.iter() if local(e.tag) == 'note']
    pitches = [''.join(c.text or '' for c in n if local(c.tag) == 'pitch') for n in notes]
    measures = [e for e in root.iter() if local(e.tag) == 'measure']
    return len(measures), len(notes), pitches

  fingered = cards.fingered_score_xml(original, {
      '53:1:note:0': '3', '53:2:note:0': '1', '53:2:note:4': '5'})
  assert shape(fingered) == shape(original), 'the music must be untouched'
  assert fingered.count('<fingering') == 3
  # right hand above, left hand below
  assert 'placement="above"' in fingered and 'placement="below"' in fingered


@needs_real
def test_fingering_is_engraved_and_hidden_on_the_front(real, tmp_path):
  """The number is engraved by Verovio (so it is placed properly) and the front
  hides it, which is the question the card asks."""
  import numpy as np
  import PIL.Image
  cfg, piece, render = real
  spec = cards.CardSpec(slug=SLUG, first_bar=53, last_bar=53,
                        fingering={'53:1:note:0': '3', '53:2:note:0': '1'})
  xml_text = cards.card_score_xml(piece, spec)
  assert xml_text and '<fingering' in xml_text

  svg, _ids = scoreview.page_svg_and_ids(cfg, piece.musicxml, 2, scale=cards.CARDS_SCALE,
                                         xml_text=xml_text)
  assert 'class="fing"' in svg, 'the fingering should be engraved'
  plain, _ = scoreview.page_svg_and_ids(cfg, piece.musicxml, 2, scale=cards.CARDS_SCALE)
  assert 'class="fing"' not in plain

  built = cards.build_card(cfg, piece, render, spec, tmp_path)
  front = np.asarray(PIL.Image.open(str(built.front_png)).convert('L'))
  back = np.asarray(PIL.Image.open(str(built.back_png)).convert('L'))
  assert front.shape == back.shape
  assert (back < 128).sum() > (front < 128).sum(), \
      'the back shows the numbers the front hides'
  assert 'fingering' in built.fields, 'the back should also say them in words'
  assert built.fields['fingering'].strip()


@needs_real
def test_a_card_with_fingering_is_self_describing(real):
  """The card records the fingerings it used, so re-exporting it does not depend on
  the piece's current set."""
  cfg, piece, render = real
  spec = cards.CardSpec(slug=SLUG, first_bar=53, last_bar=53, fingering={'53:1:note:0': '2'})
  assert cards.fingerings_in(spec, piece) == {'53:1:note:0': '2'}
  other = cards.CardSpec(slug=SLUG, first_bar=53, last_bar=53)
  assert cards.fingerings_in(other, piece) == {}, 'nothing invented for a card without'
  assert spec.signature() != other.signature(), 'fingering is part of the card identity'


# ---------------------------------------------------------------------------
# the question, written and spoken
# ---------------------------------------------------------------------------

@needs_real
def test_a_suggested_question_comes_from_the_bank(real):
  cfg, piece, render = real
  question = cards.suggest_question(piece, 53, 53)
  assert question and question.endswith('?')
  assert '53' in question, question
  # the bank only asks about one bar at a time, so a range must not borrow a
  # single-bar question: that would ask about bar 40 and ignore bars 41 to 43
  ranged = cards.suggest_question(piece, 40, 43)
  assert ranged == 'Play bars 40 to 43 from memory.', ranged
  assert cards.suggest_question(piece, 41, 41).endswith('measure 41?'), 'single bars still do'


@needs_real
def test_the_question_and_its_clip_go_on_the_front(real, tmp_path, monkeypatch):
  """With speech stubbed out, so the test says what the card contains rather than
  whether a server happens to be running."""
  import sqlite3
  import zipfile
  from analyzer import speech
  cfg, piece, render = real

  def fake_speak(text, path, **kw):
    path = pathlib.Path(str(path) + '.mp3')
    path.write_bytes(b'ID3' + text.encode('utf-8'))
    return path, ''

  monkeypatch.setattr(speech, 'speak_to_file', fake_speak)

  spec = cards.CardSpec(slug=SLUG, first_bar=53, last_bar=53, rules=['everything'],
                        question='What is the chord in bar 53?')
  deck = cards.DeckSpec(slug=SLUG, name='spoken', cards=[spec])
  package = cards.build_deck(cfg, piece, deck, workspace=tmp_path)

  with zipfile.ZipFile(package) as z:
    db_path = tmp_path / 'spoken.anki2'
    db_path.write_bytes(z.read('collection.anki2'))
    media = json.loads(z.read('media'))
  db = sqlite3.connect(str(db_path))
  front, back = db.execute('select flds from notes').fetchone()[0].split('\x1f')
  db.close()

  assert 'What is the chord in bar 53?' in front, 'the question belongs on the front'
  assert '[sound:question-' in front, 'and the clip with it'
  assert front.index('<img') < front.index('question">'), 'picture first, then the question'
  assert 'What is the chord in bar 53?' in back, 'the wording comes back with the answer'
  assert back.index('<img') < back.index('question">'), \
      'and the score stays where it was on the front, so it does not jump on reveal'
  assert any(name.startswith('question-') for name in media.values())


@needs_real
def test_a_speech_failure_leaves_a_usable_card(real, tmp_path, monkeypatch):
  cfg, piece, render = real
  spec = cards.CardSpec(slug=SLUG, first_bar=53, last_bar=53, rules=['everything'],
                        question='Say this out loud?')
  monkeypatch.setattr('analyzer.speech.speak_to_file',
                      lambda text, path, **kw: (None, 'no speech server at http://x'))
  built = cards.build_card(cfg, piece, render, spec, tmp_path)
  assert built.question_audio is None
  assert built.warnings and 'could not speak the question' in built.warnings[0]
  assert built.fields, 'the rest of the card is still built'


@needs_real
def test_rewording_a_question_updates_the_card_rather_than_adding_one(real):
  """The question is not part of a card's identity: editing the wording should update
  the card you have, not leave a second one behind in Anki."""
  a = cards.CardSpec(slug=SLUG, first_bar=53, last_bar=53, rules=['everything'],
                     question='What is the chord here?')
  b = cards.CardSpec(slug=SLUG, first_bar=53, last_bar=53, rules=['everything'],
                     question='Name the chord in this bar.')
  assert a.signature() == b.signature()
  assert cards._guid_for(a) == cards._guid_for(b)
  c = cards.CardSpec(slug=SLUG, first_bar=53, last_bar=53, rules=['left_hand'],
                     question='What is the chord here?')
  assert c.signature() != a.signature(), 'the masks still identify it'


@needs_real
def test_the_question_can_be_kept_off_the_back(real, tmp_path, monkeypatch):
  """The repeat is a nicety, not a rule: some cards read better ending on the answer."""
  import sqlite3
  import zipfile
  from analyzer import speech
  cfg, piece, render = real
  monkeypatch.setattr(speech, 'speak_to_file',
                      lambda text, path, **kw: (None, 'no speech server'))
  spec = cards.CardSpec(slug=SLUG, first_bar=53, last_bar=53, rules=['everything'],
                        question='What is the chord in bar 53?', repeat_question=False)
  deck = cards.DeckSpec(slug=SLUG, name='no repeat', cards=[spec])
  package = cards.build_deck(cfg, piece, deck, workspace=tmp_path)

  with zipfile.ZipFile(package) as z:
    db_path = tmp_path / 'r.anki2'
    db_path.write_bytes(z.read('collection.anki2'))
  db = sqlite3.connect(str(db_path))
  front, back = db.execute('select flds from notes').fetchone()[0].split('\x1f')
  db.close()
  assert 'What is the chord in bar 53?' in front
  assert 'question">' not in back, 'asked once, on the front'
  assert '<img' in back and 'chords' in back, 'the answer is still there'
  assert back.index('<img') == 0, 'and the score still starts where the front left it'


@needs_real
def test_how_the_back_is_written_is_not_part_of_the_card_identity(real):
  """Turning the repeat off should re-write the card you have, not add another."""
  asked = cards.CardSpec(slug=SLUG, first_bar=53, last_bar=53, rules=['everything'],
                         question='What is the chord here?')
  quiet = cards.CardSpec(slug=SLUG, first_bar=53, last_bar=53, rules=['everything'],
                         question='What is the chord here?', repeat_question=False)
  assert asked.signature() == quiet.signature()
  assert asked.to_dict()['repeat_question'] is True, 'and it survives a round trip'
  assert cards.CardSpec.from_dict(quiet.to_dict()).repeat_question is False
  older = {'slug': SLUG, 'first_bar': 53, 'last_bar': 53}
  assert cards.CardSpec.from_dict(older).repeat_question is True, \
      'cards written before the option existed still repeat, as they did'
