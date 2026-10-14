"""The front door: sharing the broker's login, and serving without a queue.

The arrangement these tests are about: the page is served under the *broker's* origin (a
path prefix on the same host), so the browser already holds the broker's access token in
`localStorage` and there is no second password anywhere. This service checks the token's
HS256 signature with the broker's `SECRET_KEY` instead of running a login of its own.

The other half is `SERVICE_READ_ONLY`: a deployment that only serves finished pieces has
no worker attached, so a form there would queue an analysis nobody ever runs.
"""

from __future__ import annotations

import time

import pytest

from service.app.config import Settings
from service.app.jobs import JobStore

SECRET = 'a-broker-secret-long-enough-to-be-plausible'


def gated(tmp_path, **settings):
  """A service rooted at `tmp_path`, with the door configured as asked."""
  from service.app.api import create_app
  from fastapi.testclient import TestClient

  made = Settings(data_dir=tmp_path / 'data')
  made.embedded_worker = False
  made.trust_jwt = settings.pop('trust_jwt', SECRET)
  for key, value in settings.items():
    setattr(made, key, value)
  made.ensure_dirs()
  JobStore(made)
  return TestClient(create_app(made)), made


def token(subject='user-1', secret=SECRET, expires_in=600):
  import jwt
  return jwt.encode({'sub': subject, 'exp': int(time.time()) + expires_in}, secret,
                    algorithm='HS256')


def bearer(value):
  return {'Authorization': 'Bearer %s' % value}


# --------------------------------------------------------------------------- the door

def test_without_a_trusted_secret_nothing_is_checked(client):
  """A service on loopback for one person needs no gate, and adding one silently would
  be a surprising way to lock somebody out of their own tool."""
  assert client.get('/api/v1/pieces').status_code == 200
  assert client.get('/api/v1/status').json()['auth']['mode'] == 'open'


def test_the_brokers_token_is_accepted(tmp_path):
  api, _settings = gated(tmp_path)
  assert api.get('/api/v1/pieces', headers=bearer(token())).status_code == 200
  assert api.get('/api/v1/status').json()['auth'] == {
      'mode': 'token', 'login_url': '/login', 'restricted': False}


def test_no_token_is_a_401_that_says_so(tmp_path):
  api, _settings = gated(tmp_path)
  response = api.get('/api/v1/pieces')
  assert response.status_code == 401
  assert response.headers.get('WWW-Authenticate') == 'Bearer'
  assert 'sign in' in response.json()['detail']


def test_a_token_signed_with_something_else_is_refused(tmp_path):
  """The whole security of this rests on the signature: a token that verifies against
  any other secret -- including the development `SECRET_KEY` in the repo, which is a
  *different* value from the deployed broker's -- must be rejected."""
  api, _settings = gated(tmp_path)
  forged = token(secret='not-the-deployed-brokers-secret')
  response = api.get('/api/v1/pieces', headers=bearer(forged))
  assert response.status_code == 401
  assert 'not valid' in response.json()['detail']


def test_an_expired_token_is_refused(tmp_path):
  """The broker's tokens last eight days; a stale one has to be a 401 rather than a page
  that half-loads."""
  api, _settings = gated(tmp_path)
  assert api.get('/api/v1/pieces', headers=bearer(token(expires_in=-60))).status_code == 401


def test_only_the_named_accounts_can_read(tmp_path):
  """A broker token carries `sub` and `exp` and no roles, so without a list *any*
  signed-in user of the broker could read every piece."""
  api, _settings = gated(tmp_path, allowed_subjects='user-1, user-2')
  assert api.get('/api/v1/pieces', headers=bearer(token('user-2'))).status_code == 200
  refused = api.get('/api/v1/pieces', headers=bearer(token('somebody-else')))
  assert refused.status_code == 403
  assert 'not allowed' in refused.json()['detail']
  assert api.get('/api/v1/status').json()['auth']['restricted'] is True


def test_status_is_readable_without_a_token(tmp_path):
  """The page has to be able to ask how to sign in *before* it has a token -- otherwise
  it can only show failing requests."""
  api, _settings = gated(tmp_path)
  status = api.get('/api/v1/status')
  assert status.status_code == 200
  assert status.json()['auth']['login_url'] == '/login'


def test_the_sign_in_destination_is_configurable(tmp_path):
  api, _settings = gated(tmp_path, broker_login='https://broker.example.com/login')
  assert api.get('/api/v1/status').json()['auth']['login_url'] == \
      'https://broker.example.com/login'


def test_every_read_of_a_piece_is_gated(tmp_path):
  """Not just the JSON: the engraved pages and the audio are the content."""
  api, settings = gated(tmp_path)
  assert api.get('/api/v1/pieces').status_code == 401
  assert api.get('/api/v1/pieces/anything').status_code == 401
  assert api.get('/api/v1/pieces/anything/pages/1.svg').status_code == 401
  assert api.get('/api/v1/pieces/anything/audio').status_code == 401


# --------------------------------------------------------------------------- read-only

def test_read_only_refuses_a_new_job(tmp_path):
  """A serving-only deployment has no worker: a queued job would sit there forever."""
  api, _settings = gated(tmp_path, trust_jwt='', read_only=True)
  posted = api.post('/api/v1/jobs',
                    files={'score': ('s.musicxml', b'<score-partwise/>', 'text/plain')})
  assert posted.status_code == 405
  assert 'broker' in posted.json()['detail']

  # and it is refused before the body is even looked at, so a malformed submission gets
  # the same answer as a well-formed one rather than a 422 about a missing file
  assert api.post('/api/v1/jobs').status_code == 405


def test_read_only_still_serves_what_it_has(tmp_path):
  api, settings = gated(tmp_path, trust_jwt='', read_only=True)
  assert api.get('/api/v1/pieces').status_code == 200
  status = api.get('/api/v1/status').json()
  assert status['read_only'] is True
  # the frontend is told, so it can show the pieces and say where analyses are submitted
  assert status['auth']['mode'] == 'open'


def test_read_only_does_not_stop_the_worker_that_is_there(tmp_path):
  """The flag is about what the *deployment* offers, not about disabling the analysis
  path: a service that is also where the work runs keeps working."""
  api, settings = gated(tmp_path, trust_jwt='', read_only=False)
  assert JobStore(settings).create({'score': '/tmp/x.mxl'}).status == 'queued'
