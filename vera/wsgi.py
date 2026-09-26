"""WSGI adapter over vera.api.dispatch — used by the Vercel Python runtime.

    from vera.wsgi import make_app
    app = make_app("/v1/context")     # endpoint fixed per function file

Each api/ function hardcodes its route, so the app works whether Vercel reaches
it directly (/api/v1/context) or through a rewrite (/v1/context).
"""

from __future__ import annotations

import json
from http import client as _http_client

from .api import MAX_BODY_BYTES, dispatch


def make_app(endpoint=None):
    def app(environ, start_response):
        method = (environ.get("REQUEST_METHOD") or "GET").upper()
        path = endpoint or environ.get("PATH_INFO") or "/"
        if path.startswith("/api/") or path == "/api":
            path = path[4:] or "/"

        body = None
        error = None
        if method in ("POST", "PUT"):
            try:
                length = int(environ.get("CONTENT_LENGTH") or 0)
            except (TypeError, ValueError):
                length = -1
            if length < 0:
                error = (400, {"error": "bad_request",
                               "details": "invalid content-length"})
            elif length > MAX_BODY_BYTES:
                error = (413, {"error": "payload_too_large"})
            else:
                stream = environ.get("wsgi.input")
                raw = stream.read(length) if (stream and length) else b"{}"
                try:
                    parsed = json.loads(raw.decode("utf-8") or "{}")
                    if not isinstance(parsed, dict):
                        raise ValueError("body must be a JSON object")
                    body = parsed
                except (ValueError, UnicodeDecodeError) as exc:
                    error = (400, {"error": "bad_request", "details": str(exc)})

        status, payload = error if error else dispatch(method, path, body)
        data = json.dumps(payload).encode("utf-8")
        phrase = _http_client.responses.get(status, "OK")
        start_response(f"{status} {phrase}",
                       [("Content-Type", "application/json; charset=utf-8"),
                        ("Content-Length", str(len(data)))])
        return [data]

    return app
