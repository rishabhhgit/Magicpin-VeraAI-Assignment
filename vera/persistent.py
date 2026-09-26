"""KV-backed Store for serverless deploys (Vercel): memory acts as a per-instance
cache, Redis/Upstash is the source of truth so a cold instance still sees the
contexts, suppression keys and conversations written by an earlier request.

Every KV call falls back to process-local memory when unavailable — the bot keeps
working standalone (and in tests) with no env vars at all.
"""

from __future__ import annotations

import json
import sys

from . import kv
from .store import SCOPES, Store
from .util import stable_hash

TTL = 604800          # 7 days — well past any test window
PREFIX = "vera"


def _ctx_key(scope, context_id):
    return f"{PREFIX}:ctx:{scope}:{context_id}"


def _ctx_parts(key):
    _, _, scope, context_id = key.split(":", 3)
    return scope, context_id


def _conv_key(conv_id):
    return f"{PREFIX}:conv:{conv_id}"


def _auto_key(merchant_id, normalised):
    return f"{PREFIX}:auto:{merchant_id}:{stable_hash(normalised) & 0xFFFFFFFF:08x}"


def _sup_key(key):
    return f"{PREFIX}:sup:{stable_hash(str(key)) & 0xFFFFFFFF:08x}"


def _sent_key(body):
    return f"{PREFIX}:sent:{stable_hash(str(body)) & 0xFFFFFFFF:08x}"


def _decode(entry_json):
    try:
        entry = json.loads(entry_json)
    except (TypeError, ValueError):
        return None
    return entry if isinstance(entry, dict) else None


class KVStore(Store):
    def __init__(self):
        super().__init__()
        self.kv_on = kv.available()
        if self.kv_on:
            print("vera: KV persistence enabled", file=sys.stderr)

    # --- contexts ------------------------------------------------------------

    def _remote_ctx(self, scope, context_id):
        if not self.kv_on:
            return None
        return _decode(kv.get(_ctx_key(scope, context_id)))

    def put_context(self, scope, context_id, version, payload):
        if scope not in SCOPES:
            return "invalid", "unknown scope"
        if not isinstance(context_id, str) or not context_id:
            return "invalid", "context_id required"
        try:
            version = int(version) if version is not None else 1
        except (TypeError, ValueError):
            return "invalid", "version must be an integer"
        with self.lock:
            current = self.contexts.get(scope, {}).get(context_id)
        if current is None:
            current = self._remote_ctx(scope, context_id)
        if current and current["version"] > version:
            return "stale", current["version"]
        if current and current["version"] == version:
            with self.lock:
                self.contexts[scope].setdefault(context_id, current)
            return "ok", version
        entry = {"version": version, "payload": payload or {}}
        with self.lock:
            self.contexts[scope][context_id] = entry
        if self.kv_on:
            kv.set(_ctx_key(scope, context_id), json.dumps(entry, ensure_ascii=False), TTL)
        return "ok", version

    def get(self, scope, context_id):
        if not context_id:
            return None
        with self.lock:
            entry = self.contexts.get(scope, {}).get(context_id)
        if entry:
            return entry["payload"]
        entry = self._remote_ctx(scope, context_id)
        if entry:
            with self.lock:
                self.contexts.setdefault(scope, {})[context_id] = entry
            return entry["payload"]
        return None

    def ids(self, scope):
        with self.lock:
            found = set(self.contexts.get(scope, {}))
        if self.kv_on:
            for key in kv.keys(f"{PREFIX}:ctx:{scope}:*"):
                try:
                    found.add(_ctx_parts(key)[1])
                except (ValueError, IndexError):
                    continue
        return sorted(found)

    def counts(self):
        return {s: len(self.ids(s)) for s in SCOPES}

    # --- conversations -------------------------------------------------------

    def _remote_conv(self, conv_id):
        if not self.kv_on:
            return None
        raw = kv.get(_conv_key(conv_id))
        try:
            conv = json.loads(raw) if isinstance(raw, str) else None
        except (TypeError, ValueError):
            return None
        return conv if isinstance(conv, dict) else None

    def get_conv(self, conv_id):
        with self.lock:
            conv = self.conversations.get(conv_id)
        if conv:
            return conv
        conv = self._remote_conv(conv_id)
        if conv:
            with self.lock:
                self.conversations[conv_id] = conv
        return conv

    def put_conv(self, conv_id, conv):
        with self.lock:
            self.conversations[conv_id] = conv
        if self.kv_on:
            kv.set(_conv_key(conv_id), json.dumps(conv, ensure_ascii=False, default=str), TTL)
        return conv

    def new_conv(self, conv_id, **fields):
        conv = super().new_conv(conv_id, **fields)
        if self.kv_on:
            kv.set(_conv_key(conv_id), json.dumps(conv, ensure_ascii=False, default=str), TTL)
        return conv

    def last_conv_for(self, merchant_id=None, customer_id=None):
        with self.lock:
            best = None
            for conv in self.conversations.values():
                if self._conv_matches(conv, merchant_id, customer_id):
                    if best is None or conv.get("opened_at", 0) >= best.get("opened_at", 0):
                        best = conv
        if best is not None or not self.kv_on:
            return best
        for key in kv.keys(f"{PREFIX}:conv:*"):
            conv = self._remote_conv(key.split(":", 2)[-1])
            if conv and self._conv_matches(conv, merchant_id, customer_id):
                with self.lock:
                    self.conversations.setdefault(conv.get("conversation_id", key), conv)
                if best is None or conv.get("opened_at", 0) >= best.get("opened_at", 0):
                    best = conv
        return best

    @staticmethod
    def _conv_matches(conv, merchant_id, customer_id):
        if merchant_id and conv.get("merchant_id") != merchant_id:
            return False
        if customer_id and conv.get("customer_id") != customer_id:
            return False
        return True

    # --- anti-spam -----------------------------------------------------------

    def bump_auto_reply(self, merchant_id, normalised) -> int:
        key = _auto_key(merchant_id, normalised)
        with self.lock:
            base = self.auto_counts.get((merchant_id or "", normalised or ""), 0)
        value = kv.incr(key, TTL) if self.kv_on else 0
        value = max(value, base + 1)
        with self.lock:
            self.auto_counts[(merchant_id or "", normalised or "")] = value
        return value

    def is_suppressed(self, key) -> bool:
        if not key:
            return False
        with self.lock:
            if key in self.suppressed:
                return True
        if not self.kv_on:
            return False
        found = kv.get(_sup_key(key)) is not None
        if found:
            with self.lock:
                self.suppressed.add(key)
        return found

    def mark_suppressed(self, key):
        if not key:
            return
        with self.lock:
            self.suppressed.add(key)
        if self.kv_on:
            kv.set(_sup_key(key), "1", TTL)

    def already_sent(self, body) -> bool:
        if not body:
            return False
        with self.lock:
            if body in self.sent_bodies:
                return True
        if not self.kv_on:
            return False
        found = kv.get(_sent_key(body)) is not None
        if found:
            with self.lock:
                self.sent_bodies.add(body)
        return found

    def record_sent(self, body):
        if not body:
            return
        with self.lock:
            self.sent_bodies.add(body)
        if self.kv_on:
            kv.set(_sent_key(body), "1", TTL)

    # --- lifecycle -----------------------------------------------------------

    def wipe(self):
        with self.lock:
            super().wipe()
        if self.kv_on:
            for pattern in (f"{PREFIX}:ctx:*", f"{PREFIX}:conv:*", f"{PREFIX}:sup:*",
                            f"{PREFIX}:sent:*", f"{PREFIX}:auto:*"):
                found = kv.keys(pattern)
                if found:
                    kv.delete(*found)
