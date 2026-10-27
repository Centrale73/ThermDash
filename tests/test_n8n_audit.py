"""Offline tests for token_dashboard.n8n_audit (stdlib unittest; pytest-compatible)."""
import json
import subprocess
import sys
import unittest
from pathlib import Path

from token_dashboard.n8n_audit import cron_runs_per_year, energy_split, run, scan, temporal_coverage
from token_dashboard.n8n_audit.engine import load_scenarios, quality
from token_dashboard.n8n_audit.graph import model_id, split_template
from token_dashboard.n8n_audit.schedule import cron_run_times, lookback_minutes
from token_dashboard.thermodynamics import EnergyConstants, energy_input

FIX = Path(__file__).parent / "fixtures" / "n8n"
ROOT = Path(__file__).resolve().parent.parent


def _wf():
    return json.loads((FIX / "digest_min.json").read_text(encoding="utf-8"))


class TestSchedule(unittest.TestCase):
    def test_weekday_cron_2026(self):
        self.assertEqual(cron_runs_per_year("0 8 * * 1-5", 2026), 261)

    def test_six_field_cron(self):
        self.assertEqual(cron_runs_per_year("0 0 8 * * 1-5", 2026), 261)

    def test_lookback_parse(self):
        self.assertEqual(lookback_minutes("is:unread newer_than:1d -x"), 1440)
        self.assertEqual(lookback_minutes("newer_than:6h"), 360)
        self.assertIsNone(lookback_minutes("is:unread"))

    def test_weekend_gap_is_about_120_of_168(self):
        cov = temporal_coverage(cron_run_times("0 8 * * 1-5", 2026), 1440, 2026)
        self.assertAlmostEqual(cov["uniform"], 120 / 168, delta=0.005)
        self.assertAlmostEqual(cov["redundancy"], 1.0, places=6)

    def test_wider_window_closes_gap_but_double_counts(self):
        cov = temporal_coverage(cron_run_times("0 8 * * 1-5", 2026), 3 * 1440, 2026)
        self.assertGreater(cov["uniform"], 0.99)
        self.assertGreater(cov["redundancy"], 2.0)


class TestScan(unittest.TestCase):
    def test_modelid_resource_locator_is_pinned(self):
        # Regression: AutoAudit read parameters.model and flagged this node as unpinned.
        node = next(n for n in _wf()["nodes"] if n["name"] == "Summarise")
        self.assertEqual(model_id(node), "gpt-4o-mini")
        flags = scan(_wf(), "gdpr", 2026)["flags"]
        self.assertFalse(any("unpinned" in f for f in flags["yellow"]))

    def test_delivery_and_governance_flags(self):
        a = scan(_wf(), "gdpr", 2026)
        red = " ".join(a["flags"]["red"])
        self.assertIn("placeholder", red)
        self.assertIn("GDPR Chapter V", red)
        self.assertTrue(any("legacy Telegram Markdown" in f for f in a["flags"]["yellow"]))
        self.assertTrue(a["inference_sites"][0]["guarded_by_if"])
        self.assertEqual(a["input_shaping"][0]["item_cap"], 25)

    def test_jurisdiction_switch(self):
        red = " ".join(scan(_wf(), "law25", 2026)["flags"]["red"])
        self.assertIn("Law 25", red)
        self.assertNotIn("GDPR", red)

    def test_split_template(self):
        static, ex = split_template("=Here are {{ $json.n }} mails:\n{{ $json.t }}")
        self.assertEqual(static, "Here are  mails:\n")
        self.assertEqual(ex, ["$json.n", "$json.t"])


class TestEnergy(unittest.TestCase):
    def setUp(self):
        self.sc = load_scenarios()

    def test_linear_matches_thermdash_energy_input(self):
        sc = dict(self.sc["thermdash_benchmarks_json"])
        pre, out, fix = energy_split(800, 150, sc)
        c = EnergyConstants(model="m", gpu="g", e_prefill=sc["e_prefill"], e_decode=sc["e_decode"])
        self.assertAlmostEqual(pre + out + fix, energy_input(800, 150, c), places=12)

    def test_jegham_fit_reproduces_published_points(self):
        sc = self.sc["jegham2025_topdown"]
        for (n_in, n_out), wh in {(100, 300): 0.577, (1000, 1000): 1.897, (10000, 1500): 3.098}.items():
            e = sum(energy_split(n_in, n_out, sc)) * sc["pue_included"]
            self.assertAlmostEqual(e / 3600, wh, delta=0.002)

    def test_theta_form(self):
        sc = self.sc["cavagna2026_llama31_8b_h100"]
        t = sc["theta"]
        n_in, n_out = 512, 128
        want = t[0]*n_out + t[1]*n_in**2 + t[2]*n_in*n_out + t[3]*n_in + t[4]*n_out**2 + t[5]
        self.assertAlmostEqual(sum(energy_split(n_in, n_out, sc)), want, places=9)

    def test_eta_max_is_scale_free_for_linear_c0(self):
        a = {"form": "linear", "e_prefill": 1e-3, "e_decode": 4e-3, "c": 0.0}
        b = {"form": "linear", "e_prefill": 1.0, "e_decode": 4.0, "c": 0.0}
        ea, eb = energy_split(900, 140, a), energy_split(900, 140, b)
        self.assertAlmostEqual(ea[1] / sum(ea), eb[1] / sum(eb), places=12)

    def test_unscored_quality_defaults(self):
        self.assertEqual(quality(None), (1.0, False))
        q, adj = quality({"alpha": 0.8, "rho": 0.9})
        self.assertAlmostEqual(q, 0.85)
        self.assertTrue(adj)


class TestRun(unittest.TestCase):
    def test_end_to_end_invariants(self):
        r = run(FIX / "config.json")
        self.assertEqual(r["runs_per_year"], 261)
        for s in r["scenarios"].values():
            self.assertLessEqual(s["eta_system_delivered"], s["eta_model"] + 1e-12)
            self.assertLessEqual(s["eta_model"], s["eta_max_Q1"] + 1e-12)
            self.assertAlmostEqual(s["annual"]["facility_kWh"] / s["annual"]["server_kWh"], 1.25, places=9)
            self.assertLess(s["boundary"]["llm_share_of_boundary"], 1.0)
        self.assertAlmostEqual(r["coverage"]["temporal"]["uniform"], 120 / 168, delta=0.005)

    def test_cli_smoke(self):
        out = subprocess.run([sys.executable, str(ROOT / "cli.py"), "n8n-audit", "inspect",
                              str(FIX / "digest_min.json")], capture_output=True, text=True,
                             encoding="utf-8", cwd=ROOT)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(json.loads(out.stdout)["schedule"]["runs_per_year"], 261)


if __name__ == "__main__":
    unittest.main()
