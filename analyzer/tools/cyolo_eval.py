"""Run CyoloScoreFollowing's own evaluation on a piece exported by `analyzer.cyolo`.

Their code lives in another checkout and another conda environment (python 3.7,
numpy 1.x), so this is a bridge rather than a library:

    ~/miniconda3/envs/cyolo_score_following/bin/python tools/cyolo_eval.py \
        --repo ~/projects/cyolo_score_following \
        --param_path ~/projects/cyolo_score_following/trained_models/cyolo_sb/best_model.pt \
        --test_dirs output/cyolo/recording --only_onsets

The one thing it does beyond forwarding arguments: numpy 2 renamed `numpy.core` to
`numpy._core`, so an object array written by the analyzer's numpy cannot be unpickled by
the numpy 1.x in their environment. The alias below is installed in this process only --
nothing in their repository is modified.
"""

import argparse
import pathlib
import runpy
import sys


def install_numpy_bridge() -> None:
  """Let a numpy 1.x read object arrays written by numpy 2."""
  try:
    import numpy as np
    import numpy.core
    import numpy.core.multiarray
    import numpy.core.numeric
  except Exception:
    return
  np._core = numpy.core                       # type: ignore[attr-defined]
  sys.modules.setdefault('numpy._core', numpy.core)
  sys.modules.setdefault('numpy._core.multiarray', numpy.core.multiarray)
  sys.modules.setdefault('numpy._core.numeric', numpy.core.numeric)


def main() -> int:
  parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
  parser.add_argument('--repo', required=True, help='the cyolo_score_following checkout')
  parser.add_argument('--script', default='cyolo_score_following/eval.py',
                      help='which of their scripts to run')
  parser.add_argument('rest', nargs=argparse.REMAINDER,
                      help='arguments passed straight to their script')
  args = parser.parse_args()

  repo = pathlib.Path(args.repo).expanduser().resolve()
  script = repo / args.script
  if not script.is_file():
    print('no such script: %s' % script)
    return 2
  install_numpy_bridge()
  sys.path.insert(0, str(repo))
  sys.argv = [str(script)] + [a for a in args.rest if a != '--']
  runpy.run_path(str(script), run_name='__main__')
  return 0


if __name__ == '__main__':
  raise SystemExit(main())
