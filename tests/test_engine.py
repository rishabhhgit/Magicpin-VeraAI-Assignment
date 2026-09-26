"""Engine + reply + HTTP contract tests for the Vera bot.

    python3 -m unittest discover -s tests -v

Stdlib only. HTTP tests spin the real server up on an ephemeral port.
"""

from __future__ import annotations

import datetime
import glob
import json
import os
import sys
import threading
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("VERA_STRICT", "1")

from vera.compose import compose                      # noqa: E402
from vera.ground import customer_facts, has_consent   # noqa: E402
from vera.reply_engine import respond                 # noqa: E402
from vera.store import Store                          # noqa: E402
from vera.voice import HARD_TABOOS, taboos_for        # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NOW = "2026-04-26T10:30:00Z"

QUALIFYING = ["would you", "do you", "can you tell", "what if", "how about"]
ACTIONING = ["done", "sending", "draft", "here", "confirm", "proceed", "next"]


def _load(path, key):
    with open(os.path.join(ROOT, "dataset", path), encoding="utf-8") as fh:
        data = json.load(fh)
    return data.get(key, data)


CATEGORIES = {}
for _f in sorted(glob.glob(os.path.join(ROOT, "dataset", "categories", "*.json"))):
    with open(_f, encoding="utf-8") as fh:
        _c = json.load(fh)
    CATEGORIES[_c.get("slug", _f)] = _c

MERCHANTS = {m["merchant_id"]: m for m in _load("merchants_seed.json", "merchants")}
CUSTOMERS = {c["customer_id"]: c for c in _load("customers_seed.json", "customers")}
TRIGGERS = _load("triggers_seed.json", "triggers")


def compose_all(now=NOW):
    out = []
    for t in TRIGGERS:
        m = MERCHANTS.get(t.get("merchant_id")) or {}
        c = CUSTOMERS.get(t.get("customer_id")) if t.get("customer_id") else None
        cat = CATEGORIES.get(m.get("category_slug")) or \
            CATEGORIES.get((t.get("payload") or {}).get("category")) or {}
        out.append((t, m, c, cat, compose(cat, m, t, c, now=now)))
    return out


# ---------------------------------------------------------------------------
# Composer
# ---------------------------------------------------------------------------

class TestComposer(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rows = compose_all()

    def test_every_trigger_produces_a_message(self):
        for t, _m, _c, _cat, msg in self.rows:
            self.assertFalse(msg.get("skipped"), f"{t['id']} skipped: {msg['rationale']}")
            self.assertTrue(msg["body"].strip(), t["id"])

    def test_deterministic(self):
        for t, m, c, cat, first in self.rows:
            again = compose(cat, m, t, c, now=NOW)
            self.assertEqual(first["body"], again["body"], t["id"])
            self.assertEqual(first["rationale"], again["rationale"], t["id"])

    def test_no_urls_no_taboos(self):
        for t, _m, _c, cat, msg in self.rows:
            body = msg["body"].lower()
            self.assertNotIn("http", body, t["id"])
            self.assertNotIn("www.", body, t["id"])
            banned = [w.lower() for w in HARD_TABOOS + taboos_for(cat)]
            for word in banned:
                self.assertNotIn(word, body, f"{t['id']} uses taboo '{word}'")

    def test_specificity_and_shape(self):
        with_digits = 0
        for t, _m, _c, _cat, msg in self.rows:
            self.assertLessEqual(len(msg["body"]), 700, t["id"])
            self.assertIn(msg["cta"],
                          ("binary_yes_no", "open_ended", "multi_choice_slot", "none"))
            self.assertTrue(msg["suppression_key"], t["id"])
            self.assertIn(msg["send_as"], ("vera", "merchant_on_behalf"))
            self.assertTrue(len(msg["rationale"]) > 20, t["id"])
            if any(ch.isdigit() for ch in msg["body"]):
                with_digits += 1
        self.assertGreaterEqual(with_digits / len(self.rows), 0.8)

    def test_send_as_matches_scope(self):
        for t, _m, _c, _cat, msg in self.rows:
            if t.get("scope") == "customer" or t.get("customer_id"):
                self.assertEqual(msg["send_as"], "merchant_on_behalf", t["id"])
            else:
                self.assertEqual(msg["send_as"], "vera", t["id"])

    def test_repeat_send_is_suppressed_by_key(self):
        seen = {}
        for t, _m, _c, _cat, msg in self.rows:
            key = msg["suppression_key"]
            self.assertNotIn(key, seen, f"duplicate suppression key {key}")
            seen[key] = t["id"]

    def test_consent_gate(self):
        payload = {"customer_id": "c_x", "merchant_id": "m_001_drmeera_dentist_delhi",
                   "identity": {"name": "NoConsent", "language_pref": "english"},
                   "relationship": {}, "state": "lapsed_soft",
                   "consent": {"opted_in_at": None, "scope": []}}
        self.assertFalse(has_consent(customer_facts(payload)))
        t = {"id": "trg_test", "scope": "customer", "kind": "customer_lapsed_soft",
             "merchant_id": "m_001_drmeera_dentist_delhi", "customer_id": "c_x",
             "payload": {}, "urgency": 2, "suppression_key": "test:consent"}
        msg = compose(CATEGORIES["dentists"], MERCHANTS["m_001_drmeera_dentist_delhi"],
                      t, payload, now=NOW)
        self.assertTrue(msg.get("skipped"))

    def test_future_now_does_not_break_anything(self):
        # the judge sends real wall-clock `now` — expired triggers must still compose
        rows = compose_all(now=datetime.datetime.utcnow().isoformat() + "Z")
        for t, _m, _c, _cat, msg in rows:
            self.assertTrue(msg["body"].strip(), t["id"])


# ---------------------------------------------------------------------------
# Reply engine
# ---------------------------------------------------------------------------

class TestReplyEngine(unittest.TestCase):
    def setUp(self):
        self.store = Store()

    def _conv(self, conv_id="conv_t", **fields):
        base = {"conversation_id": conv_id, "merchant_id": "m_001_drmeera_dentist_delhi",
                "customer_id": None, "sent": [], "stage": 0}
        base.update(fields)
        return self.store.put_conv(conv_id, base)

    def test_hostile_ends(self):
        out = respond(self._conv(), "Stop messaging me. This is useless spam.",
                      self.store)
        self.assertEqual(out["action"], "end")

    def test_auto_reply_stages_then_ends(self):
        conv_id = "conv_a"
        msg = "Thank you for contacting us! Our team will respond shortly."
        first = respond(self.store.put_conv(conv_id, {"conversation_id": conv_id,
                                                      "sent": [], "stage": 0}),
                        msg, self.store, turn_number=2)
        self.assertEqual(first["action"], "send")
        second = respond(self.store.put_conv("conv_b", {"conversation_id": "conv_b",
                                                        "sent": [], "stage": 0}),
                         msg, self.store, turn_number=3)
        self.assertEqual(second["action"], "wait")
        third = respond(self.store.put_conv("conv_c", {"conversation_id": "conv_c",
                                                       "sent": [], "stage": 0}),
                        msg, self.store, turn_number=4)
        self.assertEqual(third["action"], "end")

    def test_commitment_switches_to_action_mode(self):
        out = respond(self._conv("conv_i"), "Ok lets do it. Whats next?", self.store)
        self.assertEqual(out["action"], "send")
        body = out["body"].lower()
        self.assertTrue(any(w in body for w in ACTIONING), body)
        for word in QUALIFYING:
            self.assertNotIn(word, body, f"still qualifying: {word} in {body}")

    def test_commitment_without_any_context(self):
        # the simulator posts this with no context pushed at all
        out = respond({"conversation_id": "conv_x", "sent": [], "stage": 0},
                      "Ok lets do it. Whats next?", Store())
        body = out["body"].lower()
        self.assertEqual(out["action"], "send")
        self.assertTrue(any(w in body for w in ACTIONING), body)
        for word in QUALIFYING:
            self.assertNotIn(word, body, body)

    def test_out_of_scope_declines_and_redirects(self):
        conv = self._conv("conv_o", kind="perf_dip")
        out = respond(conv, "Can you file my GST return for me?", self.store)
        self.assertEqual(out["action"], "send")
        low = out["body"].lower()
        self.assertTrue("outside" in low or "help with" in low, low)
        self.assertIn("perf dip", low)   # redirected back to the live thread

    def test_accept_delivers_artifact(self):
        conv = self._conv("conv_d", kind="research_digest",
                          plan={"type": "digest_abstract", "title": "Fluoride recall",
                                "summary": "3-month recall cuts caries 38% better.",
                                "actionable": "Book 3-month recalls for high-risk adults.",
                                "source": "JIDA Oct 2026, p.14"})
        out = respond(conv, "Yes, send me the abstract", self.store)
        self.assertEqual(out["action"], "send")
        self.assertIn("JIDA Oct 2026", out["body"])

    def test_deferral_waits(self):
        out = respond(self._conv("conv_w"), "Not now, call next week", self.store)
        self.assertEqual(out["action"], "wait")
        self.assertGreaterEqual(out["wait_seconds"], 3600)

    def test_question_answered_from_context(self):
        self.store.put_context("category", "dentists", 1, CATEGORIES["dentists"])
        self.store.put_context("merchant", "m_001_drmeera_dentist_delhi", 1,
                               MERCHANTS["m_001_drmeera_dentist_delhi"])
        out = respond(self._conv("conv_q"), "What are your prices for cleaning?",
                      self.store)
        self.assertEqual(out["action"], "send")
        self.assertTrue(any(ch.isdigit() for ch in out["body"]), out["body"])
        self.assertIn("₹", out["body"])

    def test_question_without_context_does_not_invent_numbers(self):
        out = respond({"conversation_id": "conv_q2", "sent": [], "stage": 0},
                      "What are your prices?", Store())
        self.assertEqual(out["action"], "send")
        self.assertNotIn("₹", out["body"])


# ---------------------------------------------------------------------------
# HTTP contract
# ---------------------------------------------------------------------------

class TestHTTPContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import bot
        from http.server import ThreadingHTTPServer
        bot.STORE.wipe()
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), bot.Handler)
        cls.base = "http://127.0.0.1:%d" % cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def _request(self, method, path, payload=None, expect=200):
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                status, body = resp.status, json.loads(resp.read().decode())
        except urllib.error.HTTPError as err:
            status = err.code
            body = json.loads(err.read().decode())
        self.assertEqual(status, expect, f"{method} {path} -> {body}")
        return body

    def test_01_healthz_and_metadata(self):
        health = self._request("GET", "/v1/healthz")
        self.assertEqual(health["status"], "ok")
        self.assertIn("category", health["contexts_loaded"])
        meta = self._request("GET", "/v1/metadata")
        for key in ("team_name", "model", "approach", "version"):
            self.assertIn(key, meta)

    def test_02_context_idempotency_and_conflicts(self):
        payload = MERCHANTS["m_001_drmeera_dentist_delhi"]
        first = self._request("POST", "/v1/context",
                              {"scope": "merchant", "context_id": "m_001",
                               "version": 3, "payload": payload})
        self.assertTrue(first["accepted"])
        repost = self._request("POST", "/v1/context",
                               {"scope": "merchant", "context_id": "m_001",
                                "version": 3, "payload": payload})
        self.assertTrue(repost["accepted"])
        stale = self._request("POST", "/v1/context",
                              {"scope": "merchant", "context_id": "m_001",
                               "version": 2, "payload": payload}, expect=409)
        self.assertEqual(stale["reason"], "stale_version")
        self.assertEqual(stale["current_version"], 3)
        bad = self._request("POST", "/v1/context",
                            {"scope": "nope", "context_id": "x", "version": 1,
                             "payload": {}}, expect=400)
        self.assertEqual(bad["reason"], "invalid_scope")

    def test_03_tick_returns_grounded_actions_then_suppresses(self):
        for slug, cat in CATEGORIES.items():
            self._request("POST", "/v1/context",
                          {"scope": "category", "context_id": slug, "version": 1,
                           "payload": cat})
        for mid, m in MERCHANTS.items():
            self._request("POST", "/v1/context",
                          {"scope": "merchant", "context_id": mid, "version": 1,
                           "payload": m})
        for cid, c in CUSTOMERS.items():
            self._request("POST", "/v1/context",
                          {"scope": "customer", "context_id": cid, "version": 1,
                           "payload": c})
        ids = []
        for t in TRIGGERS:
            ids.append(t["id"])
            self._request("POST", "/v1/context",
                          {"scope": "trigger", "context_id": t["id"], "version": 1,
                           "payload": t})

        out = self._request("POST", "/v1/tick",
                            {"now": datetime.datetime.utcnow().isoformat() + "Z",
                             "available_triggers": ids})
        actions = out["actions"]
        self.assertTrue(actions, "tick produced no actions")
        self.assertLessEqual(len(actions), 20)
        for a in actions:
            for key in ("conversation_id", "merchant_id", "send_as", "trigger_id",
                        "body", "cta", "suppression_key", "rationale"):
                self.assertIn(key, a, f"action missing {key}: {a}")
            self.assertNotIn("http", a["body"].lower())
            self.assertTrue(a["body"].strip())

        again = self._request("POST", "/v1/tick",
                              {"now": datetime.datetime.utcnow().isoformat() + "Z",
                               "available_triggers": ids})
        first_ids = {a["trigger_id"] for a in actions}
        again_ids = {a["trigger_id"] for a in again["actions"]}
        self.assertFalse(first_ids & again_ids,
                         f"sent twice across ticks: {first_ids & again_ids}")

    def test_04_reply_paths(self):
        hostile = self._request("POST", "/v1/reply",
                                {"conversation_id": "conv_http_hostile",
                                 "merchant_id": "m_001_drmeera_dentist_delhi",
                                 "customer_id": None, "from_role": "merchant",
                                 "message": "Stop messaging me. This is useless spam.",
                                 "turn_number": 2})
        self.assertEqual(hostile["action"], "end")

        commit = self._request("POST", "/v1/reply",
                               {"conversation_id": "conv_http_intent",
                                "merchant_id": "m_001_drmeera_dentist_delhi",
                                "customer_id": None, "from_role": "merchant",
                                "message": "Ok lets do it. Whats next?",
                                "turn_number": 2})
        self.assertEqual(commit["action"], "send")
        low = commit["body"].lower()
        self.assertTrue(any(w in low for w in ACTIONING), low)
        for word in QUALIFYING:
            self.assertNotIn(word, low, low)

        auto = self._request("POST", "/v1/reply",
                             {"conversation_id": "conv_http_auto_1",
                              "merchant_id": "m_001_drmeera_dentist_delhi",
                              "customer_id": None, "from_role": "merchant",
                              "message": "Thanks for contacting us, we will respond "
                                         "shortly.",
                              "turn_number": 2})
        self.assertIn(auto["action"], ("send", "wait", "end"))

    def test_05_teardown_wipes_state(self):
        out = self._request("POST", "/v1/teardown", {})
        self.assertTrue(out.get("wiped"))
        health = self._request("GET", "/v1/healthz")
        self.assertEqual(sum(health["contexts_loaded"].values()), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
