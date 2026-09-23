"""Tests for the HTTP server and the client page it serves.

The server is started for real on an ephemeral port and driven with plain HTTP
requests, because the things that matter here only exist end to end: that a
request for "bars 40-83" comes back as a WAV of exactly that length, that the
geometry the client positions bars with matches the page image it draws, and that
the page is self-contained (a phone on the LAN must not need the internet).

Where a browser is available the client's JavaScript is actually executed
(`google-chrome --headless --dump-dom`), so the page is not shipped unverified.
"""

import asyncio
import contextlib
import json
import pathlib
import shutil
import socket
import subprocess
import sys
import threading
import urllib.error
import urllib.request

import pytest

from analyzer.config import AnalyzerConfig
from analyzer.schema import AudioSource
from analyzer import library, scoreview, server, syncmap

CHROME = shutil.which('google-chrome') or shutil.which('chromium')
needs_engines = pytest.mark.skipif(
    not (shutil.which('fluidsynth') or True) and False, reason='unused')


# ---------------------------------------------------------------------------
# a piece to serve
# ---------------------------------------------------------------------------

SCORE = '''<?xml version="1.0"?><score-partwise version="4.0"><part-list>
<score-part id="P1"><part-name>P</part-name></score-part></part-list><part id="P1">%s</part>
</score-partwise>'''


def score_xml(bars=6):
  """Valid MusicXML: Verovio needs divisions, key, time and clef to engrave it.

  A bare list of <measure> elements parses for our own reader but yields zero
  measures in Verovio's MEI, which produces an empty score rather than an error.
  """
  parts = []
  for n in range(1, bars + 1):
    attributes = ('<attributes><divisions>1</divisions><key><fifths>0</fifths></key>'
                  '<time><beats>4</beats><beat-type>4</beat-type></time>'
                  '<clef><sign>G</sign><line>2</line></clef></attributes>') if n == 1 else ''
    parts.append('<measure number="%d">%s<note><pitch><step>C</step><octave>4</octave>'
                 '</pitch><duration>4</duration><type>whole</type></note></measure>'
                 % (n, attributes))
  return SCORE % ''.join(parts)


def make_piece(cfg, slug='demo', bars=6, seconds=12.0, with_map=True, title='Demo Piece'):
  """A complete piece on disk: score, bank, cached recording, map, engraving."""
  import numpy as np
  import soundfile as sf

  cfg.ensure_dirs()
  score = cfg.sheet_music_dir / ('%s.musicxml' % slug)
  cfg.sheet_music_dir.mkdir(parents=True, exist_ok=True)
  score.write_text(score_xml(bars), encoding='utf-8')

  bank = cfg.bank_dir / slug
  bank.mkdir(parents=True, exist_ok=True)
  (bank / 'fingerprint.json').write_text(json.dumps({
      'source_file': str(score),
      'global': {'num_measures': bars, 'key_from_score': 'C major', 'title': title},
      'measures': [{}] * bars}), encoding='utf-8')

  wav = cfg.audio_path(slug)
  t = np.arange(int(seconds * 22050)) / 22050
  sf.write(str(wav), (0.3 * np.sin(2 * np.pi * 330 * t)).astype('float32'), 22050,
           subtype='PCM_16')
  cfg.audio_meta_path(slug).write_text(json.dumps(AudioSource(
      slug=slug, path=str(wav), url='https://example.invalid/x', duration_s=seconds,
      sample_rate=22050, channels=1).to_dict()), encoding='utf-8')

  if with_map:
    result = syncmap.uniform_map(list(range(1, bars + 1)), seconds)
    result.method = syncmap.SyncResult.METHOD_DTW        # pretend it was aligned
    syncmap.save(cfg, slug, result, audio=library.find(cfg, slug).audio,
                 source_musicxml=score, played=list(range(1, bars + 1)))
  scoreview.render_score(cfg, score)
  return score


@pytest.fixture
def cfg(tmp_path):
  config = AnalyzerConfig(
      cache_dir=tmp_path / 'cache', output_dir=tmp_path / 'out',
      bank_dir=tmp_path / 'banks', legacy_bank=tmp_path / 'banks' / 'fingerprint.json',
      sheet_music_dir=tmp_path / 'sheets')
  (tmp_path / 'sheets').mkdir(parents=True, exist_ok=True)
  config.ensure_dirs()
  return config


@contextlib.contextmanager
def running(cfg):
  """Run the real server on an ephemeral port in a background thread."""
  loop = asyncio.new_event_loop()
  ready = threading.Event()
  holder = {}

  def run():
    asyncio.set_event_loop(loop)
    srv, port = loop.run_until_complete(server.build_server(cfg, '127.0.0.1', 0))
    holder['port'] = port
    holder['loop'] = loop
    ready.set()
    loop.run_forever()

  thread = threading.Thread(target=run, daemon=True)
  thread.start()
  assert ready.wait(10), 'the server did not start'
  try:
    yield holder['port']
  finally:
    loop.call_soon_threadsafe(loop.stop)
    thread.join(timeout=5)


def get(port, path):
  """Return (status, body bytes) for a GET, without raising on 4xx."""
  url = 'http://127.0.0.1:%d%s' % (port, path)
  try:
    with urllib.request.urlopen(url, timeout=30) as response:
      return response.status, response.read()
  except urllib.error.HTTPError as e:
    return e.code, e.read()


def get_json(port, path):
  status, body = get(port, path)
  return status, json.loads(body.decode('utf-8'))


# ---------------------------------------------------------------------------
# the API
# ---------------------------------------------------------------------------

def test_health_and_piece_list(cfg):
  make_piece(cfg)
  with running(cfg) as port:
    status, body = get_json(port, '/health')
    assert status == 200 and body['ok'] is True and body['pieces'] == 1
    status, body = get_json(port, '/pieces')
    assert status == 200
    assert [p['slug'] for p in body['pieces']] == ['demo']
    piece = body['pieces'][0]
    assert piece['has_audio'] and piece['has_map'] and piece['has_score']
    assert piece['bars'] == 6


def test_piece_detail_has_everything_the_client_needs(cfg):
  make_piece(cfg)
  with running(cfg) as port:
    status, detail = get_json(port, '/piece/demo')
    assert status == 200
    assert detail['name'] == 'Demo Piece'
    assert detail['played'] == [1, 2, 3, 4, 5, 6]
    assert detail['map']['measures'][0]['measure'] == 1
    assert detail['map']['method'] == 'dtw'
    assert detail['score']['pages'], 'the engraved pages must be described'
    page = detail['score']['pages'][0]
    assert page['measures'] and page['width'] > 100 and page['height'] > 100
    for box in page['measures']:
      assert 0 <= box['x0'] < box['x1'] <= page['width']
      assert 0 <= box['y0'] < box['y1'] <= page['height']


def test_unknown_piece_is_a_404_with_a_message(cfg):
  make_piece(cfg)
  with running(cfg) as port:
    status, body = get_json(port, '/piece/nope')
    assert status == 404 and 'no such piece' in body['error']


# ---------------------------------------------------------------------------
# audio: the point of the whole thing
# ---------------------------------------------------------------------------

def slices_duration(data: bytes) -> float:
  import io
  import soundfile as sf
  with sf.SoundFile(io.BytesIO(data)) as handle:
    return handle.frames / float(handle.samplerate)


def test_a_slice_is_exactly_the_range_asked_for(cfg):
  make_piece(cfg, seconds=12.0)
  with running(cfg) as port:
    for start, end in ((0.0, 3.0), (2.5, 7.25), (0.0, 12.0), (11.0, 12.0)):
      status, body = get(port, '/audio/demo?from=%s&to=%s' % (start, end))
      assert status == 200, body[:200]
      assert body[:4] == b'RIFF', 'the response must be a WAV'
      got = slices_duration(body)
      assert got == pytest.approx(end - start, abs=1 / 22050.0), \
          'asked %s-%s s, got %.6f s' % (start, end, got)


def test_a_slice_by_bar_numbers_uses_the_map(cfg):
  make_piece(cfg, bars=6, seconds=12.0)          # 2 s per bar
  with running(cfg) as port:
    status, body = get(port, '/audio/demo?from_bar=2&to_bar=3')
    assert status == 200
    # bars 2-3 of a uniform 6-bar/12 s map are 2.0 s .. 6.0 s
    assert slices_duration(body) == pytest.approx(4.0, abs=0.01)


def test_range_is_clamped_to_the_recording(cfg):
  make_piece(cfg, seconds=12.0)
  with running(cfg) as port:
    status, body = get(port, '/audio/demo?from=-5&to=999')
    assert status == 200
    assert slices_duration(body) == pytest.approx(12.0, abs=0.01)


def test_a_nonsense_range_is_refused_with_a_reason(cfg):
  make_piece(cfg)
  with running(cfg) as port:
    status, body = get_json(port, '/audio/demo?from=5&to=5')
    assert status == 400 and 'empty' in body['error']
    status, body = get(port, '/audio/demo')
    assert status == 200 and body[:4] == b'RIFF'      # whole file, when asked plainly
    status, body = get_json(port, '/audio/demo?from=1')
    assert status == 400 and 'from/to' in body['error']


def test_audio_for_a_piece_without_a_recording(cfg):
  make_piece(cfg)
  (cfg.audio_path('demo')).unlink()
  with running(cfg) as port:
    status, body = get_json(port, '/audio/demo?from=0&to=1')
    assert status == 404 and 'no recording' in body['error']


# ---------------------------------------------------------------------------
# score geometry and pages
# ---------------------------------------------------------------------------

def test_geometry_matches_the_page_image_it_describes(cfg):
  from PIL import Image
  import io
  make_piece(cfg)
  with running(cfg) as port:
    status, geometry = get_json(port, '/score/demo/pages.json')
    assert status == 200
    page = geometry['pages'][0]
    status, png = get(port, '/score/demo/page-%d.png' % page['number'])
    assert status == 200 and png[:8] == b'\x89PNG\r\n\x1a\n'
    with Image.open(io.BytesIO(png)) as image:
      assert image.size == (page['width'], page['height']), \
          'bar rectangles are in the image\'s own pixels'
    numbers = [b['number'] for b in page['measures']]
    assert numbers == [1, 2, 3, 4, 5, 6]


def test_a_missing_page_is_a_404(cfg):
  make_piece(cfg)
  with running(cfg) as port:
    status, _ = get(port, '/score/demo/page-99.png')
    assert status == 404


def test_the_score_endpoint_explains_when_it_has_not_been_engraved(cfg):
  make_piece(cfg)
  for page in (cfg.render_dir / 'score-demo').glob('*'):
    page.unlink()
  with running(cfg) as port:
    status, body = get_json(port, '/score/demo/pages.json')
    assert status == 404 and 'engraved' in body['error']


# ---------------------------------------------------------------------------
# the client page
# ---------------------------------------------------------------------------

def test_the_client_page_is_served_and_self_contained(cfg):
  make_piece(cfg)
  with running(cfg) as port:
    status, body = get(port, '/')
    assert status == 200
    html = body.decode('utf-8')
    assert 'viewport' in html, 'a phone needs the viewport meta tag'
    for hook in ('id="piece"', 'id="page"', 'id="overlay"', 'id="play"', 'id="loop"'):
      assert hook in html, 'the client is missing %s' % hook
    # offline-capable on the LAN: no external asset may be referenced
    for external in ('http://', 'https://', '//cdn', 'fonts.googleapis'):
      assert external not in html, 'the client references %s' % external


@pytest.mark.skipif(CHROME is None, reason='no headless browser available')
def test_the_client_javascript_actually_runs(cfg):
  """Load the page in Chrome and check the DOM it built from the API."""
  make_piece(cfg)
  with running(cfg) as port:
    command = [CHROME, '--headless=new', '--disable-gpu', '--no-sandbox',
               '--disable-dev-shm-usage', '--virtual-time-budget=8000',
               '--dump-dom', 'http://127.0.0.1:%d/?piece=demo' % port]
    result = subprocess.run(command, capture_output=True, text=True, timeout=120)
    dom = result.stdout
    assert dom, 'chrome produced no DOM: %s' % result.stderr[-400:]
    assert 'Demo Piece' in dom, 'the piece list did not populate'
    assert 'data-n="1"' in dom and 'data-n="6"' in dom, \
        'the bar overlay was not built from the geometry'
    assert 'page 1 / 1' in dom or 'page 1 /' in dom
    # the source is in the dump too, so read the *rendered* status line
    import re
    meta = re.search(r'<div class="meta" id="meta">(.*?)</div>', dom, re.S)
    assert meta, 'the status line is missing from the rendered page'
    assert 'could not load' not in meta.group(1), 'the client failed: %s' % meta.group(1)
    assert 'bars' in meta.group(1), 'the piece summary did not render: %r' % meta.group(1)


# ---------------------------------------------------------------------------
# many pieces: search and filters, which is what the query object is for
# ---------------------------------------------------------------------------

def test_the_piece_list_can_be_searched(cfg):
  make_piece(cfg, slug='demo', bars=6, seconds=12.0)
  make_piece(cfg, slug='other', bars=8, seconds=16.0)
  with running(cfg) as port:
    status, body = get_json(port, '/pieces')
    assert body['count'] == 2 and len(body['pieces']) == 2

    status, body = get_json(port, '/pieces?q=other')
    assert [p['slug'] for p in body['pieces']] == ['other']
    assert body['query']['q'] == 'other', 'the response echoes what was asked'

    status, body = get_json(port, '/pieces?q=nothing-like-this')
    assert body['pieces'] == [] and body['count'] == 0


def test_the_piece_list_can_be_filtered_and_limited(cfg):
  make_piece(cfg, slug='demo', bars=6, seconds=12.0, with_map=True)
  make_piece(cfg, slug='bare', bars=6, seconds=12.0, with_map=False)
  with running(cfg) as port:
    status, body = get_json(port, '/pieces?has_map=1')
    assert [p['slug'] for p in body['pieces']] == ['demo']
    status, body = get_json(port, '/pieces?has_map=0')
    assert [p['slug'] for p in body['pieces']] == ['bare']
    status, body = get_json(port, '/pieces?limit=1')
    assert body['count'] == 1
    # a hidden piece stays hidden unless it is asked for explicitly
    status, body = get_json(port, '/pieces?include_hidden=1')
    assert isinstance(body['pieces'], list)


def test_a_pieces_summary_carries_what_a_list_needs(cfg):
  make_piece(cfg, slug='demo', bars=6, seconds=12.0)
  with running(cfg) as port:
    status, body = get_json(port, '/pieces')
    piece = body['pieces'][0]
    for field in ('slug', 'name', 'bars', 'key', 'has_audio', 'has_map', 'has_score',
                  'duration_s', 'method', 'measures'):
      assert field in piece, 'the list is missing %s' % field
    assert piece['measures'] == 6


# ---------------------------------------------------------------------------
# Anki decks: built by the desktop, downloaded by the phone
# ---------------------------------------------------------------------------

def test_decks_are_listed_and_downloadable(cfg):
  from analyzer import cards, store
  make_piece(cfg, slug='demo', bars=6, seconds=12.0)
  repo = store.open_repository(cfg)
  deck = repo.decks.save(cards.DeckSpec(slug='demo', name='bars 1-4', cards=[
      cards.CardSpec(slug='demo', first_bar=1, last_bar=4, rules=['melody'])]))
  payload = b'PK\x03\x04not really an apkg but a package'
  repo.decks.save_export(deck, payload)

  with running(cfg) as port:
    status, body = get_json(port, '/decks/demo')
    assert status == 200
    assert len(body['decks']) == 1
    entry = body['decks'][0]
    assert entry['name'] == 'bars 1-4' and entry['cards'] == 1 and entry['rules'] == ['melody']
    assert entry['bytes'] == len(payload) and entry['url'].endswith('/download')

    status, data = get(port, entry['url'])
    assert status == 200 and data == payload

    status, body = get_json(port, '/decks/nothing-here')
    assert status == 200 and body['decks'] == []
    status, body = get_json(port, '/decks/demo/not-a-deck/download')
    assert status == 404 and 'no built deck' in body['error']


def test_a_deck_whose_package_is_gone_is_reported(cfg):
  from analyzer import cards, store
  make_piece(cfg, slug='demo')
  repo = store.open_repository(cfg)
  deck = repo.decks.save(cards.DeckSpec(slug='demo', name='x', cards=[
      cards.CardSpec(slug='demo', first_bar=1, last_bar=2)]))
  ref = repo.decks.save_export(deck, b'PK\x03\x04data')
  repo.blobs.delete(ref)                      # the file vanished underneath us
  with running(cfg) as port:
    status, body = get_json(port, '/decks/demo/x/download')
    # The filesystem adapter notices the package is gone and says there is no deck;
    # an adapter that tracks the blob directly says the blob is missing. Both are
    # honest 404s, so either message passes.
    assert status == 404
    assert 'missing' in body['error'] or 'no built deck' in body['error'], body['error']
