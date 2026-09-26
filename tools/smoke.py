"""End-to-end smoke test against a running bot (local or Vercel).

    python3 tools/smoke.py http://localhost:8080
    python3 tools/smoke.py https://<your-app>.vercel.app

Checks: healthz, metadata, idempotent context push, tick produces exactly one
action, a second tick is suppressed, a reply is answered, teardown wipes state.
Exit code 0 = all good.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

CHECKS = []


def call(base, method, path, payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(base.rstrip("/") + path, data=data,
                                 method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read() or b"{}")
        except ValueError:
            return exc.code, {}
    except Exception as exc:
        return None, {"error": str(exc)}


def check(label, ok, detail=""):
    CHECKS.append(ok)
    print(f"{'PASS' if ok else 'FAIL'}  {label}" + (f" — {detail}" if detail else ""))


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    base = sys.argv[1]

    status, body = call(base, "GET", "/v1/healthz")
    check("healthz reachable", status == 200 and body.get("status") == "ok",
          f"HTTP {status} {body}")
    if status != 200:
        print("\nBot unreachable — fix this before the rest matters.")
        return 1

    status, meta = call(base, "GET", "/v1/metadata")
    check("metadata has team + model",
          status == 200 and bool(meta.get("team_name")) and bool(meta.get("model")),
          str(meta.get("team_name")))

    category = json.load(open(os.path.join(ROOT, "dataset", "categories",
                                            "dentists.json"), encoding="utf-8"))
    merchants = json.load(open(os.path.join(ROOT, "dataset", "merchants_seed.json"),
                               encoding="utf-8"))["merchants"]
    triggers = json.load(open(os.path.join(ROOT, "dataset", "triggers_seed.json"),
                              encoding="utf-8"))["triggers"]
    merchant = next(m for m in merchants if m["merchant_id"] == "m_001_drmeera_dentist_delhi")
    trigger = next(t for t in triggers if t["id"] == "trg_002_compliance_dci_radiograph")

    for scope, item, cid in (
            ("category", category, category.get("slug") or "dentists"),
            ("merchant", merchant, merchant["merchant_id"]),
            ("trigger", trigger, trigger["id"])):
        status, body = call(base, "POST", "/v1/context",
                            {"scope": scope, "context_id": cid, "version": 1,
                             "payload": item})
        if scope == "category":
            check("push category context", status == 200 and body.get("accepted"),
                  str(body))
        if scope == "merchant":
            status2, body2 = call(base, "POST", "/v1/context",
                                  {"scope": scope, "context_id": cid, "version": 1,
                                   "payload": item})
            check("idempotent re-push (same version)",
                  status2 == 200 and body2.get("accepted"), str(body2))

    status, body = call(base, "POST", "/v1/tick",
                        {"now": "2026-04-26T10:30:00Z",
                         "available_triggers": [trigger["id"]]})
    actions = body.get("actions") or []
    check("tick returns exactly one action", status == 200 and len(actions) == 1,
          f"{len(actions)} action(s)")
    if not actions:
        print(json.dumps(body, indent=1)[:800])
        return 1
    action = actions[0]
    check("action is fully populated",
          all(action.get(k) for k in ("body", "cta", "send_as", "suppression_key",
                                      "rationale", "conversation_id",
                                      "trigger_id", "merchant_id")))
    check("body has no URL", "http" not in action["body"].lower())
    print(f"\n  message: {action['body']}\n  cta: {action['cta']} | "
          f"send_as: {action['send_as']}")

    status, body = call(base, "POST", "/v1/tick",
                        {"now": "2026-04-26T10:35:00Z",
                         "available_triggers": [trigger["id"]]})
    check("second tick is suppressed", status == 200 and not body.get("actions"),
          f"{len(body.get('actions') or [])} action(s)")

    status, reply = call(base, "POST", "/v1/reply", {
        "conversation_id": action["conversation_id"],
        "merchant_id": action["merchant_id"], "customer_id": None,
        "from_role": "merchant", "message": "Ok lets do it. Whats next?",
        "received_at": "2026-04-26T10:45:00Z", "turn_number": 2})
    check("reply answered with send/wait/end",
          status == 200 and reply.get("action") in ("send", "wait", "end"),
          str(reply.get("action")))
    if reply.get("action") == "send":
        print(f"  reply:   {reply['body'][:220]}")

    status, reply = call(base, "POST", "/v1/reply", {
        "conversation_id": action["conversation_id"],
        "merchant_id": action["merchant_id"], "customer_id": None,
        "from_role": "merchant", "message": "stop spamming me",
        "received_at": "2026-04-26T10:50:00Z", "turn_number": 3})
    check("hostile reply ends the thread",
          status == 200 and reply.get("action") == "end", str(reply.get("action")))

    status, body = call(base, "POST", "/v1/teardown", {})
    status2, health = call(base, "GET", "/v1/healthz")
    check("teardown wipes state",
          status == 200 and health.get("contexts_loaded",
                                       {}).get("category") == 0, str(health))

    passed = sum(CHECKS)
    print(f"\n{passed}/{len(CHECKS)} checks passed")
    return 0 if passed == len(CHECKS) else 1


if __name__ == "__main__":
    sys.exit(main())
