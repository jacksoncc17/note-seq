"""The tools the pipeline shells out to, and the one that goes stale.

`yt-dlp` is the fragile dependency: YouTube changes how it serves video every few weeks, the
package is patched within days, and a version that has drifted fails with something
cryptic in a job log. Nothing here can stop that, so two smaller things are tested instead:
the failure *names* the likely cause when it is a stale version, and `manage.py --tools`
reports what is installed and how to update it.

No network, no music: `yt_dlp.YoutubeDL` is replaced, and the tool report only runs
`--version` on things already on PATH.
"""

from __future__ import annotations

import pytest

from service.app.config import Settings


def settings_for(path):
  made = Settings(data_dir=path)
  made.embedded_worker = False
  made.ensure_dirs()
  return made


# --------------------------------------------------------------------------- the hint

def test_a_stale_version_is_named_rather_than_left_cryptic():
  from service.app.pipeline import _stale_ytdlp_hint

  for message in ('ERROR: Unable to extract player response',
                  'ERROR: Sign in to confirm your age',
                  'HTTP Error 403: Forbidden',
                  'ERROR: nsig extraction failed',
                  'Requested format is not available'):
    hint = _stale_ytdlp_hint(Exception(message))
    assert 'may be out of date' in hint, message
    assert '-m pip install -U yt-dlp' in hint
    assert '.venv/bin/python' in hint or 'python' in hint


def test_an_unavailable_video_is_not_blamed_on_the_version():
  """The distinction matters: sending somebody to upgrade their tools over a deleted video
  wastes the time they were going to spend on the actual problem."""
  from service.app.pipeline import _stale_ytdlp_hint

  for message in ('ERROR: [youtube] zzzzzzzzzzz: This video is unavailable',
                  'ERROR: Private video. Sign in if you have been granted access',
                  'ERROR: This video is not available in your country'):
    assert _stale_ytdlp_hint(Exception(message)) == '', message


def test_the_fetch_failure_carries_the_hint(tmp_path, monkeypatch):
  """The wiring, not just the helper: a job's log is where somebody will read this."""
  import service.app.pipeline as pipeline

  class Broken:
    def __init__(self, *_args, **_kwargs):
      pass
    def __enter__(self):
      return self
    def __exit__(self, *_exc):
      return False
    def extract_info(self, *_args, **_kwargs):
      raise RuntimeError('ERROR: Unable to extract player response')

  import yt_dlp
  monkeypatch.setattr(yt_dlp, 'YoutubeDL', Broken)

  settings = settings_for(tmp_path / 'data')
  with pytest.raises(pipeline.PipelineError) as caught:
    pipeline.fetch_youtube(settings, tmp_path / 'job', 'https://example.com/v',
                           lambda *a: None, lambda _m: None)
  assert 'could not fetch that YouTube URL' in str(caught.value)
  assert 'may be out of date' in str(caught.value)


# --------------------------------------------------------------------------- --tools

def test_the_tool_report_names_the_versions_and_the_update(tmp_path, capsys):
  from service.app.manage import main

  assert main(['--tools', '--data', str(tmp_path / 'data')]) == 0
  printed = capsys.readouterr().out
  assert 'python' in printed and 'ffmpeg' in printed and 'yt-dlp' in printed
  assert '-m pip install -U yt-dlp' in printed
  assert 'analyzer  present' in printed or 'analyzer  MISSING' in printed


def test_the_tool_report_is_honest_about_a_missing_tool(tmp_path, capsys, monkeypatch):
  """A report that says "fine" because it could not look would be worse than none.

  Simulated by making the lookups return nothing, which is what `shutil.which` does on a
  machine without them -- deleting PATH is not the same thing, because `shutil.which` falls
  back to `confstr('CS_PATH')` and finds `/usr/bin/ffmpeg` anyway.
  """
  from service.app.config import Settings
  from service.app.manage import main

  monkeypatch.setattr(Settings, 'ffmpeg', property(lambda self: None))
  monkeypatch.setattr(Settings, 'ffprobe', property(lambda self: None))
  assert main(['--tools', '--data', str(tmp_path / 'data')]) == 0
  printed = capsys.readouterr().out
  assert 'ffmpeg    MISSING' in printed and 'ffprobe   MISSING' in printed
