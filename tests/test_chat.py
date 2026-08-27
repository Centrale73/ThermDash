import os
import sqlite3
import tempfile
import unittest

from token_dashboard.db import init_db
from token_dashboard.chat import (
    handle_chat_request,
    get_configured_keys,
    resolve_api_key,
    detect_provider,
    _simulate_response,
    log_turn_to_db,
)


class ChatbotTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "chat_test.db")
        init_db(self.db)

    def test_detect_provider(self):
        self.assertEqual(detect_provider("sonar"), "perplexity")
        self.assertEqual(detect_provider("sonar-pro"), "perplexity")
        self.assertEqual(detect_provider("sonar-reasoning"), "perplexity")
        self.assertEqual(detect_provider("llama-3.1-sonar-small-128k-online"), "perplexity")
        self.assertEqual(detect_provider("claude-3-5-sonnet"), "anthropic")
        self.assertEqual(detect_provider("claude-opus"), "anthropic")
        self.assertEqual(detect_provider("gpt-4o"), "openai")
        self.assertEqual(detect_provider("gpt-4o-mini"), "openai")

    def test_resolve_api_key_client_override(self):
        key = resolve_api_key("perplexity", client_key="pplx-custom-12345")
        self.assertEqual(key, "pplx-custom-12345")

    def test_resolve_api_key_from_env(self):
        old = os.environ.get("PPLX_API_KEY")
        try:
            os.environ["PPLX_API_KEY"] = "pplx-env-key-abc"
            self.assertEqual(resolve_api_key("perplexity"), "pplx-env-key-abc")
        finally:
            if old is not None:
                os.environ["PPLX_API_KEY"] = old
            else:
                os.environ.pop("PPLX_API_KEY", None)

    def test_simulate_response(self):
        res = _simulate_response("sonar-pro", [{"role": "user", "content": "What is clean energy?"}])
        self.assertTrue(res["simulated"])
        self.assertIn("Simulated Response", res["content"])
        self.assertGreater(res["input_tokens"], 0)
        self.assertGreater(res["output_tokens"], 0)

    def test_handle_chat_request_dry_run_and_impacts(self):
        body = {
            "model": "sonar",
            "messages": [{"role": "user", "content": "Hello world!"}],
            "dry_run": True,
            "zone": "QC",
            "log_to_db": True,
        }
        res = handle_chat_request(self.db, body)
        self.assertTrue(res["simulated"])
        self.assertEqual(res["model"], "sonar")
        self.assertIn("usage", res)
        self.assertIn("impacts", res)
        self.assertIn("energy_wh", res["impacts"])
        self.assertIn("gwp_gco2eq", res["impacts"])
        self.assertEqual(res["impacts"]["grid_zone"], "QC")
        self.assertGreaterEqual(res["cost_usd"], 0)

        # Check that DB was populated
        with sqlite3.connect(self.db) as c:
            rows = c.execute("SELECT uuid, type, model, prompt_text, energy_kwh FROM messages").fetchall()
            self.assertEqual(len(rows), 2)  # 1 user + 1 assistant
            user_msg = [r for r in rows if r[1] == "user"][0]
            asst_msg = [r for r in rows if r[1] == "assistant"][0]
            self.assertEqual(user_msg[3], "Hello world!")
            self.assertEqual(asst_msg[2], "sonar")
            self.assertGreater(asst_msg[4], 0.0)

    def test_configured_keys_status(self):
        status = get_configured_keys()
        self.assertIn("perplexity", status)
        self.assertIn("anthropic", status)
        self.assertIn("openai", status)


if __name__ == "__main__":
    unittest.main()
