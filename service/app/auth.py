"""Who is allowed in, when this service is served under the broker's own origin.

The service has no accounts and no session story of its own, and this is deliberately not
a login page: the page is meant to be served *inside* the broker's origin (a path prefix
on the same host), where the browser already holds the broker's access token in
`localStorage` and the broker has already done the signing in.

So three ways in, and which ones apply is configuration:

* **the broker's token** -- `SERVICE_TRUST_JWT` is the broker's `SECRET_KEY`, and a token
  signed with it (HS256, as `app/core/security.py` does) is accepted. Optional
  `SERVICE_ALLOWED_SUBJECTS` restricts it to particular user ids, because a broker token
  carries only `sub` and `exp` -- no roles -- so without that list *any* signed-in user
  could read every piece. (Find your id with `GET /api/v1/users/me` on the broker.)
* **a shared upload token** -- `SERVICE_UPLOAD_TOKEN`, for the worker on the LAN, which
  has no user token and needs to publish a finished piece. It is compared in constant
  time and only unlocks the upload endpoint, never the pages.
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


def credentials_from(request: Request) -> Optional[str]:
  """The bearer token on a request, if there is one."""
  raw = request.headers.get(AUTH_HEADER) or request.headers.get(AUTH_HEADER.lower())
  if not raw:
    return None
  if raw.lower().startswith(BEARER):
    return raw[len(BEARER):].strip() or None
  return raw.strip() or None


def verify_token(token: str, secret: str, allowed: Optional[set] = None) -> Dict[str, Any]:
  """Decode a broker token, or raise `HTTPException(401)`.

  HS256 with the broker's `SECRET_KEY`, which is what `create_access_token` uses. The
  expiry is checked by the library, so a stale token is a 401 rather than a page that
  half-loads.
  """
  try:
    import jwt
  except ImportError:                        # pragma: no cover - deployment mistake
    raise HTTPException(500, 'the service was built without PyJWT, so it cannot check '
                             'the broker\'s tokens; install pyjwt or unset SERVICE_TRUST_JWT')
  try:
    payload = jwt.decode(token, secret, algorithms=['HS256'])
  except Exception as e:
    raise HTTPException(401, 'that token is not valid: %s' % e)
  if allowed:
    subject = str(payload.get('sub') or '')
    if subject not in allowed:
      raise HTTPException(403, 'this account is not allowed to read these pieces')
  return payload


class Gate:
  """The service's front door, or a doorman who lets everyone through.

  `mode` is 'open' or 'token', and the page is told which in `/status` so it can show a
  sign-in link instead of an error when the token is missing or stale.
  """

  def __init__(self, *, trust_jwt: str = '', allowed_subjects: str = '',
               upload_token: str = '', broker_login: str = '/login'):
    self.trust_jwt = (trust_jwt or '').strip()
    self.allowed = {s.strip() for s in (allowed_subjects or '').split(',') if s.strip()}
    self.upload_token = (upload_token or '').strip()
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
            'restricted': bool(self.allowed)}

  def require_user(self, request: Request) -> Optional[Dict[str, Any]]:
    """FastAPI dependency: the caller's authenticated payload, or a 401."""
    if not self.enabled:
      return None
    token = credentials_from(request)
    if not token:
      raise HTTPException(401, 'sign in first', headers={'WWW-Authenticate': 'Bearer'})
    return verify_token(token, self.trust_jwt, self.allowed or None)

  def accepts_upload(self, token: Optional[str]) -> bool:
    """Is this the worker's upload token? Compared without an early exit."""
    if not self.upload_token:
      return False
    return hmac.compare_digest(self.upload_token, token or '')
