"""Shared test setup.

The question bank is a *derived* artifact -- built from a score by
`memorization/analyze.py` -- so it is not committed. Tests that need one resolve
whichever bank exists on disk (per-piece layout or the older single-file path),
and skip with instructions when none does, rather than failing on a fresh clone.
"""

import dataclasses

import pytest


def _needs_bank(request) -> bool:
  module = request.module
  if module is None:
    return False
  return getattr(module, 'NEEDS_BANK', False) or hasattr(module, 'CFG')


@pytest.fixture(autouse=True)
def _use_an_existing_bank(request, monkeypatch):
  if not _needs_bank(request):
    return
  from tutor.pieces import resolve

  cfg = getattr(request.module, 'CFG', None)
  if cfg is None:
    from tutor.config import TutorConfig
    cfg = TutorConfig()
  try:
    path, slug, _why = resolve(cfg)
  except SystemExit:
    pytest.skip('no question bank on disk. Build one with:\n'
                '  .venv/bin/python memorization/analyze.py <score.musicxml>')
  # Point the module's config at the resolved bank so the tests exercise the
  # piece that is actually present. raising=False because a module without a
  # module-level CFG is fine -- we just add one.
  monkeypatch.setattr(request.module, 'CFG',
                      dataclasses.replace(cfg, fingerprint=path, piece=slug),
                      raising=False)
