"""When was each piece last read? The record eviction needs, and nothing more.

Pieces get evicted from the serving deployment when nobody has opened them for a while, so
"nobody has opened them" has to be answerable. This is that answer: one row per piece, with
the last time it was read and how many times.

Two deliberate choices:

* **A read is coalesced.** The first read in a window records the time; reads inside the
  window only count, and do not rewrite the row. A page load fetches a piece, its page image
  and its audio -- three reads -- and a practice session touches the same piece for an hour.
  Writing on each of those would put a SQLite write behind every click to answer a question
  whose resolution is *days*. `COALESCE_SECONDS` is one hour; an access older than that is
  what moves `last_access`.
* **`accesses` therefore counts sessions, not requests.** Said plainly because a counter that
  looks exact and is not is worse than no counter: its job here is to show that a piece is
  being used, not to measure how often.

The ledger is advisory: if it cannot be written, the read still succeeds (`record` swallows
its own errors, and the eviction treats a piece with no row as "unknown, date it from
`created_at`" rather than as "unused").
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

# How much a single read may move `last_access`. See the module docstring.
COALESCE_SECONDS = 3600.0


class AccessLog:
  """Read and write `piece_access` in the service's own SQLite database."""

  def __init__(self, database):
    self.database = database

  # ------------------------------------------------------------------ writing

  def record(self, slug: str, *, now: Optional[float] = None) -> None:
    """Note that `slug` was read. Never raises: a read must not fail over bookkeeping."""
    moment = time.time() if now is None else float(now)
    cutoff = moment - COALESCE_SECONDS
    try:
      with self.database.immediate() as connection:
        connection.execute(
            'INSERT INTO piece_access (slug, last_access, accesses) VALUES (?, ?, 1) '
            'ON CONFLICT(slug) DO UPDATE SET '
            '  accesses = accesses + 1, '
            '  last_access = CASE WHEN last_access < ? THEN ? ELSE last_access END',
            (slug, moment, cutoff, moment))
    except Exception:                        # noqa: BLE001 - see the module docstring
      pass

  def forget(self, slug: str) -> None:
    """Drop a piece's row -- used when the piece itself is deleted for good."""
    try:
      with self.database.immediate() as connection:
        connection.execute('DELETE FROM piece_access WHERE slug = ?', (slug,))
    except Exception:                        # noqa: BLE001
      pass

  # ------------------------------------------------------------------ reading

  def last_access(self) -> Dict[str, Dict[str, Any]]:
    """`{slug: {'last_access': float, 'accesses': int}}` for everything recorded."""
    try:
      # `connection` is a property on the Database (one per thread), not a method -- calling it
      # raised a TypeError that the `except` below turned into "the ledger is empty", which is
      # why every piece looked unread.
      rows = self.database.connection.execute(
          'SELECT slug, last_access, accesses FROM piece_access').fetchall()
    except Exception:                        # noqa: BLE001 - an unreadable ledger is empty
      return {}
    return {row['slug']: {'last_access': float(row['last_access']),
                          'accesses': int(row['accesses'])} for row in rows}

  def stats(self) -> Dict[str, Any]:
    """A few numbers for `/status`: how much the ledger knows, and how recently."""
    rows = self.last_access()
    if not rows:
      return {'pieces': 0, 'last_access': None}
    return {'pieces': len(rows),
            'last_access': max(row['last_access'] for row in rows.values())}


def usage_order(pieces: Any, log: AccessLog) -> List[Dict[str, Any]]:
  """Every piece, least recently used first, as the eviction needs to see it.

  A piece with no row has never been read *since the ledger existed* -- which for pieces
  published earlier means "since the archive was set up", not "never". Dating those from the
  piece's own `created_at` is the honest reading, and it fails in the safe direction: an old
  piece with no record is evicted, a new one is not.
  """
  recorded = log.last_access()
  rows = []
  for slug in pieces.slugs():
    meta = pieces.meta(slug) or {}
    seen = recorded.get(slug)
    when = float(seen['last_access']) if seen else float(meta.get('created_at') or 0.0)
    rows.append({'slug': slug,
                 'last_access': when,
                 'accesses': int(seen['accesses']) if seen else 0,
                 'recorded': seen is not None,
                 'created_at': float(meta.get('created_at') or 0.0),
                 'title': meta.get('title') or slug})
  rows.sort(key=lambda row: row['last_access'])
  return rows
