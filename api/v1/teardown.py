"""Vercel function: /v1/teardown (also reachable as /api/v1/teardown)."""

from vera.wsgi import make_app

app = make_app("/v1/teardown")
