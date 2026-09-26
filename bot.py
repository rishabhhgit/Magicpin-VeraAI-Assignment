"""Vera bot — the magicpin challenge HTTP server (stdlib only).

    python3 bot.py            # listens on $PORT (default 8080)

Endpoints (all logic lives in vera/api.py and is shared with the Vercel adapter)
    GET  /v1/healthz    liveness + context counts
    GET  /v1/metadata   bot identity
    POST /v1/context    idempotent context push  (scope, context_id, version, payload)
    POST /v1/tick       proactive sends for the triggers the judge marks active
    POST /v1/reply      synchronous multi-turn reply
    POST /v1/teardown   wipe all state (end of test)

State is process-local by default; set KV_REST_API_URL + KV_REST_API_TOKEN
(Vercel KV / Upstash) to persist contexts, suppression and conversations across
serverless invocations.
"""

from __future__ import annotations

import json
import os
import sys
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from vera.api import MAX_BODY_BYTES, METADATA, STORE, dispatch


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "Vera/1.0"

    def log_message(self, fmt, *args):
        if os.environ.get("VERA_QUIET") != "1":
            sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _json(self, status: int, payload: dict):
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _read_json(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if length < 0:
            return None, "invalid content-length"
        if length > MAX_BODY_BYTES:
            return None, "payload too large"
        raw = self.rfile.read(length) if length else b"{}"
        try:
            parsed = json.loads(raw.decode("utf-8") or "{}")
        except (ValueError, UnicodeDecodeError):
            return None, "body is not valid JSON"
        if not isinstance(parsed, dict):
            return None, "body must be a JSON object"
        return parsed, None

    def _route(self, method: str):
        payload = None
        if method in ("POST", "PUT"):
            payload, err = self._read_json()
            if err == "payload too large":
                return self._json(413, {"error": "payload_too_large"})
            if err:
                return self._json(400, {"error": "bad_request", "details": err})
        status, out = dispatch(method, self.path, payload)
        return self._json(status, out)

    def do_GET(self):
        self._route("GET")

    def do_POST(self):
        self._route("POST")

    def do_PUT(self):
        self._route("POST")


def main():
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "8080"))
    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    print(f"vera bot listening on http://{host}:{port}  "
          f"(contexts: {STORE.counts()})", file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down", file=sys.stderr)
        server.shutdown()


if __name__ == "__main__":
    main()
