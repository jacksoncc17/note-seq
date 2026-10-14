"""A stand-in for Traefik's path routing, for verifying the hybrid locally.

In the deployment, Traefik gets `Host(domain) && PathPrefix('/note-seq')` for this
service and forwards everything else on that host to the broker. This does the same two
things on one local port:

    /note-seq/...  ->  the note-seq service on 8101, with the prefix stripped
    everything else ->  the broker on 8000

That matters for the check it is used by: the whole point of the arrangement is that the
page shares the broker's *origin*, so `localStorage.access_token` -- which is per-origin
-- is the token the page uses. Serving both behind one port is what makes that true
locally as well.

    .venv/bin/python service/tools/prefix_proxy.py       # then open :8200/note-seq/

Port 8101 is where the note-seq side is: the service started with `run.sh --port 8101`,
or the container publishing itself with `docker run -p 8101:8000`. Port 8000 is the broker
side -- its backend, or its front end's dev server -- and only matters for the paths that
are not this service's.
"""

from __future__ import annotations

import http.client
import http.server
import sys
import urllib.parse

PREFIX = '/note-seq'
NOTE_SEQ = ('127.0.0.1', 8101)
BROKER = ('127.0.0.1', 8000)
PORT = 8200


class Proxy(http.server.BaseHTTPRequestHandler):
  protocol_version = 'HTTP/1.1'

  def _forward(self, body: bytes | None = None) -> None:
    parsed = urllib.parse.urlsplit(self.path)
    if parsed.path == PREFIX or parsed.path.startswith(PREFIX + '/'):
      target, path = NOTE_SEQ, parsed.path[len(PREFIX):] or '/'
    else:
      target, path = BROKER, parsed.path
    if parsed.query:
      path += '?' + parsed.query

    connection = http.client.HTTPConnection(*target, timeout=120)
    length = int(self.headers.get('Content-Length') or 0)
    payload = body if body is not None else (self.rfile.read(length) if length else None)
    headers = {k: v for k, v in self.headers.items()
               if k.lower() not in ('host', 'connection', 'accept-encoding')}
    try:
      connection.request(self.command, path, body=payload, headers=headers)
      response = connection.getresponse()
      data = response.read()
    except Exception as e:                     # noqa: BLE001 - a proxy is allowed to fail loud
      self.send_error(502, 'upstream %s: %s' % (target, e))
      return
    finally:
      connection.close()

    self.send_response(response.status)
    for key, value in response.getheaders():
      if key.lower() in ('transfer-encoding', 'connection', 'content-length'):
        continue
      self.send_header(key, value)
    self.send_header('Content-Length', str(len(data)))
    self.end_headers()
    if self.command != 'HEAD':
      self.wfile.write(data)

  def do_GET(self) -> None:
    self._forward()

  def do_HEAD(self) -> None:
    self._forward()

  def do_POST(self) -> None:
    self._forward()

  def do_DELETE(self) -> None:
    self._forward()

  def log_message(self, fmt, *args) -> None:
    if '40' in (args[1] if len(args) > 1 else '') or '-1' in str(args):
      sys.stderr.write('  proxy: %s\n' % (fmt % args))


if __name__ == '__main__':
  server = http.server.ThreadingHTTPServer(('127.0.0.1', PORT), Proxy)
  print('routing %s -> note-seq:8101, everything else -> broker:8000' % PREFIX, flush=True)
  server.serve_forever()
