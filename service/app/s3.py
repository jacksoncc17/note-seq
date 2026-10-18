"""A small S3 client: enough to archive a piece and get it back, with no dependency.

**Why not boto3.** `botocore` + `s3transfer` are about 40 MB installed, for four verbs. The
serving half of this project is deliberately 20 packages and no analyzer, and the whole point
of `app/clips.py` was to keep it that way; adding 40 MB for `PUT`, `GET`, `HEAD` and
`LIST` would be the largest dependency in the image by a factor of two. So this is
signature v4 over `urllib`, in about as much code as the imports it replaces.

**What it supports**, deliberately narrow: path-style addressing (which is what Garage
serves), no multipart upload (a piece is ~16 MB, well under any single-request limit), no
retries beyond one, no ACLs, no presigning. Anything else belongs in a real client, and the
moment this needs more than four verbs is the moment to reconsider boto3.

Errors are raised as `S3Error` with the status and whatever XML detail came back, because
"SignatureDoesNotMatch" and "NoSuchBucket" both look like "it did not work" until you read
the body.
"""

from __future__ import annotations

import datetime
import hashlib
import hmac
import pathlib
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from typing import Any, Dict, List, Optional

EMPTY_SHA256 = hashlib.sha256(b'').hexdigest()


class S3Error(RuntimeError):
  """The store refused a request, with why."""

  def __init__(self, message: str, *, status: Optional[int] = None, code: str = ''):
    super().__init__(message)
    self.status = status
    self.code = code


def _sha256(payload: bytes) -> str:
  return hashlib.sha256(payload).hexdigest()


def _hmac(key: bytes, message: str) -> bytes:
  return hmac.new(key, message.encode('utf-8'), hashlib.sha256).digest()


def _quote(value: str, safe: str = '-_.~') -> str:
  return urllib.parse.quote(value, safe=safe)


class Client:
  """Path-style S3 against one bucket.

  `region` must match the store's own configured region: Garage accepts the region it was
  started with (`garage` in the deployment beside this file) and refuses anything else with
  `AuthorizationHeaderMalformed`, which reads like a credentials problem and is not one.
  """

  def __init__(self, endpoint: str, bucket: str, key: str, secret: str, *,
               region: str = 'garage', timeout: float = 120.0):
    parsed = urllib.parse.urlsplit(endpoint.rstrip('/'))
    if not parsed.scheme or not parsed.netloc:
      raise S3Error('the archive endpoint must be an http(s) URL, not %r' % endpoint)
    self.endpoint = '%s://%s' % (parsed.scheme, parsed.netloc)
    self.base_path = parsed.path.rstrip('/')
    self.bucket = bucket
    self.key = key
    self.secret = secret
    self.region = region
    self.timeout = timeout
    self._opener = urllib.request.build_opener()

  # ------------------------------------------------------------------ verbs

  def put(self, name: str, payload: bytes) -> Dict[str, Any]:
    """Store `payload` under `name`. Idempotent: the same bytes twice is one object."""
    response = self._request('PUT', name, payload=payload)
    return {'name': name, 'bytes': len(payload),
            'etag': response.header('etag').strip('"')}

  def get(self, name: str) -> bytes:
    return self._request('GET', name).body

  def head(self, name: str) -> Optional[Dict[str, Any]]:
    """Size and etag, or None when the object is not there."""
    try:
      response = self._request('HEAD', name)
    except S3Error as e:
      if e.status == 404:
        return None
      raise
    length = response.header('content-length') or '0'
    return {'name': name, 'bytes': int(length),
            'etag': response.header('etag').strip('"')}

  def delete(self, name: str) -> None:
    self._request('DELETE', name)

  def list(self, prefix: str = '') -> List[Dict[str, Any]]:
    """Everything under `prefix`, following continuation tokens.

    `ListObjectsV2` caps a response at 1000 keys; a bucket of pieces will get there, and a
    restore that silently stopped at 1000 would be worse than one that failed.
    """
    found: List[Dict[str, Any]] = []
    token = ''
    while True:
      query = {'list-type': '2', 'prefix': prefix}
      if token:
        query['continuation-token'] = token
      body = self._request('GET', '', query=query).body
      root = ET.fromstring(body)
      ns = {'s3': 'http://s3.amazonaws.com/doc/2006-03-01/'}
      for item in root.findall('s3:Contents', ns) or root.findall('Contents'):
        key = (item.findtext('s3:Key', default='', namespaces=ns)
               or item.findtext('Key', default=''))
        size = (item.findtext('s3:Size', default='0', namespaces=ns)
                or item.findtext('Size', default='0'))
        if key:
          found.append({'name': key, 'bytes': int(size or 0)})
      truncated = ((root.findtext('s3:IsTruncated', default='false', namespaces=ns)
                    or root.findtext('IsTruncated', default='false')) == 'true')
      token = (root.findtext('s3:NextContinuationToken', default='', namespaces=ns)
               or root.findtext('NextContinuationToken', default=''))
      if not truncated or not token:
        break
    return found

  # ------------------------------------------------------------------ signing

  def _request(self, method: str, name: str, *, payload: bytes = b'',
               query: Optional[Dict[str, str]] = None) -> 'Response':
    """One signed request, and the failure modes worth naming."""
    uri = '%s/%s' % (self.base_path, _quote(self.bucket)) if self.base_path else \
        '/%s' % _quote(self.bucket)
    if name:
      uri += '/' + _quote(name, safe='/-_.~')
    canonical_query = '&'.join('%s=%s' % (_quote(k), _quote(v))
                               for k, v in sorted((query or {}).items()))

    now = datetime.datetime.now(datetime.timezone.utc)
    amz_date = now.strftime('%Y%m%dT%H%M%SZ')
    date_stamp = now.strftime('%Y%m%d')
    payload_hash = _sha256(payload)

    host = urllib.parse.urlsplit(self.endpoint).netloc
    signed = {'host': host, 'x-amz-content-sha256': payload_hash, 'x-amz-date': amz_date}
    canonical_headers = ''.join('%s:%s\n' % (k, signed[k]) for k in sorted(signed))
    signed_names = ';'.join(sorted(signed))

    canonical_request = '\n'.join([
        method, uri, canonical_query, canonical_headers, signed_names, payload_hash])
    scope = '%s/%s/s3/aws4_request' % (date_stamp, self.region)
    string_to_sign = '\n'.join(['AWS4-HMAC-SHA256', amz_date, scope,
                                _sha256(canonical_request.encode('utf-8'))])

    key = ('AWS4' + self.secret).encode('utf-8')
    for part in (date_stamp, self.region, 's3', 'aws4_request'):
      key = _hmac(key, part)
    signature = hmac.new(key, string_to_sign.encode('utf-8'),
                         hashlib.sha256).hexdigest()
    authorization = ('AWS4-HMAC-SHA256 Credential=%s/%s, SignedHeaders=%s, Signature=%s'
                     % (self.key, scope, signed_names, signature))

    url = self.endpoint + uri + ('?' + canonical_query if canonical_query else '')
    request = urllib.request.Request(url, data=payload if method == 'PUT' else None,
                                     method=method)
    request.add_header('Host', host)
    request.add_header('x-amz-content-sha256', payload_hash)
    request.add_header('x-amz-date', amz_date)
    request.add_header('Authorization', authorization)
    if method == 'PUT':
      request.add_header('Content-Length', str(len(payload)))

    try:
      with self._opener.open(request, timeout=self.timeout) as response:
        return Response(response.status, dict(response.headers), response.read())
    except urllib.error.HTTPError as e:
      body = b''
      try:
        body = e.read()
      except Exception:                        # noqa: BLE001 - an unreadable body is not news
        pass
      raise S3Error('%s %s failed: HTTP %s %s' % (method, name or self.bucket, e.code,
                                                  _error_text(body) or e.reason),
                    status=e.code, code=_error_code(body))
    except urllib.error.URLError as e:
      raise S3Error('%s %s failed: %s' % (method, name or self.bucket, e.reason))


class Response:
  """A response with **case-insensitive** headers.

  `dict(response.headers)` looks harmless and is not: `http.client` preserves whatever case
  the server sent, Garage sends `content-length` in lower case, and a lookup for
  `Content-Length` then silently returns nothing -- which is how a `HEAD` of a 17-byte object
  reported zero bytes and an ETag came back empty while everything else worked.
  """

  def __init__(self, status: int, headers, body: bytes):
    self.status = status
    self.headers = {str(k).lower(): str(v) for k, v in headers.items()}
    self.body = body

  def header(self, name: str, default: str = '') -> str:
    return self.headers.get(name.lower(), default)


def _error_code(body: bytes) -> str:
  try:
    root = ET.fromstring(body)
  except Exception:                            # noqa: BLE001 - not XML, nothing to find
    return ''
  return root.findtext('Code', default='') or ''


def _error_text(body: bytes) -> str:
  try:
    root = ET.fromstring(body)
  except Exception:                            # noqa: BLE001
    return body[:200].decode('utf-8', 'replace').strip()
  parts = [root.findtext(tag, default='') or '' for tag in ('Code', 'Message')]
  return ': '.join(part for part in parts if part)


# ----------------------------------------------------------------------- the archive
#
# The archive is one object per piece, named by the piece's slug: `pieces/<slug>.zip`, the
# same package the serving deployment accepts. That is a deliberate choice over "one object
# per file" -- it makes a piece atomic, makes a restore one download, and keeps the bucket
# listable in an order that reads like the page.

ARCHIVE_PREFIX = 'pieces/'


def archive_name(slug: str) -> str:
  return '%s%s.zip' % (ARCHIVE_PREFIX, slug)


def slug_of(name: str) -> str:
  """`pieces/serenade-7.zip` -> `serenade-7`; anything else -> ''."""
  if not name.startswith(ARCHIVE_PREFIX) or not name.endswith('.zip'):
    return ''
  slug = name[len(ARCHIVE_PREFIX):-len('.zip')]
  return slug if '/' not in slug and slug else ''
