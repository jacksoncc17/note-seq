"""Command-line entry point for the analyzer.

    .venv/bin/python -m analyzer.app                # open the window
    .venv/bin/python -m analyzer.app --check        # verify the setup, no window
"""

from __future__ import annotations

import argparse
import pathlib
import sys
from typing import List, Optional

from .config import AnalyzerConfig


def build_parser() -> argparse.ArgumentParser:
  ap = argparse.ArgumentParser(
      prog='analyzer.app', description='Qt5 tool for aligning a score with a recording')
  ap.add_argument('--piece', default='', help='slug of the piece to open')
  ap.add_argument('--score', default='', help='open this MusicXML/MXL file directly')
  ap.add_argument('--url', default='', help='pre-fill the recording link')
  ap.add_argument('--serve', action='store_true',
                  help='serve the practice client for a phone or tablet browser')
  ap.add_argument('--host', default='0.0.0.0', help='address for --serve')
  ap.add_argument('--port', type=int, default=8090, help='port for --serve')
  ap.add_argument('--check', action='store_true',
                  help='report what is installed and what pieces were found, then exit')
  ap.add_argument('--list-pieces', action='store_true', help='list pieces and exit')
  return ap


def _report(cfg: AnalyzerConfig) -> int:
  """Print what the app can and cannot do right now."""
  from . import library, measures

  print('== analyzer check ==')
  print('  python      : %s' % sys.version.split()[0])
  for module, why in (('PyQt5.QtWidgets', 'the window'),
                      ('numpy', 'alignment'),
                      ('soundfile', 'reading audio'),
                      ('librosa', 'alignment'),
                      ('verovio', 'engraving the score'),
                      ('cairosvg', 'turning the engraving into page images'),
                      ('PIL', 'measuring the rendered pages')):
    try:
      __import__(module)
      print('  %-11s : ok' % module.split('.')[0])
    except ImportError as e:
      print('  %-11s : MISSING (%s needs it) — %s' % (module.split('.')[0], why, e))

  for module, why in (('analyzer.ytaudio', 'fetching recordings'),
                      ('analyzer.align', 'aligning measures to audio')):
    try:
      __import__(module)
      print('  %-11s : ok' % module.split('.')[1])
    except ImportError:
      print('  %-11s : not available (%s disabled)' % (module.split('.')[1], why))

  print('  cache       : %s' % cfg.cache_dir)
  print('  maps        : %s' % cfg.sync_dir)

  pieces = library.discover(cfg)
  print('\n  pieces: %d' % len(pieces))
  for piece in pieces:
    bits = []
    if piece.has_score:
      try:
        bits.append(measures.expand_measures(piece.musicxml).summary())
      except measures.ScoreError as e:
        bits.append('score unreadable (%s)' % e)
    else:
      bits.append('no score')
    bits.append('reference MIDI' if piece.reference_midi else 'no reference MIDI')
    bits.append('audio cached' if piece.has_audio else 'no audio')
    if piece.sync:
      bits.append('%s map' % piece.sync.method)
    print('    %-38s %s' % (piece.slug, ', '.join(bits)))
  if not pieces:
    print('    (none — build one with: .venv/bin/python memorization/analyze.py <score>)')
  return 0


def _install_excepthook(get_window=None) -> None:
  """Report an unexpected error instead of letting Qt abort the process.

  PyQt5 terminates the application when an exception escapes a slot -- which is
  how a single wrong attribute name turned into a core dump rather than a message.
  Installing a hook turns any such bug into a dialog plus a traceback on stderr,
  so the window survives and the user can say what they were doing.
  """
  import traceback

  def hook(exc_type, exc, tb) -> None:
    text = ''.join(traceback.format_exception(exc_type, exc, tb))
    print(text, file=sys.stderr, flush=True)
    try:
      from PyQt5.QtWidgets import QMessageBox
      window = get_window() if get_window else None
      QMessageBox.critical(
          window, 'Something went wrong',
          '%s: %s\n\nThe rest of the window still works; the traceback is in the '
          'terminal.' % (exc_type.__name__, exc))
    except Exception:
      pass

  sys.excepthook = hook


def main(argv: Optional[List[str]] = None) -> int:
  args = build_parser().parse_args(argv)
  cfg = AnalyzerConfig()

  if args.serve:
    # No Qt needed: the server is a second front-end on the same core.
    from . import server
    server.serve(cfg, host=args.host, port=args.port)
    return 0
  if args.check:
    return _report(cfg)
  if args.list_pieces:
    from . import library
    for piece in library.discover(cfg):
      print('%-40s %s' % (piece.slug, piece.describe()))
    return 0

  try:
    from PyQt5.QtWidgets import QApplication
  except ImportError:
    print('PyQt5 is not installed. Install it with:\n'
          '  .venv/bin/pip install PyQt5', file=sys.stderr)
    return 2

  from .ui.main_window import MainWindow

  app = QApplication(sys.argv[:1])
  app.setApplicationName('analyzer')
  window = MainWindow(cfg, piece_slug=args.piece)
  _install_excepthook(lambda: window)
  if args.score:
    path = pathlib.Path(args.score).expanduser()
    if path.is_file():
      window.open_score(path)
    else:
      print('no such score: %s' % path, file=sys.stderr)
  if args.url:
    window.url_edit.setText(args.url)
  window.show()
  return app.exec_()


if __name__ == '__main__':
  raise SystemExit(main())
