#!/usr/bin/env python3
"""Standalone Perplexity API Test Script with EcoLogits Environmental Impact Estimation.

Uses Python standard library only (no pip dependencies required).
Optionally verifies native EcoLogits tracing if ecologits + openai are installed.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

if sys.platform == "win32" and hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# ---------------------------------------------------------------------------
# EcoLogits Fallback Constants & Rates (matches token_dashboard/ecologits_bridge.py)
# ---------------------------------------------------------------------------
ENERGY_RATES = {
    "sonar-small":          (0.0003, 0.0010),   # ~8B params
    "sonar-large":          (0.0008, 0.0030),   # ~70B params
    "sonar-huge":           (0.0015, 0.0060),   # ~405B params
    "sonar-pro":            (0.0008, 0.0030),
    "sonar-reasoning-pro":  (0.0015, 0.0060),
    "sonar-reasoning":      (0.0008, 0.0030),
    "sonar":                (0.0003, 0.0010),
    "claude-opus":          (0.0015, 0.0050),
    "claude-sonnet":        (0.0010, 0.0030),
    "claude-haiku":         (0.0005, 0.0015),
    "default":              (0.0008, 0.0030),
}

GRID_GWP = {
    "USA": 380.0,
    "CAN": 120.0,
    "QC":  30.0,
    "WOR": 475.0,
    "FRA": 56.0,
}

WATER_PER_KWH = 0.015       # litres per kWh
EMBODIED_FRACTION = 0.20    # 20% embodied, 80% operational
PE_PER_KWH = 9.0            # MJ per kWh
ADPE_PER_KWH = 1.5e-7       # kgSbeq per kWh
CACHE_READ_FACTOR = 0.10    # 10% energy for cache reads
CACHE_CREATE_FACTOR = 1.20  # 120% energy for cache writes

# ---------------------------------------------------------------------------
# Test Prompts
# ---------------------------------------------------------------------------
TEST_PROMPTS = [
    {
        "name": "Small",
        "model": "llama-3.1-sonar-small-128k-online",
        "prompt": "What is 2+2? Answer in one sentence.",
        "max_tokens": 100,
    },
    {
        "name": "Medium",
        "model": "llama-3.1-sonar-large-128k-online",
        "prompt": (
            "Explain how a shell-and-tube heat exchanger works, including "
            "key thermal efficiency factors. Write 3 paragraphs."
        ),
        "max_tokens": 500,
    },
    {
        "name": "Large",
        "model": "llama-3.1-sonar-huge-128k-online",
        "prompt": (
            "Write a detailed technical analysis of AI inference energy consumption. "
            "Cover GPU power draw, PUE, datacenter cooling, and carbon intensity of "
            "electricity grids. Include quantitative estimates. Write at least 400 words."
        ),
        "max_tokens": 1000,
    },
]


def resolve_rates(model_name: str):
    m = (model_name or "").lower()
    for key in (
        "sonar-reasoning-pro",
        "sonar-reasoning",
        "sonar-small",
        "sonar-large",
        "sonar-huge",
        "sonar-pro",
        "sonar",
    ):
        if key in m:
            return ENERGY_RATES[key]
    return ENERGY_RATES["default"]


def calculate_impacts(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cached_tokens: int = 0,
    electricity_zone: str = "USA",
):
    grid_gwp = GRID_GWP.get((electricity_zone or "USA").upper(), GRID_GWP["USA"])
    input_rate, output_rate = resolve_rates(model)

    uncached_input = max(0, input_tokens - cached_tokens)
    input_energy = (
        (uncached_input / 1000.0) * input_rate
        + (cached_tokens / 1000.0) * input_rate * CACHE_READ_FACTOR
    )
    output_energy = (output_tokens / 1000.0) * output_rate
    energy_kwh = input_energy + output_energy

    # GWP (kgCO₂eq)
    gwp_usage = energy_kwh * grid_gwp / 1000.0
    gwp_embodied = gwp_usage * (EMBODIED_FRACTION / (1.0 - EMBODIED_FRACTION))  # 0.20 / 0.80
    gwp_total = gwp_usage + gwp_embodied

    wcf = energy_kwh * WATER_PER_KWH
    pe = energy_kwh * PE_PER_KWH
    adpe = energy_kwh * ADPE_PER_KWH

    energy_wh = energy_kwh * 1000.0
    gwp_g = gwp_total * 1000.0
    wcf_ml = wcf * 1000.0
    total_tokens = input_tokens + output_tokens

    wh_per_token = (energy_wh / total_tokens) if total_tokens > 0 else 0.0
    gco2_per_token = (gwp_g / total_tokens) if total_tokens > 0 else 0.0
    tokens_per_wh = (output_tokens / energy_wh) if energy_wh > 0 else 0.0

    return {
        "energy_kwh": round(energy_kwh, 9),
        "energy_wh": round(energy_wh, 4),
        "gwp_kgco2eq": round(gwp_total, 9),
        "gwp_gco2eq": round(gwp_g, 4),
        "wcf_l": round(wcf, 9),
        "wcf_ml": round(wcf_ml, 4),
        "pe_mj": round(pe, 6),
        "adpe_kgsbeq": round(adpe, 12),
        "wh_per_token": round(wh_per_token, 6),
        "gco2_per_token": round(gco2_per_token, 6),
        "tokens_per_wh": round(tokens_per_wh, 2),
    }


def call_perplexity_api(
    api_key: str,
    model: str,
    prompt: str,
    max_tokens: int,
    dry_run: bool = False,
):
    """Call the Perplexity chat completions API or return dry-run simulation data."""
    if dry_run or not api_key:
        t0 = time.perf_counter()
        time.sleep(0.05)  # brief simulation delay
        latency = round(time.perf_counter() - t0, 3)
        input_tokens = len(prompt.split()) * 2
        output_tokens = min(max_tokens, 60)
        return {
            "content": f"[Simulated Response for {model}]",
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cached_tokens": 0,
            "latency_s": latency,
            "simulated": True,
        }

    url = "https://api.perplexity.ai/chat/completions"
    payload = {
        "model": model,
        "messages": [
            {"role": "user", "content": prompt}
        ],
        "max_tokens": max_tokens,
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": "TokenDashboard-EcoLogits/1.0",
        },
    )

    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=45) as resp:
            raw = resp.read().decode("utf-8")
            latency = round(time.perf_counter() - t0, 3)
            res = json.loads(raw)
            choice = (res.get("choices") or [{}])[0]
            msg = choice.get("message") or {}
            usage = res.get("usage") or {}
            return {
                "content": msg.get("content", ""),
                "input_tokens": int(usage.get("prompt_tokens") or 0),
                "output_tokens": int(usage.get("completion_tokens") or 0),
                "cached_tokens": int(usage.get("prompt_tokens_details", {}).get("cached_tokens", 0) if isinstance(usage.get("prompt_tokens_details"), dict) else 0),
                "latency_s": latency,
                "simulated": False,
            }
    except urllib.error.HTTPError as e:
        err_msg = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Perplexity API Error (HTTP {e.code}): {err_msg}")
    except Exception as e:
        raise RuntimeError(f"Network error calling Perplexity API: {e}")


def test_optional_ecologits(api_key: str):
    """Test native ecologits library tracer if available."""
    try:
        from ecologits import EcoLogits
        import openai

        print("\n" + "=" * 70)
        print("  OPTIONAL ECOLOGITS NATIVE TRACER TEST")
        print("=" * 70)

        EcoLogits.init(providers=["openai"])
        client = openai.OpenAI(
            api_key=api_key or "dry-run-key",
            base_url="https://api.perplexity.ai",
        )
        print("[EcoLogits] Native EcoLogits wrapper initialized successfully.")
        return True
    except ImportError:
        return False
    except Exception as e:
        print(f"[EcoLogits] Tracer init note: {e}")
        return False


def get_api_key(args_key: str | None) -> str:
    if args_key:
        return args_key
    for env_var in ("PPLX_API_KEY", "PERPLEXITY_API_KEY"):
        val = os.environ.get(env_var)
        if val:
            return val
    return ""


def main():
    parser = argparse.ArgumentParser(
        description="Perplexity API Environmental Impact Estimation (EcoLogits methodology)"
    )
    parser.add_argument("--api-key", help="Perplexity API Key")
    parser.add_argument(
        "--zone",
        choices=["USA", "CAN", "QC", "WOR", "FRA"],
        default="USA",
        help="Electricity grid zone (default: USA)",
    )
    parser.add_argument("--quick", action="store_true", help="Run only the Small prompt test")
    parser.add_argument("--dry-run", action="store_true", help="Simulate API responses without network calls")
    parser.add_argument("--skip-ecologits", action="store_true", help="Skip checking for native ecologits package")
    parser.add_argument("--output", default="perplexity_impact_results.json", help="Path to output JSON results")
    args = parser.parse_args()

    api_key = get_api_key(args.api_key)
    is_dry_run = args.dry_run or not bool(api_key)

    print("=" * 70)
    print("  PERPLEXITY API × ECOLOGITS ENVIRONMENTAL IMPACT AUDIT")
    print("=" * 70)
    print(f"  Grid Zone:           {args.zone} ({GRID_GWP.get(args.zone, 380.0)} gCO₂eq/kWh)")
    print(f"  Execution Mode:      {'Dry-Run Simulation' if is_dry_run else 'Live API Calls'}")
    if is_dry_run and not args.dry_run:
        print("  Notice:              No PPLX_API_KEY found. Running in dry-run mode.")
    print("=" * 70 + "\n")

    prompts_to_run = [TEST_PROMPTS[0]] if args.quick else TEST_PROMPTS
    results = []

    total_tokens_in = 0
    total_tokens_out = 0
    total_energy_wh = 0.0
    total_gwp_g = 0.0
    total_wcf_ml = 0.0
    total_pe_mj = 0.0
    total_adpe_kg = 0.0

    for idx, test in enumerate(prompts_to_run, 1):
        print(f"[{idx}/{len(prompts_to_run)}] Test: {test['name']} ({test['model']})")
        print(f"  Prompt: \"{test['prompt'][:60]}...\" (max_tokens={test['max_tokens']})")

        try:
            api_resp = call_perplexity_api(
                api_key=api_key,
                model=test["model"],
                prompt=test["prompt"],
                max_tokens=test["max_tokens"],
                dry_run=is_dry_run,
            )
        except Exception as err:
            print(f"  Error: {err}\n")
            continue

        impacts = calculate_impacts(
            model=test["model"],
            input_tokens=api_resp["input_tokens"],
            output_tokens=api_resp["output_tokens"],
            cached_tokens=api_resp["cached_tokens"],
            electricity_zone=args.zone,
        )

        total_tokens_in += api_resp["input_tokens"]
        total_tokens_out += api_resp["output_tokens"]
        total_energy_wh += impacts["energy_wh"]
        total_gwp_g += impacts["gwp_gco2eq"]
        total_wcf_ml += impacts["wcf_ml"]
        total_pe_mj += impacts["pe_mj"]
        total_adpe_kg += impacts["adpe_kgsbeq"]

        print(f"  Tokens:   {api_resp['input_tokens']} in / {api_resp['output_tokens']} out (cached: {api_resp['cached_tokens']}) | Latency: {api_resp['latency_s']}s")
        print(f"  Energy:   {impacts['energy_wh']:.4f} Wh ({impacts['energy_kwh']:.7f} kWh)")
        print(f"  Carbon:   {impacts['gwp_gco2eq']:.4f} gCO₂eq (GWP total)")
        print(f"  Water:    {impacts['wcf_ml']:.2f} mL ({impacts['wcf_l']:.6f} L)")
        print(f"  PE / ADPe:{impacts['pe_mj']:.4f} MJ | {impacts['adpe_kgsbeq']:.2e} kgSbeq")
        print(f"  Yield:    {impacts['tokens_per_wh']:.1f} tokens/Wh ({impacts['wh_per_token']:.5f} Wh/tok)\n")

        results.append({
            "test_name": test["name"],
            "model": test["model"],
            "prompt": test["prompt"],
            "max_tokens": test["max_tokens"],
            "api_response": {
                "input_tokens": api_resp["input_tokens"],
                "output_tokens": api_resp["output_tokens"],
                "cached_tokens": api_resp["cached_tokens"],
                "latency_s": api_resp["latency_s"],
                "simulated": api_resp.get("simulated", False),
            },
            "impacts": impacts,
        })

    # Summary and Comparisons Table
    print("=" * 70)
    print("  SUMMARY AUDIT TOTALS & PHYSICAL WORLD EQUIVALENCIES")
    print("=" * 70)
    print(f"  Total In/Out Tokens:      {total_tokens_in:,} input / {total_tokens_out:,} output")
    print(f"  Total Electricity:        {total_energy_wh:.4f} Wh ({total_energy_wh/1000.0:.6f} kWh)")
    print(f"  Total Carbon Footprint:   {total_gwp_g:.4f} gCO₂eq")
    print(f"  Total Water Consumption:  {total_wcf_ml:.2f} mL ({total_wcf_ml/1000.0:.5f} L)")
    print(f"  Primary Energy:           {total_pe_mj:.4f} MJ")
    print("-" * 70)
    print("  Context Comparisons:")
    google_eq = (total_energy_wh / 0.3)
    coffee_eq = (total_energy_wh / 75.0)
    ev_m_eq = (total_energy_wh / 175.0 * 1000.0)
    qc_gwp_g = (total_energy_wh / 1000.0 * 30.0 / 0.80)
    qc_savings = max(0.0, (total_gwp_g - qc_gwp_g) / total_gwp_g * 100.0) if total_gwp_g > 0 else 0.0

    print(f"    • 1 Google search (0.3 Wh)  ≈ {google_eq:.2f} searches")
    print(f"    • 1 cup of coffee (75 Wh)   ≈ {coffee_eq:.4f} cups")
    print(f"    • 1 km EV driving (175 Wh)  ≈ {ev_m_eq:.1f} meters")
    print(f"    • Quebec Hydro-Québec grid  ≈ {qc_savings:.1f}% carbon reduction ({qc_gwp_g:.4f} gCO₂eq vs {total_gwp_g:.4f} g)")
    print("=" * 70)

    if not args.skip_ecologits:
        test_optional_ecologits(api_key)

    output_payload = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "grid_zone": args.zone,
        "grid_intensity_gco2_kwh": GRID_GWP.get(args.zone, 380.0),
        "execution_mode": "dry_run" if is_dry_run else "live",
        "totals": {
            "input_tokens": total_tokens_in,
            "output_tokens": total_tokens_out,
            "energy_wh": round(total_energy_wh, 4),
            "energy_kwh": round(total_energy_wh / 1000.0, 7),
            "gwp_gco2eq": round(total_gwp_g, 4),
            "gwp_kgco2eq": round(total_gwp_g / 1000.0, 7),
            "wcf_ml": round(total_wcf_ml, 4),
            "wcf_l": round(total_wcf_ml / 1000.0, 7),
            "pe_mj": round(total_pe_mj, 6),
            "adpe_kgsbeq": round(total_adpe_kg, 12),
        },
        "results": results,
    }

    try:
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(output_payload, f, indent=2)
        print(f"\n[Saved] Results exported to {args.output}")
    except Exception as e:
        print(f"\n[Warning] Could not save results to {args.output}: {e}")


if __name__ == "__main__":
    main()
