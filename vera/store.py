"""In-memory state for the bot: contexts, conversations, suppression.

Deliberately process-local (no disk persistence) — the judge keeps one process
alive for the whole test, and a restart is a legitimate clean slate.
"""

from __future__ import annotations

import threading
import time

SCOPES = ("category", "merchant", "customer", "trigger")


class Store:
    def __init__(self):
        self.lock = threading.RLock()
        self.started_at = time.time()
        # scope -> context_id -> {"version": int, "payload": dict}
        self.contexts = {s: {} for s in SCOPES}
        self.conversations = {}
        self.auto_counts = {}          # (merchant_id, normalised_message) -> hits
        self.suppressed = set()        # suppression keys already acted on
        self.sent_bodies = set()       # every body we ever sent (cross-thread guard)

    # --- contexts ------------------------------------------------------------

    def put_context(self, scope, context_id, version, payload):
        """Idempotent on (scope, context_id, version).

        Returns (status, extra) where status is 'ok' | 'stale' | 'invalid'.
        """
        if scope not in SCOPES:
            return "invalid", "unknown scope"
        if not isinstance(context_id, str) or not context_id:
            return "invalid", "context_id required"
        try:
            version = int(version) if version is not None else 1
        except (TypeError, ValueError):
            return "invalid", "version must be an integer"
        with self.lock:
            bucket = self.contexts[scope]
            current = bucket.get(context_id)
            if current and current["version"] > version:
                return "stale", current["version"]
            if current and current["version"] == version:
                return "ok", version          # idempotent re-post
            bucket[context_id] = {"version": version, "payload": payload or {}}
            return "ok", version

    def get(self, scope, context_id):
        if not context_id:
            return None
        with self.lock:
            entry = self.contexts.get(scope, {}).get(context_id)
            return entry["payload"] if entry else None

    def ids(self, scope):
        with self.lock:
            return list(self.contexts.get(scope, {}).keys())

    def counts(self):
        with self.lock:
            return {s: len(self.contexts[s]) for s in SCOPES}

    # --- conversations -------------------------------------------------------

    def get_conv(self, conv_id):
        with self.lock:
            return self.conversations.get(conv_id)

    def put_conv(self, conv_id, conv):
        with self.lock:
            self.conversations[conv_id] = conv
        return conv

    def new_conv(self, conv_id, **fields):
        conv = {"conversation_id": conv_id, "sent": [], "stage": 0,
                "turns": 0, "opened_at": time.time()}
        conv.update(fields)
        with self.lock:
            self.conversations[conv_id] = conv
        return conv

    def last_conv_for(self, merchant_id=None, customer_id=None):
        """Most recent conversation for a merchant/customer (for thread linking)."""
        with self.lock:
            best = None
            for conv in self.conversations.values():
                if merchant_id and conv.get("merchant_id") != merchant_id:
                    continue
                if customer_id and conv.get("customer_id") != customer_id:
                    continue
                if best is None or conv.get("opened_at", 0) >= best.get("opened_at", 0):
                    best = conv
            return best

    # --- anti-spam -----------------------------------------------------------

    def bump_auto_reply(self, merchant_id, normalised) -> int:
        key = (merchant_id or "", normalised or "")
        with self.lock:
            self.auto_counts[key] = self.auto_counts.get(key, 0) + 1
            return self.auto_counts[key]

    def is_suppressed(self, key) -> bool:
        if not key:
            return False
        with self.lock:
            return key in self.suppressed

    def mark_suppressed(self, key):
        if key:
            with self.lock:
                self.suppressed.add(key)

    def already_sent(self, body) -> bool:
        with self.lock:
            return body in self.sent_bodies

    def record_sent(self, body):
        if body:
            with self.lock:
                self.sent_bodies.add(body)

    # --- lifecycle -----------------------------------------------------------

    def wipe(self):
        with self.lock:
            for s in SCOPES:
                self.contexts[s] = {}
            self.conversations.clear()
            self.auto_counts.clear()
            self.suppressed.clear()
            self.sent_bodies.clear()

    def uptime(self) -> int:
        return int(time.time() - self.started_at)
