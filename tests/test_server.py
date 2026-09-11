import http.server
import json
import os
import socket
import sqlite3
import tempfile
import threading
import unittest
import urllib.request

from token_dashboard.db import init_db
from token_dashboard.server import build_handler


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "t.db")
        init_db(self.db)
        with sqlite3.connect(self.db) as c:
            c.execute("INSERT INTO messages (uuid, parent_uuid, session_id, project_slug, type, timestamp, model, input_tokens, output_tokens, cache_read_tokens, cache_create_5m_tokens, cache_create_1h_tokens, prompt_text, prompt_chars) VALUES ('u',NULL,'s','p','user','2026-04-19T00:00:00Z',NULL,0,0,0,0,0,'hi',2)")
            c.execute("INSERT INTO messages (uuid, parent_uuid, session_id, project_slug, type, timestamp, model, input_tokens, output_tokens, cache_read_tokens, cache_create_5m_tokens, cache_create_1h_tokens) VALUES ('a','u','s','p','assistant','2026-04-19T00:00:01Z','claude-haiku-4-5',1,1,0,0,0)")
            c.commit()
        self.port = _free_port()
        H = build_handler(self.db, projects_dir="/nonexistent")
        self.httpd = http.server.HTTPServer(("127.0.0.1", self.port), H)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.httpd.shutdown()

    def _get(self, path):
        return urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}").read()

    def test_index_html(self):
        body = self._get("/")
        self.assertIn(b"Token Dashboard", body)

    def test_overview_json(self):
        body = json.loads(self._get("/api/overview"))
        self.assertIn("sessions", body)
        self.assertEqual(body["sessions"], 1)

    def test_prompts_json(self):
        body = json.loads(self._get("/api/prompts?limit=10"))
        self.assertIsInstance(body, list)

    def test_projects_json(self):
        body = json.loads(self._get("/api/projects"))
        self.assertIsInstance(body, list)
        self.assertEqual(body[0]["project_slug"], "p")

    def test_plan_json(self):
        body = json.loads(self._get("/api/plan"))
        self.assertIn("plan", body)
        self.assertIn("pricing", body)

    def test_head_returns_200_not_501(self):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/", method="HEAD")
        with urllib.request.urlopen(req) as resp:
            self.assertEqual(resp.status, 200)
            self.assertEqual(resp.read(), b"")

    def test_head_api_endpoint(self):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/api/overview", method="HEAD")
        with urllib.request.urlopen(req) as resp:
            self.assertEqual(resp.status, 200)
            self.assertEqual(resp.read(), b"")

    def test_keys_status(self):
        body = json.loads(self._get("/api/keys/status"))
        self.assertIn("perplexity", body)
        self.assertIn("anthropic", body)
        self.assertIn("openai", body)

    def test_chat_endpoint(self):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/api/chat",
            data=json.dumps({"model": "sonar", "messages": [{"role": "user", "content": "test"}], "dry_run": True}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req) as resp:
            self.assertEqual(resp.status, 200)
            data = json.loads(resp.read().decode("utf-8"))
            self.assertIn("message", data)
            self.assertIn("impacts", data)
            self.assertIn("energy_wh", data["impacts"])


    def test_efficiency_overview_empty_state(self):
        body = json.loads(self._get("/api/efficiency/overview"))
        self.assertIn("avg_eta_adjusted", body)
        self.assertIn("avg_eta_unadjusted", body)
        self.assertNotIn("avg_eta", body)
        self.assertIn("sum_e_in_j", body)
        self.assertIn("sum_w_useful_j", body)
        self.assertIn("sum_waste_j", body)

    def test_efficiency_by_model_empty_state(self):
        body = json.loads(self._get("/api/efficiency/by_model"))
        self.assertIsInstance(body, list)

    def test_efficiency_sessions_empty_state(self):
        body = json.loads(self._get("/api/efficiency/sessions"))
        self.assertIsInstance(body, list)

    def test_efficiency_by_day_empty_state(self):
        body = json.loads(self._get("/api/efficiency/by_day"))
        self.assertIsInstance(body, list)

    def test_quality_scores_post_roundtrip(self):
        payload = {
            "message_id": "m_test_123",
            "session_id": "s_test_456",
            "alpha": 0.9,
            "rho": 0.7,
            "method": "human",
        }
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/api/quality_scores",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req) as resp:
            self.assertEqual(resp.status, 201)
            data = json.loads(resp.read().decode("utf-8"))
            self.assertTrue(data.get("ok"))
            self.assertAlmostEqual(data.get("q"), 0.8, places=4)


if __name__ == "__main__":
    unittest.main()
