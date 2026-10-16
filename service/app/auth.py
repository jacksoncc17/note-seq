"""Who is allowed in, when this service is served under the broker's own origin.

The service has no accounts and no session story of its own, and this is deliberately not
a login page: the page is meant to be served *inside* the broker's origin (a path prefix
on the same host), where the browser already holds the broker's access token in
`localStorage` and the broker has already done the signing in.

So three ways in, and which ones apply is configuration:

* **the broker's token** -- `SERVICE_TRUST_JWT` is the broker's `SECRET_KEY`, and a token
  signed with it (HS256, as `app/core/security.py` does) is accepted. A token says *who*
  you are and nothing more: a piece carries the subject of the account whose job produced
  it, and the page shows you the pieces you own. Optional `SERVICE_ALLOWED_SUBJECTS` lists
  **operators** -- accounts that see every piece, including ones whose owner is unknown --
  which is what a deployment for one person wants set (find your id with `GET
  /api/v1/users/me` on the broker). It is *not* an access list any more: an account that
  is not on it still reads its own pieces. See `Gate.viewer`.
* **an upload token** -- `SERVICE_UPLOAD_TOKEN`, for the workers that publish finished
  pieces (`PUT /api/v1/pieces/{slug}`). It is compared in constant time and unlocks the
  publish endpoint only, never the pages. Comma-separate it to give each publishing machine
  its own, so one can be revoked without rotating everybody's secret.
* **nothing** -- the default. A service on loopback for one person needs no gate, and
  adding one silently would be a surprising way to lock somebody out of their own tool.

Why trust the token rather than ask the broker: asking means an HTTPS round trip on every
request and a broker outage taking the pages with it, for a check that is a signature.
The cost is honest and worth stating: whoever holds `SERVICE_TRUST_JWT` can mint tokens
that this service accepts, so it belongs in the same trust domain as the broker -- which
is the arrangement `SERVICE_TRUST_JWT=<the broker's SECRET_KEY>` describes.
"""

from __future__ import annotations

import hmac
from typing import Any, Dict, Optional

from fastapi import HTTPException, Request

# What the browser sends, and what the page's own fetches send: the broker's token as a
# Bearer header, exactly as the broker's own client does (frontend/src/hooks/useAuth.ts).
AUTH_HEADER = 'Authorization'
BEARER = 'bearer '

# A worker may also name its token explicitly. Both are accepted because the bearer header
# is shared with the broker's user tokens, and a publisher that sends the wrong kind of
# credential into that header is a confusing 401 to debug -- an upload with this header
# says what it is.
UPLOAD_HEADER = 'X-Upload-Token'


def credentials_from(request: Request) -> Optional[str]:
  """The bearer token on a request, if there is one."""
  raw = request.headers.get(AUTH_HEADER) or request.headers.get(AUTH_HEADER.lower())
  if not raw:
    return None
  if raw.lower().startswith(BEARER):
    return raw[len(BEARER):].strip() or None
  return raw.strip() or None


def verify_token(token: str, secret: str) -> Dict[str, Any]:
  """Decode a broker token, or raise `HTTPException(401)`.

  HS256 with the broker's `SECRET_KEY`, which is what `create_access_token` uses. The
  expiry is checked by the library, so a stale token is a 401 rather than a page that
  half-loads.

  A valid signature is as far as *this* function goes, and it is deliberately all it does:
  it says the broker issued the token, and nothing about what the holder may read. That
  question is about ownership, and it is answered against the pieces themselves -- see
  `Gate.viewer` and `PieceStore.listing`.
  """
  try:
    import jwt
  except ImportError:                        # pragma: no cover - deployment mistake
    raise HTTPException(500, 'the service was built without PyJWT, so it cannot check '
                             'the broker\'s tokens; install pyjwt or unset SERVICE_TRUST_JWT')
  try:
    return jwt.decode(token, secret, algorithms=['HS256'])
  except Exception as e:
    raise HTTPException(401, 'that token is not valid: %s' % e)


class Gate:
  """The service's front door, or a doorman who lets everyone through.

  `mode` is 'open' or 'token', and the page is told which in `/status` so it can show a
  sign-in link instead of an error when the token is missing or stale.
  """

  def __init__(self, *, trust_jwt: str = '', allowed_subjects: str = '',
               upload_token: str = '', broker_login: str = '/login'):
    self.trust_jwt = (trust_jwt or '').strip()
    self.allowed = {s.strip() for s in (allowed_subjects or '').split(',') if s.strip()}
    # A list, comma-separated, because the alternative to "one shared token" should not be
    # "a table of workers": one token per publishing machine is revocable by editing a
    # variable, and costs nothing to keep. One entry is the common case.
    self.upload_tokens = [t.strip() for t in (upload_token or '').split(',') if t.strip()]
    # Where to send a browser whose token is missing or expired. Relative, because the
    # page is served on the broker's own origin -- a ../login keeps working whatever the
    # host is, and a subdomain deployment can override it.
    self.broker_login = broker_login or '/login'

  @property
  def enabled(self) -> bool:
    return bool(self.trust_jwt)

  def status(self) -> Dict[str, Any]:
    """What the page needs to know about the door, without leaking anything."""
    return {'mode': 'token' if self.enabled else 'open',
            'login_url': self.broker_login if self.enabled else None,
            # Pieces are filtered by who owns them, which is only meaningful when there is
            # somebody to filter by: with no gate this is a service for one person on
            # loopback, and it shows them everything.
            'owner_filter': self.enabled,
            'operators': len(self.allowed)}

  def require_user(self, request: Request) -> Optional[Dict[str, Any]]:
    """FastAPI dependency: the caller's authenticated payload, or a 401."""
    if not self.enabled:
      return None
    token = credentials_from(request)
    if not token:
      raise HTTPException(401, 'sign in first', headers={'WWW-Authenticate': 'Bearer'})
    return verify_token(token, self.trust_jwt)

  def viewer(self, request: Request) -> Dict[str, Any]:
    """Who is asking, and how much of the store they may see. A FastAPI dependency.

    Three cases, and the third is the one that changed when pieces gained an owner:

    * **no gate** -- loopback, one person: `all` is true, as it always was.
    * **a subject in `SERVICE_ALLOWED_SUBJECTS`** -- an operator: `all` is true. The list
      used to mean "only these accounts may read anything at all", which made every other
      signed-in user a 403; now ownership answers that question, and the list answers the
      different one of who may see *everything*. That matters because a broker's sign-up is
      often open, so "signed in" is not a small set of people.
    * **any other valid token** -- `all` is false, and the store shows them the pieces they
      own. Somebody with no pieces sees an empty list rather than a refusal, which is both
      the honest answer and the one that makes a closed deployment work without a second
      mechanism.
    """
    payload = self.require_user(request)
    subject = str((payload or {}).get('sub') or '')
    return {'sub': subject, 'all': (not self.enabled) or (subject in self.allowed)}

  def accepts_upload(self, token: Optional[str]) -> bool:
    """Is this one of the worker's upload tokens? Compared without an early exit.

    Every configured token is compared even after a match, because the time this takes
    should not depend on which token was right -- the same reason the comparison itself is
    `compare_digest` rather than `==`.
    """
    if not self.upload_tokens or not token:
      return False
    matched = False
    for candidate in self.upload_tokens:
      if hmac.compare_digest(candidate, token):
        matched = True
    return matched

  def require_writer(self, request: Request) -> Optional[Dict[str, Any]]:
    """FastAPI dependency for publishing a piece: an upload token, or a signed-in user.

    Reading is not writing, so this is deliberately a different door from
    `require_user` -- and it is the one place where an upload token is accepted at all.
    Which of the three arrangements applies depends on what is configured:

    * an upload token that matches -- the worker on the LAN, which has no user token;
    * `SERVICE_TRUST_JWT` set -- a broker token is required as well, so a human can
      publish from the page or with `curl` and an anonymous caller cannot;
    * an upload token set but no broker trust -- a token is still required, because
      configuring one is a statement that publishing is not open;
    * nothing configured at all -- open, like every other endpoint on loopback.
    """
    token = request.headers.get(UPLOAD_HEADER) or credentials_from(request)
    if self.accepts_upload(token):
      return {'sub': 'upload-token', 'via': 'upload-token'}
    if self.enabled:
      return self.require_user(request)
    if self.upload_tokens:
      raise HTTPException(401, 'a token is needed to publish a piece here',
                          headers={'WWW-Authenticate': 'Bearer'})
    return None
