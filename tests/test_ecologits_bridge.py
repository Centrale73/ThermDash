import http.server
import json
import os
import socket
import sqlite3
import tempfile
import threading
import unittest
import urllib.request

from token_dashboard.db import (
    init_db, connect,
    impact_totals, daily_impacts, model_impact_breakdown, overview_totals,
)
from token_dashboard.ecologits_bridge import (
    ImpactEstimate,
    estimate_impacts,
    estimate_for_usage,
    aggregate_impacts,
    calculate_efficiency,
    is_ecologits_available,
    get_grid_intensity,
    migrate_db,
    backfill_impacts,
    ENERGY_RATES,
    GRID_GWP,
)
from token_dashboard.scanner import scan_file
from token_dashboard.server import build_handler


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class TestEcologitsBridge(unittest.TestCase):
    def test_grid_intensity(self):
        self.assertEqual(get_grid_intensity("USA"), 380.0)
        self.assertEqual(get_grid_intensity("CAN"), 120.0)
        self.assertEqual(get_grid_intensity("QC"), 30.0)
        self.assertEqual(get_grid_intensity("WOR"), 475.0)
        self.assertEqual(get_grid_intensity("FRA"), 56.0)
        self.assertEqual(get_grid_intensity("UNKNOWN"), 380.0)

    def test_fallback_estimation_models(self):
        # 1000 input, 1000 output tokens
        sonnet = estimate_impacts("claude-3-5-sonnet-20241022", 1000, 1000, electricity_zone="USA")
        opus = estimate_impacts("claude-3-opus-20240229", 1000, 1000, electricity_zone="USA")
        haiku = estimate_impacts("claude-3-haiku-20240307", 1000, 1000, electricity_zone="USA")
        sonar_small = estimate_impacts("sonar-small", 1000, 1000, electricity_zone="USA")

        # Opus > Sonnet > Haiku > Sonar-Small in energy draw
        self.assertGreater(opus.energy_kwh, sonnet.energy_kwh)
        self.assertGreater(sonnet.energy_kwh, haiku.energy_kwh)
        self.assertGreater(haiku.energy_kwh, sonar_small.energy_kwh)

        # Sonnet exact math check:
        # input: 1k * 0.0010 = 0.0010 kWh
        # output: 1k * 0.0030 = 0.0030 kWh
        # total: 0.0040 kWh
        self.assertAlmostEqual(sonnet.energy_kwh, 0.0040, places=5)
        # gwp_usage: 0.0040 * 380 / 1000 = 0.00152 kg
        # gwp_embodied: 0.00152 * (0.20 / 0.80) = 0.00038 kg
        # gwp_total: 0.00190 kgCO2eq
        self.assertAlmostEqual(sonnet.gwp_kgco2eq, 0.00190, places=5)
        self.assertAlmostEqual(sonnet.wcf_l, 0.0040 * 0.015, places=6)
        self.assertAlmostEqual(sonnet.pe_mj, 0.0040 * 9.0, places=5)
        self.assertAlmostEqual(sonnet.adpe_kgsbeq, 0.0040 * 1.5e-7, places=10)
        self.assertIn(sonnet.source, ("ecologits", "fallback"))

    def test_cache_token_energy_adjustments(self):
        # 1000 fresh input tokens
        fresh = estimate_impacts("claude-sonnet-4-7", input_tokens=1000, output_tokens=0)
        # 1000 cache read tokens
        cache_read = estimate_impacts("claude-sonnet-4-7", input_tokens=0, output_tokens=0, cache_read_tokens=1000)
        # 1000 cache create tokens
        cache_create = estimate_impacts("claude-sonnet-4-7", input_tokens=0, output_tokens=0, cache_create_tokens=1000)

        # Cache reads should consume exactly 10% of fresh input prefill energy
        self.assertAlmostEqual(cache_read.energy_kwh, fresh.energy_kwh * 0.10, places=7)
        # Cache writes should consume exactly 120% of fresh input prefill energy
        self.assertAlmostEqual(cache_create.energy_kwh, fresh.energy_kwh * 1.20, places=7)

    def test_grid_carbon_difference_qc_vs_usa(self):
        usa = estimate_impacts("claude-sonnet", 1000, 1000, electricity_zone="USA")
        qc = estimate_impacts("claude-sonnet", 1000, 1000, electricity_zone="QC")

        # Energy is identical
        self.assertAlmostEqual(usa.energy_kwh, qc.energy_kwh, places=6)
        # Carbon in QC (30 g/kWh) should be ~92.1% lower than USA (380 g/kWh)
        savings = (usa.gwp_kgco2eq - qc.gwp_kgco2eq) / usa.gwp_kgco2eq
        self.assertAlmostEqual(savings, (380.0 - 30.0) / 380.0, places=4)

    def test_estimate_for_usage(self):
        usage = {
            "input_tokens": 500,
            "output_tokens": 200,
            "cache_read_tokens": 10000,
            "cache_create_5m_tokens": 1000,
            "cache_create_1h_tokens": 500,
        }
        res = estimate_for_usage(usage, "claude-sonnet-4-6", electricity_zone="USA")
        self.assertIsInstance(res, ImpactEstimate)
        self.assertGreater(res.energy_kwh, 0)
        self.assertGreater(res.gwp_kgco2eq, 0)
        self.assertGreater(res.wcf_l, 0)

    def test_aggregate_impacts(self):
        e1 = estimate_impacts("claude-opus", 1000, 500)
        e2 = estimate_impacts("claude-haiku", 2000, 1000)
        agg = aggregate_impacts([e1, e2])

        self.assertAlmostEqual(agg["energy_kwh"], e1.energy_kwh + e2.energy_kwh, places=7)
        self.assertAlmostEqual(agg["gwp_kgco2eq"], e1.gwp_kgco2eq + e2.gwp_kgco2eq, places=7)
        self.assertEqual(agg["total_count"], 2)

    def test_calculate_efficiency(self):
        eff = calculate_efficiency(useful_tokens=5000, energy_kwh=0.002, grid_gwp=380.0)
        self.assertEqual(eff["useful_tokens"], 5000)
        self.assertEqual(eff["energy_wh"], 2.0)
        self.assertEqual(eff["tokens_per_wh"], 2500.0)
        self.assertAlmostEqual(eff["joules_per_token"], (2.0 * 3600.0) / 5000.0, places=2)

    def test_db_migration_idempotent(self):
        tmp = tempfile.mkdtemp()
        db_path = os.path.join(tmp, "mig.db")
        # Create an old schema db without impact columns
        with sqlite3.connect(db_path) as c:
            c.execute("""
            CREATE TABLE messages (
              uuid TEXT PRIMARY KEY,
              session_id TEXT,
              project_slug TEXT,
              type TEXT,
              timestamp TEXT,
              model TEXT,
              input_tokens INTEGER DEFAULT 0,
              output_tokens INTEGER DEFAULT 0,
              cache_read_tokens INTEGER DEFAULT 0,
              cache_create_5m_tokens INTEGER DEFAULT 0,
              cache_create_1h_tokens INTEGER DEFAULT 0
            )
            """)
            c.execute("INSERT INTO messages (uuid, session_id, project_slug, type, timestamp, model, input_tokens, output_tokens) VALUES ('m1', 's1', 'p1', 'assistant', '2026-04-01T00:00:00Z', 'claude-sonnet-4-6', 100, 200)")
            c.commit()

        # Migrate DB
        migrate_db(db_path)
        with sqlite3.connect(db_path) as c:
            cols = {r[1] for r in c.execute("PRAGMA table_info(messages)")}
            self.assertIn("energy_kwh", cols)
            self.assertIn("gwp_kgco2eq", cols)
            self.assertIn("wcf_l", cols)
            self.assertIn("adpe_kgsbeq", cols)
            self.assertIn("pe_mj", cols)
            self.assertIn("impact_source", cols)

        # Run again to ensure idempotency
        migrate_db(db_path)

    def test_backfill_impacts(self):
        tmp = tempfile.mkdtemp()
        db_path = os.path.join(tmp, "backfill.db")
        init_db(db_path)

        with sqlite3.connect(db_path) as c:
            c.execute("""
            INSERT INTO messages (uuid, session_id, project_slug, type, timestamp, model, input_tokens, output_tokens, cache_read_tokens, cache_create_5m_tokens, cache_create_1h_tokens, impact_source)
            VALUES ('m1', 's1', 'p1', 'assistant', '2026-04-01T00:00:00Z', 'claude-opus-4-7', 500, 1000, 0, 0, 0, '')
            """)
            c.commit()

        updated = backfill_impacts(db_path, electricity_zone="USA")
        self.assertEqual(updated, 1)

        with sqlite3.connect(db_path) as c:
            c.row_factory = sqlite3.Row
            row = c.execute("SELECT * FROM messages WHERE uuid='m1'").fetchone()
            self.assertGreater(row["energy_kwh"], 0)
            self.assertGreater(row["gwp_kgco2eq"], 0)
            self.assertIn(row["impact_source"], ("fallback", "ecologits"))


class TestImpactQueriesAndServer(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "server_test.db")
        init_db(self.db)

        # Ingest sample data with impact scores
        imp1 = estimate_impacts("claude-opus-4-7", 1000, 2000, electricity_zone="USA")
        imp2 = estimate_impacts("claude-haiku-4-5", 500, 500, electricity_zone="USA")

        with connect(self.db) as c:
            c.execute("""
            INSERT INTO messages (
              uuid, parent_uuid, session_id, project_slug, type, timestamp, model,
              input_tokens, output_tokens, cache_read_tokens, cache_create_5m_tokens, cache_create_1h_tokens,
              energy_kwh, gwp_kgco2eq, wcf_l, adpe_kgsbeq, pe_mj, impact_source
            ) VALUES (
              'm1', NULL, 's1', 'projA', 'assistant', '2026-04-10T12:00:00Z', 'claude-opus-4-7',
              1000, 2000, 0, 0, 0,
              ?, ?, ?, ?, ?, ?
            )
            """, (imp1.energy_kwh, imp1.gwp_kgco2eq, imp1.wcf_l, imp1.adpe_kgsbeq, imp1.pe_mj, imp1.source))

            c.execute("""
            INSERT INTO messages (
              uuid, parent_uuid, session_id, project_slug, type, timestamp, model,
              input_tokens, output_tokens, cache_read_tokens, cache_create_5m_tokens, cache_create_1h_tokens,
              energy_kwh, gwp_kgco2eq, wcf_l, adpe_kgsbeq, pe_mj, impact_source
            ) VALUES (
              'm2', NULL, 's1', 'projA', 'assistant', '2026-04-11T14:00:00Z', 'claude-haiku-4-5',
              500, 500, 0, 0, 0,
              ?, ?, ?, ?, ?, ?
            )
            """, (imp2.energy_kwh, imp2.gwp_kgco2eq, imp2.wcf_l, imp2.adpe_kgsbeq, imp2.pe_mj, imp2.source))
            c.commit()

        self.port = _free_port()
        H = build_handler(self.db, projects_dir="/nonexistent")
        self.httpd = http.server.HTTPServer(("127.0.0.1", self.port), H)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.httpd.shutdown()

    def _get_json(self, path):
        raw = urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}").read()
        return json.loads(raw)

    def test_query_helpers(self):
        tot = impact_totals(self.db)
        self.assertGreater(tot["energy_kwh"], 0)
        self.assertGreater(tot["gwp_kgco2eq"], 0)
        self.assertEqual(tot["total_messages"], 2)

        daily = daily_impacts(self.db)
        self.assertEqual(len(daily), 2)
        self.assertEqual(daily[0]["day"], "2026-04-10")
        self.assertEqual(daily[1]["day"], "2026-04-11")

        models = model_impact_breakdown(self.db)
        self.assertEqual(len(models), 2)
        self.assertEqual(models[0]["model"], "claude-opus-4-7")

    def test_api_impacts(self):
        data = self._get_json("/api/impacts")
        self.assertIn("energy_kwh", data)
        self.assertIn("gwp_kgco2eq", data)
        self.assertIn("grid_intensity", data)
        self.assertIn("ecologits_available", data)

    def test_api_impacts_daily(self):
        data = self._get_json("/api/impacts/daily")
        self.assertIsInstance(data, list)
        self.assertGreaterEqual(len(data), 2)

    def test_api_impacts_models(self):
        data = self._get_json("/api/impacts/models")
        self.assertIsInstance(data, list)
        self.assertEqual(len(data), 2)

    def test_api_overview_includes_impacts(self):
        data = self._get_json("/api/overview")
        self.assertIn("impacts", data)
        self.assertGreater(data["impacts"]["energy_kwh"], 0)


if __name__ == "__main__":
    unittest.main()
