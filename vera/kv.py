"""Minimal Redis-over-REST client (Vercel KV / Upstash compatible) — stdlib only.

Configured from any one of:
    KV_REST_API_URL + KV_REST_API_TOKEN            (Vercel KV integration)
    UPSTASH_REDIS_REST_URL + UPSTASH_REDIS_REST_TOKEN
    KV_URL + KV_TOKEN

Every call degrades: if unconfigured or unreachable, callers fall back to
process-local memory (the pre-Vercel behaviour).
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

_TIMEOUT = 10
_failures = 0


def config():
    url = (os.environ.get("KV_REST_API_URL")
           or os.environ.get("UPSTASH_REDIS_REST_URL")
           or os.environ.get("KV_URL") or "")
    token = (os.environ.get("KV_REST_API_TOKEN")
             or os.environ.get("UPSTASH_REDIS_REST_TOKEN")
             or os.environ.get("KV_TOKEN") or "")
    return url.strip(), token.strip()


def available() -> bool:
    return all(config())


def run(*commands, timeout: int = _TIMEOUT):
    """Run one command (["GET", key]) or a pipeline of them; returns the reply.

    Raises on network/protocol failure so callers can fall back to memory.
    """
    url, token = config()
    if not url or not token:
        raise RuntimeError("KV not configured")
    if not commands:
        return None
    payload = commands[0] if len(commands) == 1 else [list(c) for c in commands]
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"), method="POST",
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
    except Exception:
        global _failures
        _failures += 1
        if _failures <= 3:
            import traceback
            traceback.print_exc()
        raise
    return json.loads(raw) if raw else None


def get(key):
    try:
        value = run(["GET", key])
    except Exception:
        return None
    return value if isinstance(value, (str, int, float)) or value is None else value


def set(key, value, ttl: int = None) -> bool:
    cmd = ["SET", key, "" if value is None else str(value)]
    if ttl:
        cmd += ["EX", str(int(ttl))]
    try:
        run(cmd)
        return True
    except Exception:
        return False


def incr(key, ttl: int = None) -> int:
    try:
        value = run(["INCR", key])
        if ttl:
            run(["EXPIRE", key, str(int(ttl))])
        return int(value or 0)
    except Exception:
        return 0


def delete(*keys) -> int:
    keys = [k for k in keys if k]
    if not keys:
        return 0
    try:
        return int(run(["DEL", *keys]) or 0)
    except Exception:
        return 0


def keys(pattern: str) -> list:
    try:
        found = run(["KEYS", pattern])
    except Exception:
        return []
    return [str(k) for k in found] if isinstance(found, list) else []


def get_many(keys_list):
    keys_list = [k for k in keys_list if k]
    if not keys_list:
        return []
    try:
        reply = run(*[["GET", k] for k in keys_list])
    except Exception:
        return [None] * len(keys_list)
    return reply if isinstance(reply, list) and len(reply) == len(keys_list) \
        else [None] * len(keys_list)
