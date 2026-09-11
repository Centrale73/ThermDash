"""Delta-Cost to Thermal Waste engine.

Computes financial overpayment (Delta-Cost) and maps wasted tokens to physical
thermodynamic dissipation (Joules, kWh), facility liability (Heat-Dollars), and carbon.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Union

from .ecologits_bridge import calculate_energy, EnergyResult
from .heat import calculate_heat_dollars, HeatResult
from .pricing import load_pricing

DEFAULT_PRICING_JSON = Path(__file__).resolve().parent.parent / "pricing.json"


@dataclass
class WasteDeltaResult:
    cost_billed_usd: float
    cost_useful_usd: float
    cost_wasted_usd: float  # Delta-Cost
    tokens_wasted_in: int
    tokens_wasted_out: int
    energy_wasted_joules: float
    energy_wasted_kwh: float
    heat_dollars_wasted: float
    co2_wasted_grams: float
    waste_efficiency_ratio: float  # useful / billed
    benchmark_source: str
    hardware_assumed: str


def compute_cost_and_thermal_delta(
    model: str,
    total_in: int,
    total_out: int,
    useful_in: int,
    useful_out: int,
    price_per_1k_in: Optional[float] = None,
    price_per_1k_out: Optional[float] = None,
    region_key: str = "quebec_commercial_hydro",
    custom_hardware: Optional[str] = None,
    override_pue: Optional[float] = None,
    override_rate: Optional[float] = None,
    pricing_path: Optional[Union[str, Path]] = None,
) -> WasteDeltaResult:
    """Calculate financial delta-cost and corresponding thermal/energy waste.

    Core equations:
      Cost_billed = (N_in^total * P_in) + (N_out^total * P_out)
      Cost_useful = (N_in^useful * P_in) + (N_out^useful * P_out)
      Delta_Cost  = Cost_billed - Cost_useful
      N_in^wasted = max(0, N_in^total - N_in^useful)
      N_out^wasted = max(0, N_out^total - N_out^useful)
      E_waste^Joules = (N_in^wasted * e_prefill) + (N_out^wasted * e_decode)
      E_waste^kWh    = E_waste^Joules / 3,600,000
      Heat_$         = E_waste^kWh * PUE * Rate_elec ($/kWh)
    """
    total_in = max(0, int(total_in or 0))
    total_out = max(0, int(total_out or 0))
    useful_in = max(0, min(total_in, int(useful_in or 0)))
    useful_out = max(0, min(total_out, int(useful_out or 0)))

    # Resolve pricing rates (per 1,000 tokens)
    p_in = price_per_1k_in
    p_out = price_per_1k_out
    if p_in is None or p_out is None:
        try:
            pricing = load_pricing(pricing_path or DEFAULT_PRICING_JSON)
            m_key = model or ""
            rates = pricing.get("models", {}).get(m_key)
            if not rates:
                # Check tier fallback
                for tier in ("opus", "sonnet", "haiku", "gpt-5", "gpt-4", "gemini", "sonar", "llama"):
                    if tier in m_key.lower() and tier in pricing.get("tier_fallback", {}):
                        rates = pricing["tier_fallback"][tier]
                        break
            if not rates:
                rates = {"input": 3.0, "output": 15.0}  # default baseline $/1M
            if p_in is None:
                p_in = rates["input"] / 1000.0  # convert $/1M to $/1k
            if p_out is None:
                p_out = rates["output"] / 1000.0
        except Exception:
            if p_in is None:
                p_in = 0.003
            if p_out is None:
                p_out = 0.015

    p_in = float(p_in)
    p_out = float(p_out)

    cost_billed = ((total_in / 1000.0) * p_in) + ((total_out / 1000.0) * p_out)
    cost_useful = ((useful_in / 1000.0) * p_in) + ((useful_out / 1000.0) * p_out)
    cost_wasted = max(0.0, cost_billed - cost_useful)

    wasted_in = max(0, total_in - useful_in)
    wasted_out = max(0, total_out - useful_out)

    energy_waste_res = calculate_energy(
        model=model,
        input_tokens=wasted_in,
        output_tokens=wasted_out,
        custom_hardware=custom_hardware,
    )

    heat_res = calculate_heat_dollars(
        energy_kwh=energy_waste_res.energy_kwh,
        region_key=region_key,
        override_pue=override_pue,
        override_rate=override_rate,
    )

    efficiency_ratio = round(cost_useful / cost_billed, 4) if cost_billed > 0 else 1.0

    return WasteDeltaResult(
        cost_billed_usd=round(cost_billed, 6),
        cost_useful_usd=round(cost_useful, 6),
        cost_wasted_usd=round(cost_wasted, 6),
        tokens_wasted_in=wasted_in,
        tokens_wasted_out=wasted_out,
        energy_wasted_joules=round(energy_waste_res.energy_joules, 4),
        energy_wasted_kwh=round(energy_waste_res.energy_kwh, 9),
        heat_dollars_wasted=round(heat_res.heat_dollars, 6),
        co2_wasted_grams=round(heat_res.co2_emissions_g, 4),
        waste_efficiency_ratio=efficiency_ratio,
        benchmark_source=energy_waste_res.benchmark_source,
        hardware_assumed=energy_waste_res.hardware_assumed,
    )
