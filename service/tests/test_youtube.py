"""The path that needs the network: a YouTube URL instead of an uploaded file.

Off by default, because it downloads a few megabytes and depends on YouTube being
reachable and on a search returning the right piece -- `ANALYZER_NETWORK_TESTS=1` turns
it on, the same switch the analyzer's own network tests use.

What it asserts is the *plumbing*: the URL is fetched, converted and analysed, and the
piece it produces is a real one. Whether the search found a good performance is not
something a test should have an opinion about, so the title it actually got is what gets
reported.
"""

from __future__ import annotations

import os

import pytest

network = pytest.mark.skipif(
    os.environ.get('ANALYZER_NETWORK_TESTS') != '1',
    reason='set ANALYZER_NETWORK_TESTS=1 to download from YouTube')


@network
def test_a_youtube_url_is_fetched_converted_and_analysed(client, settings):
  """`ytsearch1:` is a yt-dlp URL like any other: a search that resolves to one video."""
  from service.app.worker import Worker

  status = client.get('/api/v1/status').json()
  if not status['capabilities']['yt_dlp']:
    pytest.skip('yt-dlp is not installed')

  response = client.post(
      '/api/v1/jobs',
      files={'score': ('chopin.mxl', open('/home/wei/sheet_music/'
                                          'chopin-waltz-in-a-minor-op-34-no-2.mxl', 'rb'),
                       'application/octet-stream')},
      data={'youtube_url': 'ytsearch1:Chopin Waltz in A minor Op 34 No 2 piano',
            'title': 'Chopin from YouTube'})
  assert response.status_code == 201, response.text
  job = response.json()
  assert client.get('/api/v1/jobs/%s' % job['id']).json()['params']['youtube_url']

  assert Worker(settings).run_once()
  detail = client.get('/api/v1/jobs/%s' % job['id']).json()
  assert detail['status'] == 'done', '%s\n%s' % (detail.get('error'), detail.get('log'))

  piece = client.get('/api/v1/pieces/%s' % detail['slug']).json()
  print('fetched: %s' % piece['meta']['audio'].get('name'))
  print('log:\n%s' % detail['log'])
  assert piece['meta']['audio']['source'] == 'youtube'
  assert piece['meta']['audio']['url'].startswith('ytsearch1:')
  assert piece['meta']['duration_s'] > 60, 'a real recording, not a stub'
  assert len(piece['geometry']['pages']) >= 1
  assert piece['map']['method'] == 'dtw'
  assert len(piece['map']['measures']) > 100


@network
def test_a_youtube_url_that_does_not_resolve_fails_with_a_message(client, settings):
  from service.app.worker import Worker

  response = client.post(
      '/api/v1/jobs',
      files={'score': ('chopin.mxl', open('/home/wei/sheet_music/'
                                          'chopin-waltz-in-a-minor-op-34-no-2.mxl', 'rb'),
                       'application/octet-stream')},
      data={'youtube_url': 'https://www.youtube.com/watch?v=zzzzzzzzzzz'})
  assert response.status_code == 201
  assert Worker(settings).run_once()
  detail = client.get('/api/v1/jobs/%s' % response.json()['id']).json()
  assert detail['status'] == 'failed'
  assert 'could not fetch that YouTube URL' in detail['error']
  assert client.get('/api/v1/pieces').json()['pieces'] == []
