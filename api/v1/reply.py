"""Vercel function: /v1/reply (also reachable as /api/v1/reply)."""

from vera.wsgi import make_app

app = make_app("/v1/reply")
