"""HTTP transport tests.

The server is started on an ephemeral port in-process and driven with real HTTP
requests, so the parser, the routing, the turn wiring and the reply collection
are all exercised -- not just the handlers in isolation. No model, no ASR server
and no GPU are involved: the scripted tutor and the stub recogniser are enough.
"""

import asyncio
import json
import urllib.error
import urllib.request

import pytest

from tutor import cli, server as server_module
from tutor.audio.asr import StubAsr
from tutor.config import TutorConfig

NEEDS_BANK = True


class ServerHarness:
  def __init__(self, cfg, app, loop, server, port):
    self.cfg, self.app, self.loop, self.server, self.port = (
        cfg, app, loop, server, port)

  def request(self, path, method='GET', body=None, content_type=None):
    url = 'http://127.0.0.1:%d%s' % (self.port, path)
    headers = {}
    if content_type:
      headers['Content-Type'] = content_type
    request = urllib.request.Request(url, data=body, method=method,
                                     headers=headers)
    try:
      with urllib.request.urlopen(request, timeout=30) as response:
        return response.status, response.read()
    except urllib.error.HTTPError as exc:
      return exc.code, exc.read()

  def json(self, path, method='GET', body=None, content_type=None):
    payload = None
    if body is not None:
      payload = body if isinstance(body, bytes) else json.dumps(body).encode()
    status, raw = self.request(path, method, payload, content_type or (
        'application/json' if body is not None else None))
    try:
      return status, json.loads(raw)
    except ValueError:
      return status, {'raw': raw.decode('utf-8', 'replace')}

  async def aclose(self):
    """Must run on the server's own loop: wait_closed cannot be driven from
    another thread with run_until_complete."""
    self.server.close()
    await self.server.wait_closed()


class Harness:
  """Runs one asyncio loop in a thread and serves requests against it."""

  def __init__(self, cfg, asr=None, mode='converse'):
    import threading
    self.loop = asyncio.new_event_loop()
    self.ready = threading.Event()
    self.harness = None
    self.thread = threading.Thread(target=self._run, args=(cfg, asr, mode),
                                   daemon=True)
    self.thread.start()
    self.ready.wait(timeout=20)

  def _run(self, cfg, asr, mode):
    asyncio.set_event_loop(self.loop)

    async def boot():
      app = cli.build_app(cfg, mode, asr=asr)
      srv, port = await server_module.start(cfg, app, '127.0.0.1', 0)
      self.harness = ServerHarness(cfg, app, self.loop, srv, port)
      self.ready.set()
      return srv

    self.loop.run_until_complete(boot())
    self.loop.run_forever()

  def close(self):
    if self.harness:
      self.loop.call_soon_threadsafe(
          lambda: asyncio.ensure_future(self.harness.aclose()))
      import time
      for _ in range(50):
        if not self.harness.server.is_serving():
          break
        time.sleep(0.02)
    self.loop.call_soon_threadsafe(self.loop.stop)
    self.thread.join(timeout=5)


@pytest.fixture
def tutor(tmp_path):
  cfg = TutorConfig()
  cfg.asr = 'stub'
  cfg.tts = 'stub'
  cfg.log_path = str(tmp_path / 'server.jsonl')
  cfg.render_dir = str(tmp_path)
  harness = Harness(cfg)
  yield harness.harness
  harness.close()


# -- static + status --------------------------------------------------------

def test_health_reports_the_piece_and_wiring(tutor):
  status, payload = tutor.json('/health')
  assert status == 200 and payload['ok'] is True
  assert payload['piece']['slug']                       # the resolved bank
  assert payload['asr'] is None                          # stub means text-only


def test_client_page_is_served(tutor):
  status, raw = tutor.request('/')
  body = raw.decode('utf-8')
  assert status == 200
  assert 'Hold to talk' in body                          # the mic button
  assert 'getUserMedia' in body                          # real mic capture
  assert 'http://' not in body.split('<script')[0]        # no external assets


def test_state_exposes_mode_and_pending_question(tutor):
  status, payload = tutor.json('/state')
  assert status == 200
  assert payload['mode'] in ('converse', 'drill', 'teach', 'listen_coach',
                             'review')
  assert 'status' in payload and 'weak_spots' in payload


def test_unknown_endpoint_is_404(tutor):
  status, payload = tutor.json('/nope')
  assert status == 404 and 'no such endpoint' in payload['error']


def test_get_on_turn_is_rejected(tutor):
  status, payload = tutor.json('/turn')
  assert status == 405 and 'POST' in payload['error']


# -- turns ------------------------------------------------------------------

def test_start_greets_with_a_grounded_reply(tutor):
  status, payload = tutor.json('/start', method='POST', body={})
  assert status == 200
  assert payload['reply']
  assert payload['grounded'] is True
  assert payload['audio_wav_b64'] is None                # stub TTS: no audio


def test_typed_turn_reaches_the_tutor_and_returns_the_reply(tutor):
  tutor.json('/start', method='POST', body={})
  status, payload = tutor.json('/turn', method='POST',
                               body={'text': 'drill me'})
  assert status == 200
  assert payload['transcript'] == 'drill me'
  assert payload['mode'] == 'drill'                      # the mode switched
  assert payload['reply']                                # and a question came back
  assert payload['pending_question'] is not None
  assert payload['timings']['total_ms'] >= 0


def test_bar_question_is_answered_from_the_score(tutor):
  tutor.json('/start', method='POST', body={})
  bar = None
  status, state = tutor.json('/state')
  # Ask about a real bar in the loaded piece.
  from tutor.core.facts import FactStore
  facts = FactStore(tutor.cfg.fingerprint)
  bar = max(1, int(facts.global_facts.get('num_measures', 2)) // 2)
  status, payload = tutor.json('/turn', method='POST',
                               body={'text': 'what happens at bar %d?' % bar})
  assert status == 200
  assert payload['grounded'] is True
  assert any(call['name'] == 'get_bar' for call in payload['tools'])
  assert str(bar) in payload['reply']


def test_empty_text_is_skipped_not_answered(tutor):
  status, payload = tutor.json('/turn', method='POST', body={'text': '   '})
  assert payload['skipped'] == 'empty text'
  assert payload['reply'] == ''


def test_mode_can_be_set_from_the_client(tutor):
  status, payload = tutor.json('/turn', method='POST',
                               body={'text': 'teach me', 'mode': 'teach'})
  assert payload['mode'] == 'teach'


def test_audio_without_an_asr_is_refused_clearly(tutor):
  status, payload = tutor.json('/turn', method='POST', body=b'\x00' * 4096,
                               content_type='audio/webm')
  assert 'no ASR configured' in payload['error']


# -- audio path, with a stub recogniser ------------------------------------

@pytest.fixture
def voice_tutor(tmp_path):
  cfg = TutorConfig()
  cfg.asr = 'stub'
  cfg.tts = 'stub'
  cfg.log_path = str(tmp_path / 'server.jsonl')
  cfg.render_dir = str(tmp_path)
  asr = StubAsr(['what happens at bar 12'])
  harness = Harness(cfg, asr=asr)
  yield harness.harness, asr
  harness.close()


def test_uploaded_audio_is_transcribed_and_answered(voice_tutor):
  tutor, asr = voice_tutor
  tutor.json('/start', method='POST', body={})
  status, payload = tutor.json('/turn', method='POST', body=b'RIFFfakeaudio',
                               content_type='audio/webm;codecs=opus')
  assert status == 200
  assert payload['transcript'] == 'what happens at bar 12'
  assert asr.calls >= 1
  assert payload['reply']


def test_hallucinated_audio_is_dropped(voice_tutor):
  tutor, asr = voice_tutor
  asr.script = ['Bich, Bich, bich, bich, bich, bich, bich']
  status, payload = tutor.json('/turn', method='POST', body=b'RIFFnoise',
                               content_type='audio/webm')
  assert payload['skipped'] == 'not speech'
  assert payload['reply'] == ''                          # nothing was said to it


def test_filename_follows_the_container(voice_tutor):
  from tutor.server import _filename_for
  assert _filename_for('audio/webm;codecs=opus') == 'speech.webm'
  assert _filename_for('audio/mp4') == 'speech.m4a'      # Safari
  assert _filename_for('audio/ogg') == 'speech.ogg'
  assert _filename_for('audio/wav') == 'speech.wav'


# -- limits -----------------------------------------------------------------

def test_oversized_body_is_rejected(tutor, monkeypatch):
  monkeypatch.setattr(server_module, 'MAX_BODY', 64)
  status, payload = tutor.json('/turn', method='POST', body=b'x' * 500,
                               content_type='audio/webm')
  assert status == 413
  assert 'too large' in payload['error']


# -- backend outages --------------------------------------------------------

class FailingTts:
  """A TTS box that has gone away, which is a normal Tuesday."""

  counter = 0

  async def speak(self, text, cancel):
    raise RuntimeError('TTS server unreachable at http://192.168.2.174:8880')


def test_a_dead_tts_still_delivers_the_reply_as_text(tmp_path):
  """The lesson must survive a voice backend outage: the reply text still
  reaches the client, with a warning, instead of a 500 and no answer."""
  cfg = TutorConfig()
  cfg.asr, cfg.tts = 'stub', 'stub'
  cfg.log_path = str(tmp_path / 's.jsonl')
  cfg.render_dir = str(tmp_path)
  conductor, facts, session, tools, _verifier = cli.build(cfg, 'converse')
  conductor.tts = FailingTts()

  from tutor.server import TutorHttpApp
  app = TutorHttpApp(cfg, conductor, facts, session, tools, asr=None,
                     tts=conductor.tts)
  payload = asyncio.run(app.start())
  assert payload['reply'], payload                 # the text survived
  assert payload['audio_wav_b64'] is None          # obviously no audio
  assert any('speech unavailable' in w for w in payload['warnings']), payload
  # And the greeting is not consumed, so a later turn can greet properly.
  assert conductor.greeted is True


def test_dead_tts_does_not_lose_a_turn(tmp_path):
  cfg = TutorConfig()
  cfg.asr, cfg.tts = 'stub', 'stub'
  cfg.log_path = str(tmp_path / 's.jsonl')
  cfg.render_dir = str(tmp_path)
  conductor, facts, session, tools, _v = cli.build(cfg, 'drill')
  conductor.tts = FailingTts()
  from tutor.server import TutorHttpApp
  app = TutorHttpApp(cfg, conductor, facts, session, tools, asr=None,
                     tts=conductor.tts)
  asyncio.run(app.start())
  payload = asyncio.run(app.turn(_json_request({'text': 'what is the structure'})))
  assert payload['reply']
  assert [t['name'] for t in payload['tools']] == ['get_piece_map']
  assert payload['grounded'] is True


def _json_request(payload):
  from tutor.server import Request
  return Request(method='POST', path='/turn',
                 headers={'content-type': 'application/json'},
                 body=json.dumps(payload).encode())


# -- stale audio ------------------------------------------------------------

class WritingTts:
  """Stands in for a working TTS: writes a file and records it."""

  def __init__(self, out_dir):
    self.out_dir = out_dir
    self.counter = 0
    self.written = []

  async def speak(self, text, cancel):
    import os
    from tutor.audio.wav_io import pcm16_to_wav_bytes
    self.counter += 1
    path = os.path.join(self.out_dir, 'tts_%04d.wav' % self.counter)
    with open(path, 'wb') as fh:
      fh.write(pcm16_to_wav_bytes(b'\x00' * 3200, 16000))
    self.written.append(path)
    return True


def _app_with(cfg, tmp_path, tts, mode='converse'):
  conductor, facts, session, tools, _v = cli.build(cfg, mode)
  conductor.tts = tts
  from tutor.server import TutorHttpApp
  return TutorHttpApp(cfg, conductor, facts, session, tools, asr=None, tts=tts)


def _cfg(tmp_path):
  cfg = TutorConfig()
  cfg.asr, cfg.tts = 'stub', 'stub'
  cfg.log_path = str(tmp_path / 's.jsonl')
  cfg.render_dir = str(tmp_path)
  return cfg


def test_audio_from_a_previous_session_is_never_served(tmp_path):
  """Filenames restart with the process, so a leftover tts_0001.wav from an
  earlier run must not be mistaken for this turn's reply."""
  import os
  from tutor.audio.wav_io import pcm16_to_wav_bytes
  stale = os.path.join(str(tmp_path), 'tts_0001.wav')
  with open(stale, 'wb') as fh:
    fh.write(pcm16_to_wav_bytes(b'\x11' * 3200, 16000))

  cfg = _cfg(tmp_path)
  app = _app_with(cfg, tmp_path, FailingTts())          # writes nothing
  payload = asyncio.run(app.start())
  assert payload['audio_wav_b64'] is None, 'served stale audio'


def test_audio_written_during_the_turn_is_returned(tmp_path):
  cfg = _cfg(tmp_path)
  tts = WritingTts(str(tmp_path))
  app = _app_with(cfg, tmp_path, tts)
  payload = asyncio.run(app.start())
  assert payload['audio_wav_b64'], payload
  assert len(tts.written) >= 1

  # A second turn returns only its own audio, not the first turn's.
  first_written = len(tts.written)
  payload2 = asyncio.run(app.turn(_json_request({'text': 'what is the structure'})))
  assert payload2['audio_wav_b64']
  assert len(tts.written) > first_written


# -- TLS support routes -----------------------------------------------------

def test_ca_route_explains_itself_when_there_is_no_ca(tutor):
  status, payload = tutor.json('/ca.pem')
  assert status == 404
  assert '--make-cert' in payload['error']


def test_ca_route_serves_the_certificate_when_present(tmp_path):
  """The tablet fetches the CA over plain http, because https is not trusted
  yet -- that is the one step that cannot happen over TLS."""
  ca = tmp_path / 'ca.pem'
  ca.write_text('-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----\n')
  cfg = _cfg(tmp_path)
  conductor, facts, session, tools, _v = cli.build(cfg, 'converse')
  from tutor.server import TutorHttpApp
  app = TutorHttpApp(cfg, conductor, facts, session, tools, asr=None,
                     tts=conductor.tts, ca_path=str(ca))
  # handle() returns the raw HTTP response; parse the status line out of it.
  raw = asyncio.run(app.handle(_get_request('/ca.pem')))
  assert _status_of(raw) == 200
  assert b'BEGIN CERTIFICATE' in raw
  assert b'application/x-pem-file' in raw


def test_ca_route_is_404_when_the_path_is_missing(tmp_path):
  cfg = _cfg(tmp_path)
  conductor, facts, session, tools, _v = cli.build(cfg, 'converse')
  from tutor.server import TutorHttpApp
  app = TutorHttpApp(cfg, conductor, facts, session, tools, asr=None,
                     tts=conductor.tts, ca_path=str(tmp_path / 'nope.pem'))
  raw = asyncio.run(app.handle(_get_request('/ca.pem')))
  assert _status_of(raw) == 404
  assert b'--make-cert' in raw


def _status_of(raw: bytes) -> int:
  return int(raw.split(b'\r\n', 1)[0].split(b' ')[1])


def _get_request(path):
  from tutor.server import Request
  return Request(method='GET', path=path, headers={}, body=b'')


# -- pieces, map and progress routes ---------------------------------------

def _two_piece_world(tmp_path, monkeypatch):
  """Two banks in an isolated pieces dir, so switching can be exercised."""
  from tutor import pieces as pieces_module
  root = tmp_path / 'output'
  pdir = root / 'pieces'
  pdir.mkdir(parents=True)
  monkeypatch.setattr(pieces_module, 'OUTPUT_DIR', str(root))
  monkeypatch.setattr(pieces_module, 'PIECES_DIR', str(pdir))
  monkeypatch.setattr(pieces_module, 'INDEX_PATH', str(pdir / 'index.json'))
  monkeypatch.setattr(pieces_module, 'LEGACY_FINGERPRINT',
                      str(root / 'fingerprint.json'))
  return pdir


def _write_bank(pdir, slug, bars, key='A minor', questions=2, mtime=None):
  import os
  directory = pdir / slug
  directory.mkdir(parents=True, exist_ok=True)
  def measure(i):
    return {'measure': i, 'chord': 'Am', 'melody': 'A4', 'notes_count': 4,
            'rhythm': 'Q Q E E', 'contour': 'up', 'avg_velocity': 64.0,
            'pitches': ['A4', 'E4']}
  raw = {
      'source_file': '/scores/%s.mxl' % slug,
      'grid_source': 'score-measures', 'section_basis': 'key-signatures',
      'repeat_basis': 'score-content', 'climax_measure': bars // 2,
      'global': {'title': slug.title(), 'num_measures': bars,
                 'time_signatures_declared': ['3/4'], 'key_inferred': key,
                 'key_from_score': key, 'lowest_note': 'E2', 'highest_note': 'A5',
                 'total_time_s': 30.0, 'total_notes': bars * 4},
      'sections': [{'label': 'A', 'key': key, 'start_measure': 1,
                    'end_measure': bars, 'measures': bars,
                    'signature_in_score': key}],
      'repetitions': [],
      'measures': [measure(i) for i in range(1, bars + 1)],
      'questions': [{'id': 'q%d' % i, 'tier': 1, 'type': 'chord',
                     'prompt': 'What is the chord in measure %d?' % (i + 1),
                     'answer': 'Am'} for i in range(questions)],
  }
  path = directory / 'fingerprint.json'
  path.write_text(json.dumps(raw))
  if mtime:
    os.utime(path, (mtime, mtime))
  return path


@pytest.fixture
def two_pieces(tmp_path, monkeypatch):
  pdir = _two_piece_world(tmp_path, monkeypatch)
  _write_bank(pdir, 'older-piece', bars=4, mtime=1000)
  _write_bank(pdir, 'newer-piece', bars=12, key='B minor', mtime=2000)
  cfg = TutorConfig()
  cfg.asr, cfg.tts = 'stub', 'stub'
  cfg.log_path = str(tmp_path / 'pieces.jsonl')
  cfg.render_dir = str(tmp_path)
  harness = Harness(cfg)
  yield harness.harness
  harness.close()


def test_pieces_route_lists_banks_with_the_active_one(two_pieces):
  status, payload = two_pieces.json('/pieces')
  assert status == 200
  slugs = [p['slug'] for p in payload['pieces']]
  assert slugs == ['newer-piece', 'older-piece']       # newest first
  assert payload['active'] == 'newer-piece'
  assert [p['active'] for p in payload['pieces']] == [True, False]
  assert payload['pieces'][0]['bars'] == 12


def test_map_route_returns_the_piece_anatomy(two_pieces):
  status, payload = two_pieces.json('/map')
  assert status == 200
  assert payload['piece']['bars'] == 12
  assert len(payload['measures']) == 12
  assert payload['trust']['verified'] is True
  assert payload['questions']['total'] == 2
  assert payload['unique']['count'] == 12              # no repeats in this bank


def test_progress_route_reports_shapes(two_pieces):
  status, payload = two_pieces.json('/progress')
  assert status == 200
  assert payload['piece']['slug'] == 'newer-piece'
  assert 'totals' in payload['history'] and 'by_type' in payload['history']
  assert payload['current_run']['status']['attempts'] == 0
  assert isinstance(payload['pieces'], list)


def test_switching_piece_rebuilds_the_lesson(two_pieces):
  status, before = two_pieces.json('/state')
  assert before['piece']['bars'] == 12                 # the newest bank

  status, payload = two_pieces.json('/piece', method='POST',
                                    body={'slug': 'older-piece'})
  assert status == 200 and payload['ok'] is True
  assert payload['piece']['slug'] == 'older-piece'
  assert payload['piece']['bars'] == 4                 # a different bank loaded

  # The views follow the switch, and the new piece has no pending question.
  status, state = two_pieces.json('/state')
  assert state['piece']['slug'] == 'older-piece'
  assert state['pending_question'] is None
  status, mapping = two_pieces.json('/map')
  assert mapping['piece']['bars'] == 4


def test_switching_keeps_the_mode(two_pieces):
  two_pieces.json('/turn', method='POST', body={'text': 'drill me'})
  status, state = two_pieces.json('/state')
  assert state['mode'] == 'drill'
  two_pieces.json('/piece', method='POST', body={'slug': 'older-piece'})
  status, state = two_pieces.json('/state')
  assert state['mode'] == 'drill'                      # still drilling


def test_switching_to_the_active_piece_is_a_noop(two_pieces):
  status, payload = two_pieces.json('/piece', method='POST',
                                    body={'slug': 'newer-piece'})
  assert status == 200 and payload['already'] is True
  assert payload['piece']['slug'] == 'newer-piece'


def test_switching_to_an_unknown_piece_fails_loudly(two_pieces):
  status, payload = two_pieces.json('/piece', method='POST',
                                    body={'slug': 'no-such-piece'})
  assert status == 404
  assert 'no-such-piece' in payload['error']
  assert sorted(payload['available']) == ['newer-piece', 'older-piece']


def test_switching_with_no_slug_is_a_bad_request(two_pieces):
  status, payload = two_pieces.json('/piece', method='POST', body={})
  assert status == 400 and 'no slug' in payload['error']


def test_switching_must_be_a_post(two_pieces):
  status, payload = two_pieces.json('/piece')
  assert status == 405


def test_the_client_page_carries_the_new_tabs(two_pieces):
  status, raw = two_pieces.request('/')
  body = raw.decode('utf-8')
  for needle in ('data-tab="practice"', 'data-tab="map"', 'data-tab="progress"',
                 'id="piece-select"', 'id="map-table"', 'id="prog-types"'):
    assert needle in body, needle
