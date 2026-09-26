"""Vercel function: /v1/metadata (also reachable as /api/v1/metadata)."""

from vera.wsgi import make_app

app = make_app("/v1/metadata")
