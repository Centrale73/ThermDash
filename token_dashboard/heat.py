"""Heat-Dollar & Utility Engine: translates physical server energy into facility overhead,

utility liability ($), and regional carbon emissions.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Union

UTILITY_RATES_JSON = Path(__file__).parent / "data" / "utility_rates.json"
_CACHED_RATES = None


def load_utility_rates(path: Optional[Union[str, Path]] = None) -> dict:
    """Load utility profiles and regional parameters."""
    global _CACHED_RATES
    if path is None and _CACHED_RATES is not None:
        return _CACHED_RATES
    p = Path(path) if path else UTILITY_RATES_JSON
    if p.is_file():
        data = json.loads(p.read_text(encoding="utf-8"))
        if path is None:
            _CACHED_RATES = data
        return data
    return {"default_region": "quebec_commercial_hydro", "profiles": {}}


@dataclass
class HeatResult:
    heat_dollars: float
    facility_energy_kwh: float
    co2_emissions_g: float
    region_key: str
    pue_applied: float
    rate_applied: float
    assumptions: dict


def calculate_heat_dollars(
    energy_kwh: float,
    region_key: str = "quebec_commercial_hydro",
    override_pue: Optional[float] = None,
    override_rate: Optional[float] = None,
    rates_path: Optional[Union[str, Path]] = None,
) -> HeatResult:
    """Translate physical server energy usage into local utility liability and facility overhead:

    Facility Energy (kWh) = E_server * PUE
    Heat Dollars ($) = Facility Energy * Rate ($/kWh)
    CO2 Emissions (g) = Facility Energy * g_CO2/kWh
    """
    catalog = load_utility_rates(rates_path)
    profiles = catalog.get("profiles", {})
    default_key = catalog.get("default_region", "quebec_commercial_hydro")

    reg = (region_key or default_key).strip().lower()
    profile = profiles.get(reg)
    if not profile:
        profile = profiles.get(default_key, {
            "name": "Hydro-Québec Commercial",
            "pue": 1.15,
            "rate_usd_per_kwh": 0.055,
            "g_co2_per_kwh": 1.7,
        })
        reg = default_key

    pue = float(override_pue) if override_pue is not None else float(profile.get("pue", 1.15))
    rate = float(override_rate) if override_rate is not None else float(profile.get("rate_usd_per_kwh", 0.055))
    co2_rate = float(profile.get("g_co2_per_kwh", 1.7))

    e_server = max(0.0, float(energy_kwh or 0.0))
    facility_energy = e_server * pue
    heat_dollars = facility_energy * rate
    co2_grams = facility_energy * co2_rate

    assumptions = {
        "region_name": profile.get("name", reg),
        "default_pue": profile.get("pue", 1.15),
        "default_rate_usd_per_kwh": profile.get("rate_usd_per_kwh", 0.055),
        "g_co2_per_kwh": co2_rate,
        "is_pue_overridden": override_pue is not None,
        "is_rate_overridden": override_rate is not None,
    }

    return HeatResult(
        heat_dollars=round(heat_dollars, 6),
        facility_energy_kwh=round(facility_energy, 6),
        co2_emissions_g=round(co2_grams, 4),
        region_key=reg,
        pue_applied=round(pue, 4),
        rate_applied=round(rate, 6),
        assumptions=assumptions,
    )
