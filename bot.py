"""Vera bot — the magicpin challenge HTTP server (stdlib only).

    python3 bot.py            # listens on $PORT (default 8080)

Endpoints
    GET  /v1/healthz    liveness + context counts
    GET  /v1/metadata   bot identity
    POST /v1/context    idempotent context push  (scope, context_id, version, payload)
    POST /v1/tick       proactive sends for the triggers the judge marks active
    POST /v1/reply      synchronous multi-turn reply
    POST /v1/teardown   wipe all state (end of test)
"""

from __future__ import annotations

import datetime
import json
import os
import re
import sys
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from vera.compose import compose
from vera.ground import customer_facts, has_consent, trigger_facts
from vera.reply_engine import respond
from vera.store import Store
from vera.util import DEFAULT_NOW, stable_hash, squeeze
from vera.voice import sanitize

MAX_BODY_BYTES = 500_000          # brief: /v1/context payload cap 500 KB
TICK_CAP = 12                     # brief: hard cap 20 actions/tick, we stay well under

METADATA = {
    "team_name": os.environ.get("VERA_TEAM_NAME", "Team Vera"),
    "team_members": [m.strip() for m in os.environ.get("VERA_TEAM_MEMBERS", "").split(",")
                     if m.strip()],
    "model": os.environ.get("VERA_MODEL", "deterministic-python-rule-engine"),
    "approach": ("stateless deterministic composer (trigger -> grounded anchor -> "
                 "single CTA) + stateful multi-turn reply engine; no LLM calls, "
                 "no randomness, no clock"),
    "contact_email": os.environ.get("VERA_CONTACT_EMAIL", ""),
    "version": "1.0.0",
    "submitted_at": "2026-04-26T08:00:00Z",
}

STORE = Store()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _slugify(text: str) -> str:
    return re.sub(r"[^a-zA-Z0-9]+", "_", str(text or "")).strip("_")[:60] or "conv"


def _conv_id_for(trigger_id: str) -> str:
    return f"conv_{_slugify(trigger_id)}"


# ---------------------------------------------------------------------------
# Endpoint logic
# ---------------------------------------------------------------------------

def handle_context(body: dict) -> tuple:
    """-> (http_status, response_dict)"""
    scope = body.get("scope")
    context_id = body.get("context_id")
    if scope not in ("category", "merchant", "customer", "trigger"):
        return 400, {"accepted": False, "reason": "invalid_scope",
                     "details": f"scope must be one of category|merchant|customer|trigger, "
                                f"got {scope!r}"}
    if not isinstance(context_id, str) or not context_id:
        return 400, {"accepted": False, "reason": "invalid_scope",
                     "details": "context_id must be a non-empty string"}
    raw_version = body.get("version")
    try:
        version = 1 if raw_version is None else int(raw_version)
    except (TypeError, ValueError):
        return 400, {"accepted": False, "reason": "invalid_scope",
                     "details": "version must be an integer"}

    status, extra = STORE.put_context(scope, context_id, version, body.get("payload"))
    if status == "stale":
        return 409, {"accepted": False, "reason": "stale_version",
                     "current_version": int(extra)}
    if status == "invalid":
        return 400, {"accepted": False, "reason": "invalid_scope", "details": extra}
    ack = "ack_{:08x}".format(stable_hash(f"{scope}:{context_id}:{version}") & 0xFFFFFFFF)
    return 200, {"accepted": True, "ack_id": ack, "stored_at": _now_iso()}


def handle_tick(body: dict) -> dict:
    now = str(body.get("now") or DEFAULT_NOW)
    hints = body.get("available_triggers")
    trigger_ids = STORE.ids("trigger") if hints is None else [str(t) for t in hints]

    candidates = []
    for tid in trigger_ids:
        trigger = STORE.get("trigger", tid)
        if not isinstance(trigger, dict):
            continue

        tf = trigger_facts(trigger)
        real_id = tf["id"] or tid
        suppression = tf["suppression"] or real_id
        if STORE.is_suppressed(suppression):
            continue

        merchant_id = tf["merchant_id"]
        customer_id = tf["customer_id"]
        merchant = STORE.get("merchant", merchant_id) or {}
        customer = STORE.get("customer", customer_id) if customer_id else None
        if not merchant and not customer:
            # Context not pushed yet — leave the trigger unsuppressed for a later tick.
            continue

        if customer is not None and not has_consent(customer_facts(customer)):
            STORE.mark_suppressed(suppression)
            continue
        if tf["scope"] == "customer" and customer is None:
            continue

        category = STORE.get("category", (merchant or {}).get("category_slug")) or {}
        message = compose(category, merchant, trigger, customer, now=now)
        if message.get("skipped"):
            if "opt-in" in str(message.get("rationale", "")):
                STORE.mark_suppressed(suppression)
            continue
        if not message.get("body"):
            continue
        if STORE.already_sent(message["body"]):
            STORE.mark_suppressed(suppression)
            continue

        candidates.append((int(tf["urgency"] or 1), real_id, suppression, message,
                           merchant_id, customer_id, trigger, category))

    # most urgent first, deterministic tie-break on trigger id
    candidates.sort(key=lambda c: (-c[0], c[1]))
    actions = []
    for _, tid, suppression, message, merchant_id, customer_id, trigger, category in candidates[:TICK_CAP]:
        conv_id = _conv_id_for(tid)
        body_text = sanitize(message["body"], category) or message["body"]

        conv = STORE.get_conv(conv_id)
        if conv is None:
            conv = STORE.new_conv(conv_id, merchant_id=merchant_id,
                                  customer_id=customer_id, trigger_id=tid,
                                  kind=(trigger.get("kind") or ""), now=now)
        conv["plan"] = message.get("plan")
        conv["send_as"] = message.get("send_as")
        conv.setdefault("sent", []).append(body_text)

        STORE.mark_suppressed(suppression)
        STORE.record_sent(body_text)

        actions.append({
            "conversation_id": conv_id,
            "merchant_id": merchant_id or None,
            "customer_id": customer_id or None,
            "send_as": message.get("send_as") or "vera",
            "trigger_id": tid,
            "template_name": message.get("template_name") or "",
            "template_params": message.get("template_params") or [],
            "body": body_text,
            "cta": message.get("cta") or "none",
            "suppression_key": suppression,
            "rationale": message.get("rationale") or "",
        })
    return {"actions": actions}


def handle_reply(body: dict) -> dict:
    conv_id = str(body.get("conversation_id") or f"conv_{stable_hash(json.dumps(body, sort_keys=True))}")
    merchant_id = body.get("merchant_id")
    customer_id = body.get("customer_id")
    role = str(body.get("from_role") or "merchant")
    turn = int(body.get("turn_number") or 1)

    conv = STORE.get_conv(conv_id)
    if conv is None:
        conv = STORE.new_conv(conv_id, merchant_id=merchant_id, customer_id=customer_id)
    conv["turns"] = max(int(conv.get("turns") or 0), turn)

    message = respond(conv, body.get("message"), STORE, from_role=role, turn_number=turn)

    action = message.get("action")
    if action == "send":
        merchant = STORE.get("merchant", conv.get("merchant_id") or merchant_id) or {}
        category = STORE.get("category", merchant.get("category_slug")) or {}
        text = sanitize(str(message.get("body") or ""), category)
        if text:
            message["body"] = text
            conv["sent"] = (conv.get("sent") or [])
            if conv["sent"] and conv["sent"][-1] != text:
                conv["sent"][-1] = text
        STORE.record_sent(message["body"])
        out = {"action": "send", "body": message["body"],
               "cta": message.get("cta") or "none",
               "rationale": message.get("rationale") or ""}
    elif action == "wait":
        out = {"action": "wait",
               "wait_seconds": int(message.get("wait_seconds") or 3600),
               "rationale": message.get("rationale") or ""}
    else:
        conv["ended"] = True
        out = {"action": "end", "rationale": message.get("rationale") or ""}

    conv["last_action"] = out["action"]
    STORE.put_conv(conv_id, conv)
    return out


# ---------------------------------------------------------------------------
# HTTP plumbing
# ---------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "Vera/1.0"

    def log_message(self, fmt, *args):
        if os.environ.get("VERA_QUIET") != "1":
            sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _json(self, status: int, payload: dict):
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _read_json(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if length < 0:
            return None, "invalid content-length"
        if length > MAX_BODY_BYTES:
            return None, "payload too large"
        raw = self.rfile.read(length) if length else b"{}"
        try:
            parsed = json.loads(raw.decode("utf-8") or "{}")
        except (ValueError, UnicodeDecodeError):
            return None, "body is not valid JSON"
        if not isinstance(parsed, dict):
            return None, "body must be a JSON object"
        return parsed, None

    def _route(self, method: str):
        path = self.path.split("?", 1)[0].rstrip("/") or "/"

        if method == "GET":
            if path == "/v1/healthz":
                return self._json(200, {"status": "ok",
                                        "uptime_seconds": STORE.uptime(),
                                        "contexts_loaded": STORE.counts()})
            if path == "/v1/metadata":
                return self._json(200, METADATA)
            if path in ("/", "/v1"):
                return self._json(200, {"bot": METADATA.get("team_name"),
                                        "endpoints": ["/v1/healthz", "/v1/metadata",
                                                      "/v1/context", "/v1/tick",
                                                      "/v1/reply", "/v1/teardown"]})
            return self._json(404, {"error": "not_found", "path": path})

        if method == "POST":
            if path not in ("/v1/context", "/v1/tick", "/v1/reply", "/v1/teardown"):
                return self._json(404, {"error": "not_found", "path": path})
            payload, err = self._read_json()
            if err == "payload too large":
                return self._json(413, {"error": "payload_too_large"})
            if err:
                return self._json(400, {"error": "bad_request", "details": err})
            try:
                if path == "/v1/context":
                    status, out = handle_context(payload)
                    return self._json(status, out)
                if path == "/v1/tick":
                    return self._json(200, handle_tick(payload))
                if path == "/v1/reply":
                    return self._json(200, handle_reply(payload))
                STORE.wipe()
                return self._json(200, {"ok": True, "wiped": True})
            except Exception as exc:  # never let an exception fail the probe
                traceback.print_exc()
                return self._json(500, {"error": "internal_error",
                                        "detail": f"{type(exc).__name__}: {exc}"})

        return self._json(405, {"error": "method_not_allowed"})

    def do_GET(self):
        self._route("GET")

    def do_POST(self):
        self._route("POST")

    def do_PUT(self):
        self._route("POST")


def main():
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "8080"))
    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    print(f"vera bot listening on http://{host}:{port}  "
          f"(contexts: {STORE.counts()})", file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down", file=sys.stderr)
        server.shutdown()


if __name__ == "__main__":
    main()
