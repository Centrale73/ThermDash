"""EcoLogits bridge: estimates energy, carbon, water, and material footprint from LLM usage."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple, Union

# Energy rates in kWh per 1K tokens: (input_rate, output_rate)
ENERGY_RATES = {
    "claude-opus":          (0.0015, 0.0050),   # ~175B params
    "claude-sonnet":        (0.0010, 0.0030),   # ~70B params
    "claude-haiku":         (0.0005, 0.0015),   # ~20B params
    "sonar-small":          (0.0003, 0.0010),   # ~8B (Llama 3.1)
    "sonar-large":          (0.0008, 0.0030),   # ~70B
    "sonar-huge":           (0.0015, 0.0060),   # ~405B
    "sonar-pro":            (0.0008, 0.0030),
    "sonar-reasoning-pro":  (0.0015, 0.0060),
    "sonar-reasoning":      (0.0008, 0.0030),
    "sonar":                (0.0003, 0.0010),
    "default":              (0.0008, 0.0030),
}

# Grid carbon intensity (gCO₂eq per kWh)
GRID_GWP = {
    "USA": 380.0,
    "CAN": 120.0,
    "QC":  30.0,
    "WOR": 475.0,
    "FRA": 56.0,
}

# Impact multipliers & ratios
WATER_PER_KWH = 0.015       # litres per kWh (datacenter + power generation)
EMBODIED_FRACTION = 0.20    # embodied GWP as fraction of total (20% embodied, 80% usage)
PE_PER_KWH = 9.0            # MJ per kWh (3:1 primary-to-delivered energy ratio)
ADPE_PER_KWH = 1.5e-7       # kgSbeq per kWh (abiotic depletion potential)
CACHE_READ_FACTOR = 0.10    # cache reads ≈ 10% of full input prefill energy
CACHE_CREATE_FACTOR = 1.20  # cache writes ≈ 120% of input prefill

_MODEL_ALIASES = {
    "claude-opus-4": "claude-3-opus",
    "claude-opus-4-7": "claude-3-opus",
    "claude-sonnet-4": "claude-3-5-sonnet",
    "claude-sonnet-4-7": "claude-3-5-sonnet",
    "claude-sonnet-4-20250514": "claude-3-5-sonnet",
    "claude-haiku-4": "claude-3-haiku",
}


@dataclass
class ImpactEstimate:
    energy_kwh: float
    gwp_kgco2eq: float
    wcf_l: float
    adpe_kgsbeq: float
    pe_mj: float
    source: str          # "ecologits" | "fallback"
    model: str
    warnings: list[str] = field(default_factory=list)


def is_ecologits_available() -> bool:
    """Check if the optional ecologits package is installed."""
    try:
        import ecologits  # noqa: F401
        return True
    except ImportError:
        return False


def get_grid_intensity(zone: str = "USA") -> float:
    """Return grid carbon intensity in gCO₂eq/kWh for a zone."""
    return GRID_GWP.get((zone or "USA").upper(), GRID_GWP["USA"])


def _rates_for_model(model: Optional[str]) -> Tuple[float, float]:
    """Resolve input/output energy rates (kWh per 1k tokens) for a given model identifier."""
    m = (model or "").lower().strip()
    if not m:
        return ENERGY_RATES["default"]

    # Specific prefixes first
    for key in (
        "sonar-reasoning-pro",
        "sonar-reasoning",
        "sonar-small",
        "sonar-large",
        "sonar-huge",
        "sonar-pro",
        "sonar",
        "claude-opus",
        "claude-sonnet",
        "claude-haiku",
    ):
        if key in m:
            return ENERGY_RATES[key]

    # Substring family match
    if "opus" in m:
        return ENERGY_RATES["claude-opus"]
    if "sonnet" in m:
        return ENERGY_RATES["claude-sonnet"]
    if "haiku" in m:
        return ENERGY_RATES["claude-haiku"]

    return ENERGY_RATES["default"]


def _try_ecologits(
    model: str,
    output_tokens: int,
    electricity_zone: str,
) -> Optional[Tuple[float, float, float, float, float]]:
    """Attempt estimation via ecologits if installed. Returns (energy, gwp, wcf, adpe, pe) or None."""
    try:
        from ecologits.impacts import compute_llm_impacts
        from ecologits.model_repository import ModelRepository
        from ecologits.electricity_mix_repository import ElectricityMixRepository

        # Normalize model name with aliases
        mapped_name = _MODEL_ALIASES.get(model, model)

        # Look up model
        model_repo = ModelRepository()
        model_info = None
        for lookup_fn in ("find_model", "from_name", "search", "get"):
            if hasattr(model_repo, lookup_fn):
                try:
                    res = getattr(model_repo, lookup_fn)(mapped_name, provider="anthropic")
                    if res:
                        model_info = res
                        break
                except Exception:
                    pass

        if not model_info:
            return None

        # Look up electricity mix
        emix_repo = ElectricityMixRepository()
        zone_info = None
        for lookup_fn in ("find_zone", "from_zone", "get", "search"):
            if hasattr(emix_repo, lookup_fn):
                try:
                    res = getattr(emix_repo, lookup_fn)(electricity_zone)
                    if res:
                        zone_info = res
                        break
                except Exception:
                    pass

        latency = max(0.05, output_tokens / 60.0)

        active_params = getattr(model_info, "active_parameter_count", getattr(model_info, "parameter_count", None))
        total_params = getattr(model_info, "total_parameter_count", active_params)

        if active_params is None or total_params is None:
            return None

        kwargs = {
            "model_active_parameter_count": active_params,
            "model_total_parameter_count": total_params,
            "output_token_count": output_tokens,
            "request_latency": latency,
        }
        if zone_info is not None:
            kwargs["electricity_mix"] = zone_info

        impacts = compute_llm_impacts(**kwargs)

        def _val(x):
            if hasattr(x, "value"):
                v = x.value
                return getattr(v, "mean", getattr(v, "value", float(v)))
            return float(x)

        return (
            _val(impacts.energy),
            _val(impacts.gwp),
            _val(impacts.wcf),
            _val(impacts.adpe),
            _val(impacts.pe),
        )
    except Exception:
        return None


def estimate_impacts(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
    cache_create_tokens: int = 0,
    electricity_zone: str = "USA",
) -> ImpactEstimate:
    """Estimate environmental impacts from model and token usage."""
    input_tokens = max(0, int(input_tokens or 0))
    output_tokens = max(0, int(output_tokens or 0))
    cache_read_tokens = max(0, int(cache_read_tokens or 0))
    cache_create_tokens = max(0, int(cache_create_tokens or 0))

    grid_gwp = get_grid_intensity(electricity_zone)
    input_rate, output_rate = _rates_for_model(model)

    # Input prefill energy calculation
    input_energy = (
        (input_tokens / 1000.0) * input_rate
        + (cache_read_tokens / 1000.0) * input_rate * CACHE_READ_FACTOR
        + (cache_create_tokens / 1000.0) * input_rate * CACHE_CREATE_FACTOR
    )

    warnings: List[str] = []
    ecologits_res = None

    if is_ecologits_available() and output_tokens > 0:
        ecologits_res = _try_ecologits(model, output_tokens, electricity_zone)
        if ecologits_res is None:
            warnings.append(f"ecologits calculation failed for model '{model}', fell back to standard formula")

    if ecologits_res is not None:
        eco_energy, eco_gwp, eco_wcf, eco_adpe, eco_pe = ecologits_res
        total_energy = input_energy + eco_energy
        # Combine ecologits output impacts with input prefill impacts
        input_gwp_usage = input_energy * grid_gwp / 1000.0
        input_gwp_embodied = input_gwp_usage * (EMBODIED_FRACTION / (1.0 - EMBODIED_FRACTION))
        total_gwp = eco_gwp + (input_gwp_usage + input_gwp_embodied)
        total_wcf = eco_wcf + (input_energy * WATER_PER_KWH)
        total_adpe = eco_adpe + (input_energy * ADPE_PER_KWH)
        total_pe = eco_pe + (input_energy * PE_PER_KWH)

        return ImpactEstimate(
            energy_kwh=round(total_energy, 9),
            gwp_kgco2eq=round(total_gwp, 9),
            wcf_l=round(total_wcf, 9),
            adpe_kgsbeq=round(total_adpe, 12),
            pe_mj=round(total_pe, 9),
            source="ecologits",
            model=model or "unknown",
            warnings=warnings,
        )

    # Simplified fallback formula
    output_energy = (output_tokens / 1000.0) * output_rate
    energy_kwh = input_energy + output_energy

    # GWP (kgCO₂eq): usage + embodied
    gwp_usage = energy_kwh * grid_gwp / 1000.0
    gwp_embodied = gwp_usage * (EMBODIED_FRACTION / (1.0 - EMBODIED_FRACTION))  # 0.20 / 0.80 = 0.25
    gwp_total = gwp_usage + gwp_embodied

    wcf = energy_kwh * WATER_PER_KWH
    pe = energy_kwh * PE_PER_KWH
    adpe = energy_kwh * ADPE_PER_KWH

    return ImpactEstimate(
        energy_kwh=round(energy_kwh, 9),
        gwp_kgco2eq=round(gwp_total, 9),
        wcf_l=round(wcf, 9),
        adpe_kgsbeq=round(adpe, 12),
        pe_mj=round(pe, 9),
        source="fallback",
        model=model or "unknown",
        warnings=warnings,
    )


def estimate_for_usage(usage: dict, model: str, electricity_zone: str = "USA") -> ImpactEstimate:
    """Mirror pricing.cost_for(): compute impacts from a standard usage dict."""
    u = usage or {}
    cache_create = (
        int(u.get("cache_create_5m_tokens") or 0)
        + int(u.get("cache_create_1h_tokens") or 0)
        + int(u.get("cache_create_tokens") or 0)
    )
    return estimate_impacts(
        model=model,
        input_tokens=int(u.get("input_tokens") or 0),
        output_tokens=int(u.get("output_tokens") or 0),
        cache_read_tokens=int(u.get("cache_read_tokens") or 0),
        cache_create_tokens=cache_create,
        electricity_zone=electricity_zone,
    )


def aggregate_impacts(estimates: list[ImpactEstimate]) -> dict:
    """Aggregate a list of ImpactEstimate objects into summary totals."""
    total_energy = 0.0
    total_gwp = 0.0
    total_wcf = 0.0
    total_adpe = 0.0
    total_pe = 0.0
    ecologits_count = 0
    fallback_count = 0

    for e in estimates:
        total_energy += e.energy_kwh
        total_gwp += e.gwp_kgco2eq
        total_wcf += e.wcf_l
        total_adpe += e.adpe_kgsbeq
        total_pe += e.pe_mj
        if e.source == "ecologits":
            ecologits_count += 1
        else:
            fallback_count += 1

    return {
        "energy_kwh": round(total_energy, 9),
        "gwp_kgco2eq": round(total_gwp, 9),
        "wcf_l": round(total_wcf, 9),
        "adpe_kgsbeq": round(total_adpe, 12),
        "pe_mj": round(total_pe, 9),
        "ecologits_count": ecologits_count,
        "fallback_count": fallback_count,
        "total_count": len(estimates),
    }


def calculate_efficiency(useful_tokens: int, energy_kwh: float, grid_gwp: float = 380.0) -> dict:
    """Compute Lean Six Sigma / Centrale 73 efficiency metrics:

    efficiency = useful_work / energy_input
    """
    useful_tokens = max(0, int(useful_tokens or 0))
    energy_wh = max(0.0, float(energy_kwh or 0.0)) * 1000.0
    energy_joules = energy_wh * 3600.0
    gco2 = (energy_kwh * grid_gwp / 0.80) if energy_kwh > 0 else 0.0  # total including embodied

    tokens_per_wh = (useful_tokens / energy_wh) if energy_wh > 0 else 0.0
    joules_per_token = (energy_joules / useful_tokens) if useful_tokens > 0 else 0.0
    gco2_per_ktoken = (gco2 / (useful_tokens / 1000.0)) if useful_tokens > 0 else 0.0

    return {
        "useful_tokens": useful_tokens,
        "energy_wh": round(energy_wh, 4),
        "tokens_per_wh": round(tokens_per_wh, 2),
        "joules_per_token": round(joules_per_token, 3),
        "gco2_per_ktoken": round(gco2_per_ktoken, 4),
    }


def migrate_db(db_path: Union[str, Path]) -> None:
    """Add environmental impact columns to the messages table if they do not exist."""
    path = Path(db_path)
    if not path.exists():
        return
    with sqlite3.connect(path) as conn:
        has_table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='messages'"
        ).fetchone()
        if not has_table:
            return

        cols = {row[1] for row in conn.execute("PRAGMA table_info(messages)")}

        new_columns = [
            ("energy_kwh", "REAL DEFAULT 0"),
            ("gwp_kgco2eq", "REAL DEFAULT 0"),
            ("wcf_l", "REAL DEFAULT 0"),
            ("adpe_kgsbeq", "REAL DEFAULT 0"),
            ("pe_mj", "REAL DEFAULT 0"),
            ("impact_source", "TEXT DEFAULT ''"),
        ]

        for col_name, col_type in new_columns:
            if col_name not in cols:
                conn.execute(f"ALTER TABLE messages ADD COLUMN {col_name} {col_type}")
        conn.commit()


def backfill_impacts(
    db_path: Union[str, Path],
    electricity_zone: str = "USA",
    batch_size: int = 500,
) -> int:
    """Populate environmental impact columns for existing rows with missing estimates.

    Returns the number of updated records.
    """
    migrate_db(db_path)
    path = Path(db_path)
    updated_count = 0

    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        while True:
            rows = conn.execute(
                """
                SELECT uuid, model, input_tokens, output_tokens,
                       cache_read_tokens, cache_create_5m_tokens, cache_create_1h_tokens
                  FROM messages
                 WHERE impact_source IS NULL OR impact_source = ''
                 LIMIT ?
                """,
                (batch_size,),
            ).fetchall()

            if not rows:
                break

            updates = []
            for r in rows:
                impact = estimate_impacts(
                    model=r["model"],
                    input_tokens=r["input_tokens"],
                    output_tokens=r["output_tokens"],
                    cache_read_tokens=r["cache_read_tokens"],
                    cache_create_tokens=(r["cache_create_5m_tokens"] or 0) + (r["cache_create_1h_tokens"] or 0),
                    electricity_zone=electricity_zone,
                )
                updates.append((
                    impact.energy_kwh,
                    impact.gwp_kgco2eq,
                    impact.wcf_l,
                    impact.adpe_kgsbeq,
                    impact.pe_mj,
                    impact.source,
                    r["uuid"],
                ))

            conn.executemany(
                """
                UPDATE messages
                   SET energy_kwh = ?,
                       gwp_kgco2eq = ?,
                       wcf_l = ?,
                       adpe_kgsbeq = ?,
                       pe_mj = ?,
                       impact_source = ?
                 WHERE uuid = ?
                """,
                updates,
            )
            conn.commit()
            updated_count += len(updates)

    return updated_count
