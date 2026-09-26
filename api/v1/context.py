"""Vercel function: /v1/context (also reachable as /api/v1/context)."""

from vera.wsgi import make_app

app = make_app("/v1/context")
