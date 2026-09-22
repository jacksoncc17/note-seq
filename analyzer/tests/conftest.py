"""Shared test setup.

Puts the `analyzer/` directory on `sys.path` so `import analyzer.<module>` works
when pytest is run from the repository root, mirroring how `tutor/tests` does it.
"""

import pathlib
import sys

_PACKAGE_PARENT = pathlib.Path(__file__).resolve().parents[1]
if str(_PACKAGE_PARENT) not in sys.path:
  sys.path.insert(0, str(_PACKAGE_PARENT))


def pytest_configure(config):
  """Register the marker for tests that talk to YouTube for real."""
  config.addinivalue_line(
      'markers', 'network: needs the live YouTube API (ANALYZER_NETWORK_TESTS=1)')
