"""Offline tests for `analyzer.ytaudio`.

The suite must never touch YouTube. `yt_dlp.YoutubeDL` is replaced by a scripted
double that writes a real 2-second WAV with soundfile, so the ffmpeg conversion,
the ffprobe probes, the cache and the error messages are all exercised for real
while the network stays out of it. One test is marked `network` and skipped
unless ANALYZER_NETWORK_TESTS=1, so the live path is documented but not required.
"""

import datetime
import json
import os
import pathlib

import numpy as np
import pytest
import soundfile as sf
from yt_dlp.utils import DownloadError as YtDlpError

from analyzer import ytaudio
from analyzer.config import AnalyzerConfig
from analyzer.ytaudio import DownloadError

URL = 'https://www.youtube.com/watch?v=VID123'
SLUG = 'chopin-op34-2'

# What yt-dlp raises for the failures a user actually hits.
FAILURES = {
    'network': 'ERROR: Unable to download webpage: <urlopen error [Errno 101] '
               'network is unreachable>',
    'private': 'ERROR: Private video. Sign in if you have been granted access',
    'removed': 'ERROR: Video unavailable',
    'unsupported': 'ERROR: Unsupported URL: https://example.com/notavideo',
}


@pytest.fixture
def cfg(tmp_path):
  """An AnalyzerConfig whose every path is inside tmp_path."""
  return AnalyzerConfig(cache_dir=tmp_path / 'cache', output_dir=tmp_path / 'output',
                        bank_dir=tmp_path / 'bank',
                        legacy_bank=tmp_path / 'legacy.json',
                        sheet_music_dir=tmp_path / 'sheets')


def make_wav(path, seconds=2.0, rate=44100, channels=2):
  """Write a real WAV, so ffprobe and ffmpeg have something to chew on."""
  path = pathlib.Path(path)
  path.parent.mkdir(parents=True, exist_ok=True)
  tone = (0.3 * np.sin(2 * np.pi * 440.0 * np.arange(int(rate * seconds)) / rate)).astype('float32')
  data = np.repeat(tone[:, None], channels, axis=1) if channels > 1 else tone
  sf.write(str(path), data, rate, subtype='PCM_16')
  return path


def scratch(cfg):
  return cfg.cache_dir / 'downloads'


def leftovers(cfg):
  return sorted(p.name for p in scratch(cfg).iterdir()) if scratch(cfg).exists() else []


class _Double:
  """A scripted stand-in for `yt_dlp.YoutubeDL`.

  It writes a real WAV where the module told it to, calls the progress hooks the
  way yt-dlp does, and can be switched to any of the failure modes in FAILURES.
  """

  def __init__(self, opts, plan):
    self.opts = opts
    self.plan = plan

  def __enter__(self):
    return self

  def __exit__(self, *exc_info):
    return False

  def extract_info(self, url, download=True):
    plan, opts = self.plan, self.opts
    plan['urls'].append(url)
    if url.startswith('ytsearch'):
      plan['searches'].append(url)
      if plan['behavior'] == 'search_fail':
        raise YtDlpError('ERROR: Unable to download API page: network is unreachable')
      return {'entries': plan.get('entries', [])}
    plan['downloads'] += 1
    if plan['behavior'] in FAILURES:
      raise YtDlpError(FAILURES[plan['behavior']])
    if plan['behavior'] == 'nofile':
      return {'id': 'VID123', 'title': 'nothing came out'}
    raw = pathlib.Path(opts['outtmpl'].replace('%(ext)s', 'wav'))
    if plan['behavior'] == 'empty':
      raw.parent.mkdir(parents=True, exist_ok=True)
      raw.write_bytes(b'')
    else:
      make_wav(raw, seconds=plan['seconds'], rate=plan['rate'],
               channels=plan['channels'])
    for hook in opts.get('progress_hooks') or []:
      hook({'status': 'downloading', 'downloaded_bytes': 512, 'total_bytes': 1024})
      hook({'status': 'finished', 'filename': str(raw)})
    return {'id': plan['video_id'], 'title': plan['title'],
            'duration': plan['seconds'],
            'requested_downloads': [{'filepath': str(raw)}]}


@pytest.fixture
def fake(monkeypatch):
  """Scripted yt-dlp double; mutate the returned dict to change its behaviour."""
  plan = {'behavior': 'ok', 'video_id': 'VID123', 'title': 'Chopin: Waltz Op. 34 No. 2',
          'seconds': 2.0, 'rate': 44100, 'channels': 2,
          'urls': [], 'downloads': 0, 'searches': [], 'options': []}

  def install(opts):
    plan['options'].append(opts)
    return _Double(opts, plan)

  monkeypatch.setattr(ytaudio.yt_dlp, 'YoutubeDL', install)
  return plan


# -- a successful download ---------------------------------------------------

def test_download_writes_a_mono_wav_and_its_metadata(cfg, fake):
  source = ytaudio.download_audio(cfg, SLUG, URL)

  wav = cfg.audio_path(SLUG)
  assert source.path == str(wav) and wav.is_file()
  assert (source.slug, source.url, source.source_id) == (SLUG, URL, 'VID123')
  assert source.title == 'Chopin: Waltz Op. 34 No. 2'
  assert source.duration_s == pytest.approx(2.0, abs=0.05)
  assert (source.sample_rate, source.channels) == (22050, 1)
  # The cached file really is mono/22050/16-bit, whatever came off the wire.
  assert ytaudio.probe_stream(wav) == (22050, 1)
  assert ytaudio.probe_duration(wav) == pytest.approx(2.0, abs=0.05)
  assert sf.info(str(wav)).subtype == 'PCM_16'

  meta = json.loads(cfg.audio_meta_path(SLUG).read_text())
  assert meta == source.to_dict()
  assert meta['fetched_at'] == source.fetched_at
  datetime.datetime.strptime(source.fetched_at, '%Y-%m-%dT%H:%M:%SZ')
  assert ytaudio.is_cached(cfg, SLUG)

  assert fake['downloads'] == 1
  assert leftovers(cfg) == []          # the raw download was cleaned up


def test_yt_dlp_is_configured_for_one_audio_stream_into_the_cache(cfg, fake):
  ytaudio.download_audio(cfg, SLUG, URL)

  opts = fake['options'][0]
  assert opts['format'] == 'bestaudio/best'
  assert opts['noplaylist'] is True
  assert opts['quiet'] is True and opts['no_warnings'] is True
  assert opts['outtmpl'].startswith(str(scratch(cfg)))
  assert opts['progress_hooks']
  assert fake['urls'] == [URL]


def test_the_cache_is_honoured_without_a_second_download(cfg, fake):
  first = ytaudio.download_audio(cfg, SLUG, URL)
  calls = list(fake['urls'])
  second = ytaudio.download_audio(cfg, SLUG, URL)

  assert second == first
  assert fake['urls'] == calls and fake['downloads'] == 1


def test_the_cache_is_keyed_by_slug_not_url(cfg, fake):
  first = ytaudio.download_audio(cfg, SLUG, URL)
  other = ytaudio.download_audio(cfg, SLUG, 'https://youtu.be/SOMETHINGELSE')

  assert other == first and fake['downloads'] == 1


def test_force_downloads_again(cfg, fake):
  first = ytaudio.download_audio(cfg, SLUG, URL)
  fake['video_id'] = 'VID999'
  second = ytaudio.download_audio(cfg, SLUG, URL, force=True)

  assert fake['downloads'] == 2
  assert first.source_id == 'VID123' and second.source_id == 'VID999'


def test_fetch_is_an_alias_for_download_audio(cfg, fake):
  source = ytaudio.fetch(cfg, SLUG, URL)

  assert source.slug == SLUG and ytaudio.is_cached(cfg, SLUG)
  assert fake['downloads'] == 1


def test_delete_cached_removes_the_wav_and_the_json(cfg, fake):
  ytaudio.download_audio(cfg, SLUG, URL)

  assert ytaudio.delete_cached(cfg, SLUG) is True
  assert not cfg.audio_path(SLUG).exists()
  assert not cfg.audio_meta_path(SLUG).exists()
  assert ytaudio.delete_cached(cfg, SLUG) is False


def test_delete_cached_also_clears_a_stale_partial(cfg):
  cfg.ensure_dirs()
  scratch(cfg).mkdir(parents=True, exist_ok=True)
  (scratch(cfg) / (SLUG + '.webm.part')).write_bytes(b'junk')

  assert ytaudio.delete_cached(cfg, SLUG) is True
  assert leftovers(cfg) == []


# -- progress ----------------------------------------------------------------

def test_progress_is_reported_in_range_and_in_order(cfg, fake):
  seen = []
  ytaudio.download_audio(cfg, SLUG, URL, progress=lambda p, m: seen.append((p, m)))

  assert seen, 'the progress callback was never called'
  percents = [p for p, _ in seen]
  assert all(0.0 <= p <= 100.0 for p in percents)
  assert percents == sorted(percents)          # a bar that reverses is a bug
  assert percents[0] == 0.0 and percents[-1] == 100.0
  assert any(m == 'converting' for _, m in seen)
  assert any(m == 'downloading' for _, m in seen)


def test_a_broken_progress_callback_cannot_abort_the_download(cfg, fake):
  def rude(percent, message):
    raise RuntimeError('the window went away')

  source = ytaudio.download_audio(cfg, SLUG, URL, progress=rude)

  assert source.duration_s > 0 and ytaudio.is_cached(cfg, SLUG)
  assert leftovers(cfg) == []


# -- failures ----------------------------------------------------------------

@pytest.mark.parametrize('behavior,needle', [
    ('network', 'network'),
    ('private', 'private'),
    ('removed', 'unavailable'),
    ('unsupported', 'link'),
])
def test_yt_dlp_failures_become_actionable_download_errors(cfg, fake, behavior, needle):
  fake['behavior'] = behavior

  with pytest.raises(DownloadError) as err:
    ytaudio.download_audio(cfg, SLUG, URL)

  assert needle in str(err.value).lower()
  assert not isinstance(err.value, YtDlpError)     # never leak yt-dlp's own type
  assert isinstance(err.value.__cause__, YtDlpError)   # ...but keep it chained
  assert not ytaudio.is_cached(cfg, SLUG)
  assert not cfg.audio_path(SLUG).exists()
  assert leftovers(cfg) == []                  # nothing half-downloaded left behind


def test_a_zero_byte_download_is_an_error(cfg, fake):
  fake['behavior'] = 'empty'

  with pytest.raises(DownloadError) as err:
    ytaudio.download_audio(cfg, SLUG, URL)

  message = str(err.value).lower()
  assert 'empty' in message and '0 bytes' in message
  assert not ytaudio.is_cached(cfg, SLUG)
  assert leftovers(cfg) == []


def test_a_download_that_writes_no_file_is_reported(cfg, fake):
  fake['behavior'] = 'nofile'

  with pytest.raises(DownloadError, match='no file'):
    ytaudio.download_audio(cfg, SLUG, URL)

  assert leftovers(cfg) == []


def test_a_missing_ffmpeg_is_reported_not_crashed(cfg, fake, monkeypatch):
  monkeypatch.setattr(ytaudio.shutil, 'which', lambda name: None)

  with pytest.raises(DownloadError, match='ffmpeg'):
    ytaudio.download_audio(cfg, SLUG, URL)

  assert not ytaudio.is_cached(cfg, SLUG)
  assert leftovers(cfg) == []


def test_an_empty_link_is_refused_before_any_download(cfg, fake):
  with pytest.raises(DownloadError, match='link'):
    ytaudio.download_audio(cfg, SLUG, '   ')

  assert fake['downloads'] == 0 and fake['options'] == []


def test_a_missing_yt_dlp_is_reported(cfg, monkeypatch):
  monkeypatch.setattr(ytaudio, 'yt_dlp', None)

  with pytest.raises(DownloadError, match='not installed'):
    ytaudio.download_audio(cfg, SLUG, URL)


def test_an_ancient_yt_dlp_is_reported(cfg, monkeypatch):
  class Ancient:
    __version__ = '2021.01.01'

  monkeypatch.setattr(ytaudio.yt_dlp, 'version', Ancient)

  with pytest.raises(DownloadError, match='too old'):
    ytaudio.download_audio(cfg, SLUG, URL)


# -- probing -----------------------------------------------------------------

def test_probes_measure_a_generated_wav(tmp_path):
  wav = make_wav(tmp_path / 'tone.wav', seconds=2.0, rate=44100, channels=2)

  assert ytaudio.probe_duration(wav) == pytest.approx(2.0, abs=0.05)
  assert ytaudio.probe_stream(wav) == (44100, 2)


def test_probes_reject_a_file_that_is_not_audio(tmp_path):
  junk = tmp_path / 'notes.txt'
  junk.write_text('this is not audio')

  with pytest.raises(DownloadError, match='ffprobe'):
    ytaudio.probe_duration(junk)
  with pytest.raises(DownloadError, match='ffprobe'):
    ytaudio.probe_stream(junk)
  with pytest.raises(DownloadError, match='ffprobe'):
    ytaudio.probe_duration(tmp_path / 'missing.wav')


def test_a_missing_ffprobe_is_reported_not_crashed(tmp_path, monkeypatch):
  wav = make_wav(tmp_path / 'tone.wav', seconds=0.5)
  monkeypatch.setattr(ytaudio.shutil, 'which', lambda name: None)

  with pytest.raises(DownloadError, match='ffprobe'):
    ytaudio.probe_duration(wav)


def test_an_unreadable_wav_does_not_stop_load_cached(cfg):
  cfg.ensure_dirs()
  wav = cfg.audio_path(SLUG)
  wav.write_bytes(b'RIFF' + b'\x00' * 200)      # big enough to look cached, not audio
  cfg.audio_meta_path(SLUG).write_text(json.dumps({'title': 'Junk', 'source_id': 'X'}))

  source = ytaudio.load_cached(cfg, SLUG)

  assert source is not None
  assert (source.title, source.source_id) == ('Junk', 'X')
  assert (source.duration_s, source.sample_rate, source.channels) == (0.0, 0, 0)


# -- the cache on disk -------------------------------------------------------

def test_is_cached_needs_both_the_wav_and_the_json(cfg):
  assert ytaudio.is_cached(cfg, SLUG) is False
  cfg.ensure_dirs()
  make_wav(cfg.audio_path(SLUG))
  assert ytaudio.is_cached(cfg, SLUG) is False          # no metadata yet

  cfg.audio_meta_path(SLUG).write_text('{}')
  assert ytaudio.is_cached(cfg, SLUG) is True


def test_an_empty_wav_is_not_a_cache(cfg):
  cfg.ensure_dirs()
  cfg.audio_path(SLUG).write_bytes(b'')
  cfg.audio_meta_path(SLUG).write_text('{}')

  assert ytaudio.is_cached(cfg, SLUG) is False
  assert ytaudio.load_cached(cfg, SLUG) is None


def test_load_cached_rebuilds_what_it_can_from_the_wav(cfg, fake):
  ytaudio.download_audio(cfg, SLUG, URL)
  cfg.audio_meta_path(SLUG).write_text('{"slug": "chopin-op34-2", "title": "trunc')

  source = ytaudio.load_cached(cfg, SLUG)

  assert source is not None
  assert (source.slug, source.path) == (SLUG, str(cfg.audio_path(SLUG)))
  assert source.duration_s == pytest.approx(2.0, abs=0.05)
  assert (source.sample_rate, source.channels) == (22050, 1)
  assert (source.title, source.source_id, source.url) == ('', '', '')


def test_load_cached_survives_a_sidecar_full_of_rubbish(cfg):
  cfg.ensure_dirs()
  make_wav(cfg.audio_path(SLUG), seconds=1.0, rate=22050, channels=1)
  # JSON permits NaN, and yt-dlp metadata can be missing: neither may raise.
  cfg.audio_meta_path(SLUG).write_text(
      '{"sample_rate": NaN, "channels": "two", "duration_s": "ages", "title": 7}')

  source = ytaudio.load_cached(cfg, SLUG)

  assert source.duration_s == pytest.approx(1.0, abs=0.05)
  assert (source.sample_rate, source.channels) == (22050, 1)
  assert source.title == '7'


def test_load_cached_is_none_when_the_wav_is_gone(cfg):
  cfg.ensure_dirs()
  cfg.audio_meta_path(SLUG).write_text('{"title": "nothing here"}')

  assert ytaudio.load_cached(cfg, SLUG) is None


def test_load_cached_trusts_the_disk_over_a_stale_json(cfg):
  cfg.ensure_dirs()
  make_wav(cfg.audio_path(SLUG), seconds=1.0, rate=22050, channels=1)
  cfg.audio_meta_path(SLUG).write_text(json.dumps({
      'slug': 'some-other-piece', 'path': '/nowhere.wav', 'url': URL,
      'source_id': 'VID123', 'title': 'Etude Op. 10 No. 4',
      'duration_s': 0.0, 'sample_rate': 0, 'channels': 0,
      'fetched_at': '2026-01-01T00:00:00Z'}))

  source = ytaudio.load_cached(cfg, SLUG)

  assert (source.slug, source.path) == (SLUG, str(cfg.audio_path(SLUG)))
  assert (source.source_id, source.title) == ('VID123', 'Etude Op. 10 No. 4')
  assert source.duration_s == pytest.approx(1.0, abs=0.05)
  assert (source.sample_rate, source.channels) == (22050, 1)


# -- searching ---------------------------------------------------------------

def test_search_shapes_the_rows_the_gui_needs(fake):
  fake['entries'] = [
      {'id': 'abc', 'title': 'Chopin Op. 34 No. 2', 'duration': 512.4,
       'uploader': 'Some Pianist'},
      None,                                  # yt-dlp can emit these
      {'id': 'xyz', 'title': 'Other', 'duration': None},
  ]

  rows = ytaudio.search_youtube('chopin op 34 no 2', limit=5)

  assert rows[0] == {'id': 'abc', 'title': 'Chopin Op. 34 No. 2',
                     'duration_s': 512.4, 'uploader': 'Some Pianist'}
  assert [r['id'] for r in rows] == ['abc', 'xyz']
  assert rows[1]['duration_s'] == 0.0 and rows[1]['uploader'] == ''
  assert fake['searches'] == ['ytsearch5:chopin op 34 no 2']
  opts = fake['options'][-1]
  assert opts['extract_flat'] is True and opts['skip_download'] is True


def test_search_honours_the_limit(fake):
  fake['entries'] = [{'id': 'v%d' % i, 'title': 't%d' % i} for i in range(9)]

  assert [r['id'] for r in ytaudio.search_youtube('anything', limit=2)] == ['v0', 'v1']
  assert fake['searches'] == ['ytsearch2:anything']


def test_search_with_no_results_is_empty(fake):
  fake['entries'] = []

  assert ytaudio.search_youtube('nothing at all') == []


def test_search_without_a_query_never_calls_yt_dlp(fake):
  assert ytaudio.search_youtube('   ') == []
  assert ytaudio.search_youtube('anything', limit=0) == []
  assert fake['searches'] == [] and fake['options'] == []


def test_search_failure_raises_download_error(fake):
  fake['behavior'] = 'search_fail'

  with pytest.raises(DownloadError) as err:
    ytaudio.search_youtube('chopin op 34 no 2')

  assert 'network' in str(err.value).lower()


# -- the live path, opt-in only ----------------------------------------------

@pytest.mark.network
@pytest.mark.skipif(os.environ.get('ANALYZER_NETWORK_TESTS') != '1',
                    reason='set ANALYZER_NETWORK_TESTS=1 to search YouTube for real')
def test_live_search_finds_a_chopin_recording():
  rows = ytaudio.search_youtube('chopin op 34 no 2', limit=3)

  assert rows, 'YouTube search returned nothing'
  for row in rows:
    assert set(row) == {'id', 'title', 'duration_s', 'uploader'}
    assert row['id'] and row['title']
