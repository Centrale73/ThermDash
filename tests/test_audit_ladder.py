"""Comprehensive unit tests for Antigravity Metrics Ladder and Delta-Cost to Thermal Waste engine."""
import json
import sqlite3
import pytest
from pathlib import Path

from token_dashboard.db import (
    init_db, connect, insert_audit_run, insert_audit_event,
    get_audit_summary, get_audit_waste_events, get_audit_runs
)
from token_dashboard.ecologits_bridge import calculate_energy, load_energy_benchmarks
from token_dashboard.heat import calculate_heat_dollars, load_utility_rates
from token_dashboard.delta_heat import compute_cost_and_thermal_delta
from token_dashboard.scanner import (
    cosine_similarity,
    eval_w001_retry_chain,
    eval_w002_redundant_tool_call,
    eval_w003_cache_miss_leak,
    eval_w004_excessive_output_padding,
    scan_waste_rules,
)
from token_dashboard.server import build_handler


# ---------------------------------------------------------------------------
# 1. Database Schema & Migration Tests
# ---------------------------------------------------------------------------

def test_db_audit_schema_and_insert(tmp_path: Path):
    db_file = tmp_path / "test_audit.db"
    init_db(db_file)

    # Insert an audit run
    run_data = {
        "run_id": "run-001",
        "session_id": "sess-abc",
        "client_id": "client-xyz",
        "timestamp": "2026-09-02T12:00:00Z",
        "model": "claude-5-sonnet",
        "input_tokens": 1000,
        "output_tokens": 500,
        "cost_usd": 0.0105,
        "energy_kwh": 0.00000833,
        "energy_joules": 30.0,
        "energy_method": "benchmark_lookup",
        "benchmark_source": "Anthropic Architecture Spec 2026",
        "hardware_assumed": "NVIDIA H100-SXM5-80GB",
        "heat_dollars": 0.00000052,
        "is_waste": 1,
        "waste_cost_usd": 0.0050,
        "waste_energy_kwh": 0.00000416,
        "cost_billed_usd": 0.0105,
        "cost_useful_usd": 0.0055,
        "cost_delta_usd": 0.0050,
        "thermal_waste_joules": 15.0,
        "thermal_waste_kwh": 0.00000416,
        "thermal_waste_heat_usd": 0.00000026,
    }
    run_id = insert_audit_run(db_file, run_data)
    assert run_id == "run-001"

    # Insert a child audit waste event
    event_data = {
        "event_id": "evt-001",
        "run_id": run_id,
        "event_type": "retry_chain",
        "tokens_wasted": 500,
        "cost_wasted_usd": 0.0050,
        "energy_wasted_kwh": 0.00000416,
        "heat_wasted_dollars": 0.00000026,
        "rule_id": "W001_RETRY_CHAIN",
        "details": "Prompt thrashing detected with cosine similarity > 0.92",
    }
    evt_id = insert_audit_event(db_file, event_data)
    assert evt_id == "evt-001"

    # Verify summary
    summary = get_audit_summary(db_file)
    assert summary["total_runs"] == 1
    assert summary["total_input_tokens"] == 1000
    assert summary["total_output_tokens"] == 500
    assert summary["waste_runs_count"] == 1
    assert summary["total_cost_billed_usd"] == 0.0105
    assert summary["total_cost_useful_usd"] == 0.0055
    assert summary["total_cost_delta_usd"] == 0.0050
    assert summary["waste_efficiency_ratio"] == pytest.approx(0.0055 / 0.0105, rel=1e-2)
    assert len(summary["by_energy_method"]) == 1
    assert summary["by_energy_method"][0]["energy_method"] == "benchmark_lookup"
    assert len(summary["by_hardware"]) == 1
    assert summary["by_hardware"][0]["hardware_assumed"] == "NVIDIA H100-SXM5-80GB"

    # Verify waste events query
    events = get_audit_waste_events(db_file, run_id=run_id)
    assert len(events) == 1
    assert events[0]["rule_id"] == "W001_RETRY_CHAIN"
    assert events[0]["event_type"] == "retry_chain"

    # Verify runs query
    runs = get_audit_runs(db_file, session_id="sess-abc")
    assert len(runs) == 1
    assert runs[0]["run_id"] == "run-001"


# ---------------------------------------------------------------------------
# 2. Energy Split & Provenance Engine Tests
# ---------------------------------------------------------------------------

def test_energy_prefill_decode_split():
    # Test claude-5-sonnet: 0.010 J/in, 0.040 J/out on NVIDIA H100
    res = calculate_energy("claude-5-sonnet", input_tokens=1000, output_tokens=500)
    expected_joules = (1000 * 0.010) + (500 * 0.040)  # 10 + 20 = 30 J
    expected_kwh = 30.0 / 3_600_000.0

    assert res.energy_joules == pytest.approx(expected_joules, rel=1e-4)
    assert res.energy_kwh == pytest.approx(expected_kwh, rel=1e-4)
    assert res.hardware_assumed == "NVIDIA H100-SXM5-80GB"
    assert res.energy_method == "benchmark_lookup"
    assert "Anthropic" in res.benchmark_source


def test_energy_new_model_catalog_coverage():
    models_to_test = [
        ("gpt-5.6", "NVIDIA B200 SXM", 0.022, 0.088),
        ("gpt-4o", "NVIDIA H100-SXM5-80GB", 0.015, 0.055),
        ("claude-5-opus", "NVIDIA B200 SXM", 0.030, 0.120),
        ("claude-5-haiku", "NVIDIA H100-SXM5-80GB", 0.0022, 0.0085),
        ("gemini-3.8-flash", "Google TPU v6e", 0.0018, 0.0068),
        ("gemini-3.7-flash", "Google TPU v5e", 0.0020, 0.0075),
        ("gemini-3.6-pro", "Google TPU v5p", 0.0080, 0.0320),
        ("sonar-pro", "NVIDIA H100-SXM5-80GB", 0.012, 0.045),
        ("sonar-reasoning", "NVIDIA H100-SXM5-80GB", 0.014, 0.050),
        ("sonar", "NVIDIA A100-SXM4-80GB", 0.0025, 0.010),
        ("llama-3.1-70b", "NVIDIA A100-SXM4-80GB", 0.018, 0.072),
    ]
    for m, expected_hw, expected_prefill, expected_decode in models_to_test:
        res = calculate_energy(m, input_tokens=100, output_tokens=100)
        assert res.hardware_assumed == expected_hw, f"Hardware mismatch for {m}"
        assert res.e_prefill_j == expected_prefill, f"Prefill J mismatch for {m}"
        assert res.e_decode_j == expected_decode, f"Decode J mismatch for {m}"


def test_energy_conservative_fallback_and_custom_hardware():
    res_default = calculate_energy("completely-unknown-custom-llm", input_tokens=1000, output_tokens=1000)
    assert res_default.energy_method == "conservative_fallback"
    assert "arXiv:2511.05597" in res_default.benchmark_source

    res_custom = calculate_energy("gpt-4o", input_tokens=500, output_tokens=500, custom_hardware="Custom-Cluster-v1")
    assert res_custom.hardware_assumed == "Custom-Cluster-v1"


# ---------------------------------------------------------------------------
# 3. Heat-Dollar & Utility Engine Tests
# ---------------------------------------------------------------------------

def test_heat_dollars_profiles():
    # Quebec Commercial Hydro: PUE 1.15, Rate 0.055 $/kWh, CO2 1.7 g/kWh
    energy_kwh = 10.0  # 10 kWh server energy
    res_qc = calculate_heat_dollars(energy_kwh, region_key="quebec_commercial_hydro")

    expected_facility_kwh = 10.0 * 1.15  # 11.5 kWh
    expected_heat_dollars = 11.5 * 0.055  # $0.6325
    expected_co2 = 11.5 * 1.7            # 19.55 g

    assert res_qc.facility_energy_kwh == pytest.approx(expected_facility_kwh, rel=1e-4)
    assert res_qc.heat_dollars == pytest.approx(expected_heat_dollars, rel=1e-4)
    assert res_qc.co2_emissions_g == pytest.approx(expected_co2, rel=1e-4)
    assert res_qc.region_key == "quebec_commercial_hydro"

    # US East Virginia: PUE 1.25, Rate 0.085 $/kWh, CO2 380 g/kWh
    res_va = calculate_heat_dollars(energy_kwh, region_key="us_east_virginia")
    assert res_va.facility_energy_kwh == pytest.approx(10.0 * 1.25, rel=1e-4)
    assert res_va.heat_dollars == pytest.approx(12.5 * 0.085, rel=1e-4)
    assert res_va.co2_emissions_g == pytest.approx(12.5 * 380.0, rel=1e-4)

    # Overrides
    res_override = calculate_heat_dollars(energy_kwh, override_pue=1.40, override_rate=0.20)
    assert res_override.pue_applied == 1.40
    assert res_override.rate_applied == 0.20
    assert res_override.facility_energy_kwh == pytest.approx(14.0, rel=1e-4)
    assert res_override.heat_dollars == pytest.approx(2.80, rel=1e-4)


# ---------------------------------------------------------------------------
# 4. Delta-Cost to Thermal Waste Tests
# ---------------------------------------------------------------------------

def test_delta_cost_zero_waste():
    # 100% useful work: total == useful
    res = compute_cost_and_thermal_delta(
        model="claude-3-5-sonnet",
        total_in=1000,
        total_out=500,
        useful_in=1000,
        useful_out=500,
        price_per_1k_in=0.003,
        price_per_1k_out=0.015,
    )
    assert res.cost_wasted_usd == 0.0
    assert res.tokens_wasted_in == 0
    assert res.tokens_wasted_out == 0
    assert res.energy_wasted_joules == 0.0
    assert res.energy_wasted_kwh == 0.0
    assert res.heat_dollars_wasted == 0.0
    assert res.waste_efficiency_ratio == 1.0


def test_delta_cost_with_waste():
    # Billed: 2,000 in, 1,000 out. Useful: 1,000 in, 500 out.
    # Wasted: 1,000 in, 500 out.
    res = compute_cost_and_thermal_delta(
        model="claude-3-5-sonnet",
        total_in=2000,
        total_out=1000,
        useful_in=1000,
        useful_out=500,
        price_per_1k_in=0.003,
        price_per_1k_out=0.015,
        region_key="quebec_commercial_hydro",
    )
    # Cost billed = (2 * 0.003) + (1 * 0.015) = 0.006 + 0.015 = 0.021
    # Cost useful = (1 * 0.003) + (0.5 * 0.015) = 0.003 + 0.0075 = 0.0105
    # Delta cost = 0.021 - 0.0105 = 0.0105
    assert res.cost_billed_usd == pytest.approx(0.021, rel=1e-4)
    assert res.cost_useful_usd == pytest.approx(0.0105, rel=1e-4)
    assert res.cost_wasted_usd == pytest.approx(0.0105, rel=1e-4)
    assert res.tokens_wasted_in == 1000
    assert res.tokens_wasted_out == 500

    # Claude 3.5 Sonnet: 0.012 J/in, 0.048 J/out
    expected_joules = (1000 * 0.012) + (500 * 0.048)  # 12 + 24 = 36 J
    assert res.energy_wasted_joules == pytest.approx(expected_joules, rel=1e-4)

    expected_kwh = expected_joules / 3_600_000.0
    assert res.energy_wasted_kwh == pytest.approx(expected_kwh, rel=1e-4)
    assert res.heat_dollars_wasted > 0
    assert res.waste_efficiency_ratio == pytest.approx(0.50, rel=1e-2)


# ---------------------------------------------------------------------------
# 5. Waste-Event Attribution Rules (W001 - W004) Tests
# ---------------------------------------------------------------------------

def test_cosine_similarity():
    # Identical
    assert cosine_similarity("Fix the database connection bug", "Fix the database connection bug") == pytest.approx(1.0)
    # Slight variation (thrashing)
    s1 = "Please write a python script to parse the nginx access log file"
    s2 = "Please write a python script to parse that nginx access log file"
    assert cosine_similarity(s1, s2) > 0.92
    # Disjoint
    assert cosine_similarity("python script", "quantum physics gravity") == 0.0


def test_rule_w001_retry_chain():
    turns = [
        {
            "uuid": "u1", "type": "user",
            "prompt_text": "Please write a python script to parse the nginx access log file",
            "input_tokens": 200, "output_tokens": 0
        },
        {
            "parent_uuid": "u1", "type": "assistant",
            "prompt_text": None,
            "input_tokens": 0, "output_tokens": 300,
            "model": "claude-3-5-sonnet"
        },
        {
            "uuid": "u2", "type": "user",
            "prompt_text": "Please write a python script to parse that nginx access log file",
            "input_tokens": 500, "output_tokens": 0
        }
    ]
    events = eval_w001_retry_chain(turns, run_id="run-1", model="claude-3-5-sonnet")
    assert len(events) == 1
    assert events[0].rule_id == "W001_RETRY_CHAIN"
    assert events[0].tokens_wasted == 500  # 200 input + 300 assistant output


def test_rule_w002_redundant_tool_call():
    tool_calls = [
        {"tool_name": "Read", "target": "src/main.py", "result_tokens": 120},
        {"tool_name": "Grep", "target": "TODO", "result_tokens": 50},
        {"tool_name": "Read", "target": "src/main.py", "result_tokens": 120},  # Duplicate
    ]
    events = eval_w002_redundant_tool_call(tool_calls, run_id="run-1")
    assert len(events) == 1
    assert events[0].rule_id == "W002_REDUNDANT_TOOL_CALL"
    assert events[0].tokens_wasted == 120
    assert "src/main.py" in events[0].details


def test_rule_w003_cache_miss_leak():
    turns = [
        {"type": "user", "input_tokens": 2500, "cache_read_tokens": 0, "cache_create_5m_tokens": 0, "cache_create_1h_tokens": 0},
        # Repeated large system prompt on turn 2 without cache headers
        {"type": "user", "input_tokens": 2600, "cache_read_tokens": 0, "cache_create_5m_tokens": 0, "cache_create_1h_tokens": 0},
    ]
    events = eval_w003_cache_miss_leak(turns, run_id="run-1")
    assert len(events) == 1
    assert events[0].rule_id == "W003_CACHE_MISS_LEAK"
    assert events[0].tokens_wasted == 2100  # 2600 - 500 baseline


def test_rule_w004_excessive_output_padding():
    turns = [
        {"type": "user", "prompt_text": "Extract user count in json format only."},
        {
            "type": "assistant",
            # Returned 500 tokens of conversational fluff instead of concise JSON
            "output_tokens": 500,
            "model": "claude-3-5-sonnet"
        }
    ]
    events = eval_w004_excessive_output_padding(turns, run_id="run-1")
    assert len(events) == 1
    assert events[0].rule_id == "W004_EXCESSIVE_OUTPUT_PADDING"
    assert events[0].tokens_wasted == 450  # 500 - 50 expected payload


def test_scan_waste_rules_aggregation():
    turns = [
        {"type": "user", "prompt_text": "Fetch status in json only", "input_tokens": 3000, "cache_read_tokens": 0, "cache_create_5m_tokens": 0, "cache_create_1h_tokens": 0},
        {"type": "assistant", "output_tokens": 400},
        {"type": "user", "prompt_text": "Fetch status in json only", "input_tokens": 3000, "cache_read_tokens": 0, "cache_create_5m_tokens": 0, "cache_create_1h_tokens": 0},
    ]
    tool_calls = [
        {"tool_name": "Glob", "target": "*.py", "result_tokens": 40},
        {"tool_name": "Glob", "target": "*.py", "result_tokens": 40},
    ]
    all_events = scan_waste_rules(turns, tool_calls=tool_calls, run_id="run-agg")
    assert len(all_events) >= 3
    rule_ids = {e.rule_id for e in all_events}
    assert "W001_RETRY_CHAIN" in rule_ids
    assert "W002_REDUNDANT_TOOL_CALL" in rule_ids
    assert "W003_CACHE_MISS_LEAK" in rule_ids


# ---------------------------------------------------------------------------
# 6. REST API Endpoints Integration Tests
# ---------------------------------------------------------------------------

def test_api_audit_endpoints_e2e(tmp_path: Path):
    db_file = tmp_path / "test_api_audit.db"
    init_db(db_file)
    projects_dir = tmp_path / "projects"
    projects_dir.mkdir(parents=True, exist_ok=True)

    handler_cls = build_handler(str(db_file), str(projects_dir))

    class DummyServer:
        pass

    class RequestMock:
        def __init__(self, method: str, path: str, body: dict = None):
            self.method = method
            self.path = path
            self.headers = {}
            if body is not None:
                raw = json.dumps(body).encode("utf-8")
                self.headers["Content-Length"] = str(len(raw))
                self.rfile = io.BytesIO(raw)
            else:
                self.rfile = io.BytesIO(b"")
            self.wfile = io.BytesIO()
            self.status_code = None
            self.response_headers = {}

        def send_response(self, code, message=None):
            self.status_code = code

        def send_header(self, keyword, value):
            self.response_headers[keyword] = value

        def end_headers(self):
            pass

    import io

    # 1. Ingest run via POST /api/audit/ingest
    ingest_payload = {
        "run_id": "run-api-01",
        "session_id": "session-api-01",
        "client_id": "client-api-01",
        "model": "gpt-5.6",
        "input_tokens": 2000,
        "output_tokens": 800,
        "useful_in": 1000,
        "useful_out": 400,
        "waste_events": [
            {
                "event_type": "retry_chain",
                "tokens_wasted": 1000,
                "cost_wasted_usd": 0.015,
                "energy_wasted_kwh": 0.000005,
                "heat_wasted_dollars": 0.0000003,
                "rule_id": "W001_RETRY_CHAIN",
                "details": "Thrashing retry loop"
            }
        ]
    }
    req_post = RequestMock("POST", "/api/audit/ingest", ingest_payload)
    handler_cls.do_POST(req_post)
    assert req_post.status_code == 201

    post_resp = json.loads(req_post.wfile.getvalue().decode("utf-8"))
    assert post_resp["ok"] is True
    assert post_resp["run_id"] == "run-api-01"
    assert post_resp["delta"]["cost_wasted_usd"] > 0
    assert post_resp["energy"]["hardware_assumed"] == "NVIDIA B200 SXM"

    # 2. Query summary via GET /api/audit/summary
    req_summary = RequestMock("GET", "/api/audit/summary")
    handler_cls.do_GET(req_summary)
    assert req_summary.status_code == 200
    summary_resp = json.loads(req_summary.wfile.getvalue().decode("utf-8"))
    assert summary_resp["total_runs"] == 1
    assert summary_resp["total_cost_billed_usd"] > 0
    assert summary_resp["waste_runs_count"] == 1

    # 3. Query waste events via GET /api/audit/waste-events
    req_events = RequestMock("GET", "/api/audit/waste-events?rule_id=W001_RETRY_CHAIN")
    handler_cls.do_GET(req_events)
    assert req_events.status_code == 200
    events_resp = json.loads(req_events.wfile.getvalue().decode("utf-8"))
    assert len(events_resp) == 1
    assert events_resp[0]["rule_id"] == "W001_RETRY_CHAIN"
