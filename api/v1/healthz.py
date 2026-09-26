"""Vercel function: /v1/healthz (also reachable as /api/v1/healthz)."""

from vera.wsgi import make_app

app = make_app("/v1/healthz")
