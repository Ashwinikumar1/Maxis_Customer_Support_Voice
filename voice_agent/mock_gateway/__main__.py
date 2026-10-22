"""Standalone mock gateway over real HTTP: python -m mock_gateway --port 8090

Then set GATEWAY_URL=http://127.0.0.1:8090 in .env. Local testing only (no auth).
"""

import argparse
import asyncio
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .app import handle

_loop = asyncio.new_event_loop()
_lock = __import__("threading").Lock()


class _Handler(BaseHTTPRequestHandler):

  def _serve(self):
    length = int(self.headers.get("Content-Length") or 0)
    body = self.rfile.read(length) if length else b""
    with _lock:  # the mock's state isn't thread-safe; one request at a time
      status, data = _loop.run_until_complete(handle(self.command, self.path, dict(self.headers), body))
    raw = json.dumps(data).encode()
    self.send_response(status)
    self.send_header("Content-Type", "application/json")
    self.send_header("Content-Length", str(len(raw)))
    self.end_headers()
    self.wfile.write(raw)

  do_GET = do_POST = _serve


def main():
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument("--host", default="127.0.0.1")
  parser.add_argument("--port", type=int, default=8090)
  args = parser.parse_args()
  print(f"Mock gateway on http://{args.host}:{args.port} (DEMO DATA)")
  ThreadingHTTPServer((args.host, args.port), _Handler).serve_forever()


if __name__ == "__main__":
  main()
