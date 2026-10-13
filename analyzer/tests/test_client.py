"""Interaction tests for the phone client, driven through a real browser.

The API tests in `test_server.py` cannot catch a bug in the page itself -- and one
shipped: the Play button was refreshed only when a piece loaded and when the range
was cleared, never when a bar was tapped, so unless the URL already carried a
range the button stayed greyed out for ever.

So these tests load the page in headless Chrome and drive it over the DevTools
protocol, clicking the way a person would and then checking what the page did
about it -- including that the audio URL it asks for is served correctly.

Requires `google-chrome` (or chromium) and `websocket-client`; both are skipped
cleanly when absent.
"""

import json
import pathlib
import shutil
import socket
import subprocess
import time
import urllib.request

import pytest

from analyzer.config import AnalyzerConfig
from test_server import cfg, make_piece, running, get, slices_duration   # noqa: F401

CHROME = shutil.which('google-chrome') or shutil.which('chromium')
pytestmark = [
    pytest.mark.skipif(CHROME is None, reason='no headless browser available'),
    pytest.mark.slow,
]

websocket = pytest.importorskip('websocket', reason='needs websocket-client to drive the browser')


def free_port() -> int:
  with socket.socket() as probe:
    probe.bind(('127.0.0.1', 0))
    return probe.getsockname()[1]


class Browser:
  """A headless Chrome tab, driven over the DevTools protocol."""

  def __init__(self, profile: pathlib.Path):
    self.port = free_port()
    self.profile = profile
    profile.mkdir(parents=True, exist_ok=True)
    self.process = subprocess.Popen(
        [CHROME, '--headless=new', '--disable-gpu', '--no-sandbox',
         '--disable-dev-shm-usage', '--mute-audio', '--remote-allow-origins=*',
         '--remote-debugging-port=%d' % self.port,
         '--user-data-dir=%s' % profile, 'about:blank'],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    self.next_id = 0
    self.ws = self._connect()

  def _connect(self, timeout: float = 30.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
      try:
        listing = json.load(urllib.request.urlopen(
            'http://127.0.0.1:%d/json/list' % self.port, timeout=3))
        page = next(t for t in listing if t.get('type') == 'page')
        ws = websocket.create_connection(page['webSocketDebuggerUrl'], timeout=30)
        self._send(ws, 'Runtime.enable')
        self._send(ws, 'Page.enable')
        return ws
      except Exception:
        time.sleep(0.5)
    raise RuntimeError('chrome never exposed a debugging target')

  def _send(self, ws, method: str, params=None) -> int:
    self.next_id += 1
    ws.send(json.dumps({'id': self.next_id, 'method': method, 'params': params or {}}))
    return self.next_id

  def _call(self, method: str, params=None):
    wanted = self._send(self.ws, method, params)
    while True:
      message = json.loads(self.ws.recv())
      if message.get('id') == wanted:
        if 'error' in message:
          raise AssertionError('%s failed: %s' % (method, message['error']))
        return message.get('result', {})

  def evaluate(self, expression: str):
    result = self._call('Runtime.evaluate',
                        {'expression': expression, 'returnByValue': True,
                         'awaitPromise': True})
    if 'exceptionDetails' in result:
      raise AssertionError('page threw: %s' % result['exceptionDetails'])
    return result.get('result', {}).get('value')

  def open(self, url: str, wait_for: str = 'document.querySelectorAll(".bar").length', timeout=30.0):
    self._call('Page.navigate', {'url': url})
    deadline = time.time() + timeout
    while time.time() < deadline:
      try:
        if (self.evaluate(wait_for) or 0) > 0:
          return
      except AssertionError:
        raise
      time.sleep(0.3)
    raise AssertionError('the page never finished loading: %s' % self.evaluate(
        'document.body.innerText.slice(0, 300)'))

  def close(self):
    try:
      self.ws.close()
    except Exception:
      pass
    self.process.terminate()
    try:
      self.process.wait(timeout=10)
    except subprocess.TimeoutExpired:
      self.process.kill()


@pytest.fixture
def browser(tmp_path):
  instance = Browser(tmp_path / 'chrome')
  yield instance
  instance.close()


# ---------------------------------------------------------------------------

def test_play_offers_the_whole_piece_then_the_tapped_passage(cfg, tmp_path, browser):
  """The regression this pins: tapping a bar must enable Play without a range in the
  URL. With a track available it is enabled from the start -- as the whole piece."""
  make_piece(cfg, bars=6, seconds=12.0)
  with running(cfg) as port:
    browser.open('http://127.0.0.1:%d/' % port)

    assert browser.evaluate('document.getElementById("play").disabled') is False
    assert browser.evaluate('document.getElementById("play").textContent').strip() == '▶ Whole piece'
    assert browser.evaluate('document.querySelectorAll(".bar").length') == 6

    browser.evaluate('document.querySelectorAll(".bar")[2].click()')     # bar 3
    assert browser.evaluate('document.getElementById("play").disabled') is False, \
        'tapping a bar must make the passage playable'
    assert browser.evaluate('document.getElementById("play").textContent').strip() == '▶ Play'
    assert 'bar' in browser.evaluate('document.getElementById("rangeline").textContent')

    browser.evaluate('document.querySelectorAll(".bar")[3].click()')     # bar 4 ends it
    line = browser.evaluate('document.getElementById("rangeline").textContent')
    assert '3' in line and '4' in line


def test_play_requests_exactly_the_tapped_passage(cfg, tmp_path, browser):
  make_piece(cfg, bars=6, seconds=12.0)          # 2 s per bar
  with running(cfg) as port:
    browser.open('http://127.0.0.1:%d/' % port)
    browser.evaluate('document.querySelectorAll(".bar")[2].click()')     # bars 3-4
    browser.evaluate('document.querySelectorAll(".bar")[3].click()')
    browser.evaluate('document.getElementById("play").click()')
    time.sleep(0.5)

    src = browser.evaluate('document.getElementById("player").src')
    assert '/audio/' in src and 'from=' in src and 'to=' in src, src
    query = src.split('?', 1)[1]

    # the URL the page chose must be served, and be the passage plus its rolls
    path = '/audio/demo?' + query
    status, body = get(port, path)
    assert status == 200, body[:200]
    assert body[:4] == b'RIFF'
    assert slices_duration(body) == pytest.approx(4.0 + 0.12 + 0.25, abs=0.02)


def test_a_bookmark_opens_with_its_range_ready(cfg, tmp_path, browser):
  make_piece(cfg, bars=6, seconds=12.0)
  with running(cfg) as port:
    browser.open('http://127.0.0.1:%d/?piece=demo&from=2&to=5' % port)
    assert browser.evaluate('document.getElementById("play").disabled') is False
    line = browser.evaluate('document.getElementById("rangeline").textContent')
    assert '2' in line and '5' in line
    selected = browser.evaluate('document.querySelectorAll(".bar.sel").length')
    assert selected == 4, 'bars 2-5 should be highlighted, got %s' % selected


def test_clear_resets_the_range_and_goes_back_to_the_whole_piece(cfg, tmp_path, browser):
  make_piece(cfg, bars=6, seconds=12.0)
  with running(cfg) as port:
    browser.open('http://127.0.0.1:%d/?piece=demo&from=1&to=3' % port)
    assert browser.evaluate('document.getElementById("play").textContent').strip() == '▶ Play'
    browser.evaluate('document.getElementById("clear").click()')
    assert browser.evaluate('document.querySelectorAll(".bar.sel").length') == 0
    assert browser.evaluate('document.getElementById("play").disabled') is False
    assert browser.evaluate('document.getElementById("play").textContent').strip() == '▶ Whole piece'


def test_a_recording_that_was_never_aligned_says_what_is_missing(cfg, tmp_path, browser):
  """The one offered track cannot play, and the phone says why rather than going quiet."""
  make_piece(cfg, bars=6)
  (cfg.sync_path('demo')).unlink()               # never aligned
  (cfg.sheet_music_dir / 'demo.musicxml').unlink()   # and no score to render either
  with running(cfg) as port:
    browser.open('http://127.0.0.1:%d/?piece=demo' % port, wait_for='document.body.innerText.length')
    # Poll for the line rather than sleeping a fixed second: the detail fetch is one round
    # trip, and under a full test run it has been slower than a second -- which failed this
    # test once, for no reason to do with what it is about.
    deadline = time.time() + 10
    line = ''
    while time.time() < deadline:
      line = browser.evaluate('document.getElementById("rangeline").textContent')
      if 'no measure map' in line:
        break
      time.sleep(0.2)
    assert browser.evaluate('document.getElementById("play").disabled') is True
    assert 'no measure map' in line, line


def test_a_piece_with_nothing_at_all_says_so(cfg, tmp_path, browser):
  make_piece(cfg, bars=6)
  (cfg.sync_path('demo')).unlink()
  (cfg.sheet_music_dir / 'demo.musicxml').unlink()
  cfg.audio_path('demo').unlink()                # no recording either
  cfg.audio_meta_path('demo').unlink()
  with running(cfg) as port:
    browser.open('http://127.0.0.1:%d/?piece=demo' % port, wait_for='document.body.innerText.length')
    # wait for the piece detail to arrive rather than sleeping a fixed second: under a full
    # test run the fetch can take longer, and this failed once for that reason alone
    deadline = time.time() + 10
    body = ''
    while time.time() < deadline:
      body = browser.evaluate('document.body.innerText')
      if 'nothing to play' in body:
        break
      time.sleep(0.2)
    assert browser.evaluate('document.getElementById("play").disabled') is True
    assert 'nothing to play' in body, body[:200]
    assert browser.evaluate('document.querySelectorAll(".chip").length') == 0


def test_a_piece_with_no_map_plays_its_score_instead(cfg, tmp_path, browser):
  """A recording that was never aligned is a dead end, but the score needs no map: its
  tracks carry their own bar grid, so the phone falls back to one of those."""
  make_piece(cfg, bars=6, seconds=12.0)
  (cfg.sync_path('demo')).unlink()
  with running(cfg) as port:
    browser.open('http://127.0.0.1:%d/?piece=demo' % port, wait_for='document.body.innerText.length')
    time.sleep(1.0)
    assert browser.evaluate('document.getElementById("play").disabled') is False, \
        'the score track can be played without a map'
    pressed = browser.evaluate(
        'Array.from(document.querySelectorAll(".chip"))'
        '.filter(c => c.getAttribute("aria-pressed") === "true").map(c => c.textContent)')
    assert pressed and 'Both hands' in pressed[0], pressed


def test_typing_in_the_filter_narrows_the_piece_list(cfg, tmp_path, browser):
  """The phone's search box queries the repository, not a widget list."""
  make_piece(cfg, slug='demo', bars=6, seconds=12.0, title='First Piece')
  make_piece(cfg, slug='other', bars=6, seconds=12.0, title='Second Piece')
  with running(cfg) as port:
    browser.open('http://127.0.0.1:%d/' % port)
    options = browser.evaluate(
        'Array.from(document.getElementById("piece").options).map(o => o.textContent)')
    assert len(options) == 2 and any('Second Piece' in o for o in options)

    browser.evaluate(
        'document.getElementById("search").value = "other";'
        'document.getElementById("search").dispatchEvent(new Event("input"));')
    time.sleep(1.2)                                  # the box debounces before asking
    options = browser.evaluate(
        'Array.from(document.getElementById("piece").options).map(o => o.value)')
    assert options == ['other'], options
    assert browser.evaluate('document.getElementById("piece").value') == 'other'
    # the client switched to the piece the filter found, not merely narrowed a list
    selected = browser.evaluate(
        'document.getElementById("piece").selectedOptions[0].textContent')
    assert 'Second Piece' in selected, selected

    browser.evaluate(
        'document.getElementById("search").value = "zzz-nothing";'
        'document.getElementById("search").dispatchEvent(new Event("input"));')
    time.sleep(1.2)
    assert browser.evaluate('document.getElementById("piece").options.length') == 1
    assert browser.evaluate('document.getElementById("play").disabled') is True


# ---------------------------------------------------------------------------
# practice tracks: left hand, right hand, or the whole piece
# ---------------------------------------------------------------------------

def test_three_hand_chips_appear_for_a_two_staff_piece(cfg, tmp_path, browser):
  """The controls the phone was missing: pick a hand, or both."""
  make_piece(cfg, bars=6, seconds=12.0, hands=True)
  with running(cfg) as port:
    browser.open('http://127.0.0.1:%d/?piece=demo' % port)
    labels = browser.evaluate(
        'Array.from(document.querySelectorAll(".chip")).map(c => c.textContent)')
    assert any('Left hand' in l for l in labels), labels
    assert any('Right hand' in l for l in labels), labels
    assert any('Both hands' in l for l in labels), labels
    # the recording is still there, so it can still be listened to
    assert any('Performance' in l for l in labels), labels


def test_a_single_staff_piece_offers_no_hands(cfg, tmp_path, browser):
  """Nothing is offered that the score cannot do."""
  make_piece(cfg, bars=6, seconds=12.0)          # one staff
  with running(cfg) as port:
    browser.open('http://127.0.0.1:%d/?piece=demo' % port)
    labels = browser.evaluate(
        'Array.from(document.querySelectorAll(".chip")).map(c => c.textContent)')
    assert any('Both hands' in l for l in labels), labels
    assert not any('Left hand' in l for l in labels), labels
    assert not any('Right hand' in l for l in labels), labels


def test_choosing_the_left_hand_asks_for_the_left_hand(cfg, tmp_path, browser):
  """The whole point: the request carries the track, and the server answers it."""
  make_piece(cfg, bars=6, seconds=12.0, hands=True)
  with running(cfg) as port:
    browser.open('http://127.0.0.1:%d/?piece=demo' % port)
    browser.evaluate(
        'Array.from(document.querySelectorAll(".chip"))'
        '.find(c => c.textContent.includes("Left hand")).click()')
    browser.evaluate('document.querySelectorAll(".bar")[2].click()')     # bar 3
    browser.evaluate('document.getElementById("play").click()')
    time.sleep(0.5)

    src = browser.evaluate('document.getElementById("player").src')
    assert 'track=left' in src, src
    assert 'from_bar=3' in src and 'to_bar=3' in src, src
    status, body = get(port, '/audio/demo?' + src.split('?', 1)[1])
    assert status == 200 and body[:4] == b'RIFF', body[:200]
    # and the choice is in the URL, so the phone keeps it on a refresh
    assert 'track=left' in browser.evaluate('location.search')


def test_the_chosen_hand_survives_a_piece_switch(cfg, tmp_path, browser):
  make_piece(cfg, slug='demo', bars=6, seconds=12.0, hands=True)
  make_piece(cfg, slug='other', bars=6, seconds=12.0, hands=True)
  with running(cfg) as port:
    browser.open('http://127.0.0.1:%d/?piece=demo' % port)
    browser.evaluate(
        'Array.from(document.querySelectorAll(".chip"))'
        '.find(c => c.textContent.includes("Right hand")).click()')
    browser.evaluate(
        'const s = document.getElementById("piece"); s.value = "other";'
        's.dispatchEvent(new Event("change"));')
    time.sleep(1.0)
    pressed = browser.evaluate(
        'Array.from(document.querySelectorAll(".chip"))'
        '.filter(c => c.getAttribute("aria-pressed") === "true").map(c => c.textContent)')
    assert pressed and 'Right hand' in pressed[0], pressed


def test_playing_with_nothing_tapped_plays_the_whole_track(cfg, tmp_path, browser):
  """"Whole piece playback" without tapping a bar first."""
  make_piece(cfg, bars=6, seconds=12.0, hands=True)
  with running(cfg) as port:
    browser.open('http://127.0.0.1:%d/?piece=demo' % port)
    assert browser.evaluate('document.getElementById("play").textContent').strip() == '▶ Whole piece'
    browser.evaluate('document.getElementById("play").click()')
    time.sleep(0.5)
    src = browser.evaluate('document.getElementById("player").src')
    assert 'track=' in src and 'from_bar' not in src, src
    status, body = get(port, '/audio/demo?' + src.split('?', 1)[1])
    assert status == 200 and body[:4] == b'RIFF'
    assert slices_duration(body) > 4.0, 'the whole piece, not a bar'
