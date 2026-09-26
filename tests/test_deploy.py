"""Deploy-path tests: the WSGI adapter (Vercel) and the KV-backed store."""

from __future__ import annotations

import fnmatch
import io
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from vera import kv as kvmod  # noqa: E402
from vera.persistent import KVStore  # noqa: E402
from vera.wsgi import make_app  # noqa: E402


class TestWSGIAdapter(unittest.TestCase):
    def call(self, method, path, body=None, raw=None):
        payload = raw if raw is not None else (
            json.dumps(body).encode("utf-8") if body is not None else b"")
        environ = {"REQUEST_METHOD": method, "PATH_INFO": path,
                   "CONTENT_LENGTH": str(len(payload)),
                   "wsgi.input": io.BytesIO(payload)}
        captured = {}

        def start_response(status, headers):
            captured["status"] = status
            captured["headers"] = dict(headers)

        app = make_app()
        chunks = b"".join(app(environ, start_response))
        return int(captured["status"].split()[0]), json.loads(chunks or b"{}")

    def test_healthz_and_metadata(self):
        status, body = self.call("GET", "/v1/healthz")
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "ok")
        self.assertIn("contexts_loaded", body)

        status, body = self.call("GET", "/v1/metadata")
        self.assertEqual(status, 200)
        for key in ("team_name", "team_members", "model", "approach", "version",
                    "submitted_at"):
            self.assertIn(key, body)

    def test_context_tick_reply_over_wsgi(self):
        merchant = {"merchant_id": "m_wsgi", "category_slug": "dentists",
                    "identity": {"owner_first_name": "Meera", "languages": ["en"]}}
        self.call("POST", "/v1/context", {
            "scope": "merchant", "context_id": "m_wsgi", "version": 1,
            "payload": merchant})
        status, body = self.call("POST", "/v1/context", {
            "scope": "merchant", "context_id": "m_wsgi", "version": 1,
            "payload": merchant})
        self.assertEqual(status, 200)
        self.assertTrue(body["accepted"])

        status, body = self.call("GET", "/v1/healthz")
        self.assertEqual(status, 200)
        self.assertGreaterEqual(body["contexts_loaded"]["merchant"], 1)

        status, body = self.call("POST", "/v1/teardown", {})
        self.assertEqual(status, 200)
        self.assertTrue(body["wiped"])

    def test_api_prefix_is_normalised(self):
        status, body = self.call("GET", "/api/v1/healthz")
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "ok")

    def test_bad_json_and_unknown_route(self):
        status, body = self.call("POST", "/v1/tick", raw=b"{not json")
        self.assertEqual(status, 400)
        status, body = self.call("GET", "/v1/nope")
        self.assertEqual(status, 404)

    def test_payload_too_large(self):
        status, body = self.call("POST", "/v1/context", raw=b"0" * (600 * 1024))
        self.assertEqual(status, 413)


class TestKVStoreAcrossInstances(unittest.TestCase):
    """Two KVStore instances with empty memory = two cold serverless instances."""

    def setUp(self):
        self.data = {}
        self._original = {name: getattr(kvmod, name) for name in
                          ("available", "config", "get", "set", "incr",
                           "delete", "keys", "get_many")}
        kvmod.available = lambda: True
        kvmod.config = lambda: ("http://kv.test", "token")
        kvmod.get = lambda key: (self.data.get(key)
                                 if isinstance(self.data.get(key), str)
                                 else json.dumps(self.data[key])
                                 if key in self.data else None)
        kvmod.set = lambda key, value, ttl=None: (self.data.__setitem__(key, str(value))
                                                  or True)

        def incr(key, ttl=None):
            value = int(self.data.get(key) or 0) + 1
            self.data[key] = str(value)
            return value
        kvmod.incr = incr

        def delete(*keys):
            return sum(1 for k in keys if self.data.pop(k, None) is not None)
        kvmod.delete = delete

        def keys(pattern):
            return [k for k in list(self.data) if fnmatch.fnmatch(k, pattern)]
        kvmod.keys = keys
        kvmod.get_many = lambda key_list: [kvmod.get(k) for k in key_list]

    def tearDown(self):
        for name, value in self._original.items():
            setattr(kvmod, name, value)

    def test_contexts_survive_a_cold_start(self):
        first = KVStore()
        self.assertTrue(first.kv_on)
        status, extra = first.put_context("merchant", "m_x", 1, {"merchant_id": "m_x"})
        self.assertEqual((status, extra), ("ok", 1))
        status, extra = first.put_context("merchant", "m_x", 1, {"merchant_id": "m_x"})
        self.assertEqual((status, extra), ("ok", 1))

        second = KVStore()          # fresh memory, same KV
        self.assertEqual(second.get("merchant", "m_x"), {"merchant_id": "m_x"})
        self.assertIn("m_x", second.ids("merchant"))
        self.assertEqual(second.counts()["merchant"], 1)
        status, extra = second.put_context("merchant", "m_x", 0, {})
        self.assertEqual(status, "stale")
        self.assertEqual(extra, 1)
        status, _ = second.put_context("merchant", "m_x", 2, {"merchant_id": "m_x",
                                                             "v": 2})
        self.assertEqual(status, "ok")
        self.assertEqual(second.get("merchant", "m_x").get("v"), 2)

    def test_conversations_and_antispam_survive(self):
        first = KVStore()
        first.new_conv("conv_a", merchant_id="m_a")
        conv = first.get_conv("conv_a")
        conv["sent"] = ["hello"]
        first.put_conv("conv_a", conv)
        first.mark_suppressed("sup_key")
        first.record_sent("some body")
        self.assertEqual(first.bump_auto_reply("m_a", "away"), 1)

        second = KVStore()
        conv = second.get_conv("conv_a")
        self.assertIsNotNone(conv)
        self.assertEqual(conv["sent"], ["hello"])
        self.assertTrue(second.is_suppressed("sup_key"))
        self.assertTrue(second.already_sent("some body"))
        self.assertEqual(second.bump_auto_reply("m_a", "away"), 2)

    def test_wipe_clears_kv(self):
        store = KVStore()
        store.put_context("trigger", "t_1", 1, {"id": "t_1"})
        store.mark_suppressed("sup")
        store.wipe()
        self.assertIsNone(store.get("trigger", "t_1"))
        self.assertFalse(store.is_suppressed("sup"))
        self.assertEqual(store.counts(), {"category": 0, "merchant": 0,
                                          "customer": 0, "trigger": 0})


if __name__ == "__main__":
    unittest.main(verbosity=2)
