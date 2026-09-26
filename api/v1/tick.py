"""Vercel function: /v1/tick (also reachable as /api/v1/tick)."""

from vera.wsgi import make_app

app = make_app("/v1/tick")
