"""Monte Carlo energy / efficiency engine for one inference node of an n8n workflow.

Formulas (docs/specs/thermodynamic-efficiency-spec.md section 2; PDF eqs. 3-5):
  E_in     = kappa * (n_in * e_prefill + n_out * e_decode) + c       (linear form)
  E_in     = theta . [n_out, n_in^2, n_in*n_out, n_in, n_out^2, 1]   (theta form, Cavagna 2026)
  W_useful = Q * E_out,   Q = (w_a*alpha + w_p*rho) / (w_a + w_p)
  eta      = W_useful / E_in
Extensions (docs/specs/n8n-thermaudit-method.md):
  delivery gate D   W_delivered = D * W_useful
  coverage K        service metric only, never multiplies eta
  boundary          eta_boundary = W_delivered / (E_llm_facility + E_host)
"""
from __future__ import annotations

import datetime as dt
import json
import math
import random
import re
import statistics
from pathlib import Path

from token_dashboard.heat import load_utility_rates
from token_dashboard.quality_scores import QualityScore, composite_q as td_composite_q
from token_dashboard.thermodynamics import EnergyConstants, energy_input

from .graph import split_template
from .scan import scan

DATA = Path(__file__).resolve().parent.parent / "data"
SCENARIOS_JSON = DATA / "n8n_energy_scenarios.json"
CHAT_OVERHEAD_PER_MSG, CHAT_REPLY_PRIMING = 3, 3  # OpenAI chat-format convention


class Tokenizer:
    """tiktoken when installed (optional), else the chars/4 heuristic. Offline-safe."""

    def __init__(self, model: str | None):
        self.method, self.enc = "heuristic chars/4", None
        try:
            import tiktoken  # type: ignore
            m = (model or "").lower()
            name = "cl100k_base" if re.match(r"gpt-(3\.5|4)(?!o|\.1)", m) else "o200k_base"
            self.enc = tiktoken.get_encoding(name)
            proxy = not m.startswith(("gpt-", "o1", "o3", "o4"))
            self.method = f"tiktoken {name}" + (" (proxy for non-OpenAI model)" if proxy else "")
        except Exception:
            pass

    def count(self, text: str) -> int:
        if self.enc:
            return len(self.enc.encode(text))
        return max(1, math.ceil(len(text) / 4))


def load_scenarios(path: Path | None = None) -> dict:
    return json.loads(Path(path or SCENARIOS_JSON).read_text(encoding="utf-8"))["scenarios"]


def energy_split(n_in: int, n_out: int, sc: dict) -> tuple[float, float, float]:
    """Return (E_prefill, E_out, E_fixed) in server-side joules (PUE divided out)."""
    k = float(sc.get("kappa", 1.0))
    pue = float(sc.get("pue_included", 1.0))
    if sc["form"] == "theta":
        t0, t1, t2, t3, t4, t5 = sc["theta"]
        e_out = t0 * n_out + t2 * n_in * n_out + t4 * n_out ** 2  # decode attends over context
        e_pre = t1 * n_in ** 2 + t3 * n_in
        return k * e_pre / pue, k * e_out / pue, k * t5 / pue
    # Linear form: reuse ThermDash's own E_in so both programs share one definition.
    pre_c = EnergyConstants(model="", gpu="", e_prefill=sc["e_prefill"], e_decode=0.0, kappa=k)
    dec_c = EnergyConstants(model="", gpu="", e_prefill=0.0, e_decode=sc["e_decode"], kappa=k)
    return (energy_input(n_in, 0, pre_c) / pue, energy_input(0, n_out, dec_c) / pue,
            k * sc.get("c", 0.0) / pue)


def quality(q: dict | None) -> tuple[float, bool]:
    """ThermDash v1 rule: no score -> Q = 1, quality_adjusted = False. Never mix the two."""
    if not q:
        return 1.0, False
    qs = QualityScore(alpha=q["alpha"], rho=q["rho"], method=q.get("method", "prior"),
                      w_a=q.get("w_a", 0.5), w_p=q.get("w_p", 0.5))
    return td_composite_q(qs), True


def poisson(lam: float, rng: random.Random) -> int:
    if lam <= 0:
        return 0
    L, k, p = math.exp(-lam), 0, 1.0
    while True:
        p *= rng.random()
        if p <= L:
            return k
        k += 1


def pct(xs: list[float], q: float) -> float:
    s = sorted(xs)
    return s[min(len(s) - 1, int(q * (len(s) - 1)))] if s else 0.0


def _calibrate_output(out_cfg: dict, tok: Tokenizer) -> dict:
    out_cfg = dict(out_cfg)
    cal = out_cfg.pop("calibration_text", None)
    if cal:
        out_cfg.setdefault("fixed_tokens", tok.count(cal["fixed"]))
        out_cfg.setdefault("per_item_tokens", statistics.mean(tok.count(x) for x in cal["item_lines"]))
    return out_cfg


def simulate(cfg: dict, site: dict, tok: Tokenizer, item_tok: list[int], static_tokens: int,
             out_cfg: dict) -> list[tuple | None]:
    """One draw per scheduled run: (n_raw, n_eff, n_in, n_out, delivered) or None if skipped."""
    it, mc = cfg["items"], cfg.get("monte_carlo", {"n": 20000, "seed": 7})
    rng = random.Random(mc.get("seed", 7))
    d_p = cfg.get("delivery", {}).get("p_success", 1.0)
    frac = out_cfg.get("item_fraction_listed", 1.0)
    draws = []
    mix = it.get("mixture") or [{"weight": 1.0, "mean": it["mean"]}]  # e.g. Monday catch-up runs
    weights = [m["weight"] for m in mix]
    for _ in range(mc["n"]):
        lam = rng.choices(mix, weights)[0]["mean"] if len(mix) > 1 else mix[0]["mean"]
        n_raw = poisson(lam * it.get("redundancy", 1.0), rng)
        n_eff = min(n_raw, it.get("cap", 10 ** 9))
        if n_eff == 0 and site.get("guarded_by_if", True):
            draws.append(None)  # IF gate (or empty Gmail output) stops before inference
            continue
        n_in = static_tokens + len(str(n_eff)) + sum(rng.choice(item_tok) for _ in range(n_eff))
        listed = sum(rng.random() < frac for _ in range(n_eff))
        n_out = int(round(out_cfg["fixed_tokens"] + out_cfg["per_item_tokens"] * listed))
        draws.append((n_raw, n_eff, n_in, n_out, rng.random() < d_p))
    return draws


def scenario_result(sc: dict, reg: dict, called: list[tuple], q_val: float, d_p: float,
                    p_call: float, runs: int, host: dict | None, redundancy: float = 1.0) -> dict:
    E_in, E_out, Wd = [], [], []
    for _, _, n_in, n_out, delivered in called:
        pre, out, fix = energy_split(n_in, n_out, sc)
        E_in.append(pre + out + fix)
        E_out.append(out)
        Wd.append(q_val * out * delivered)
    e_mean, eo_mean = statistics.mean(E_in), statistics.mean(E_out)
    kwh = e_mean * p_call * runs / 3.6e6
    fac = kwh * reg["pue"]
    r = {"label": sc["label"], "short_label": sc.get("short_label"), "tier": sc["tier"], "form": sc["form"], "hardware": sc.get("hardware"),
         "provenance": sc["provenance"], "region": reg["name"],
         "E_in_J": {"mean": e_mean, "p5": pct(E_in, .05), "p95": pct(E_in, .95)},
         "E_out_J_mean": eo_mean,
         "eta_max_Q1": eo_mean / e_mean,
         "eta_model": q_val * eo_mean / e_mean,
         "eta_system_delivered": statistics.mean(Wd) / e_mean,
         "waste_J_per_inference": e_mean - statistics.mean(Wd),
         "annual": {"server_kWh": kwh, "facility_kWh": fac, "heat_rejected_MJ": fac * 3.6,
                    "heat_dollars_usd": fac * reg["rate_usd_per_kwh"],
                    "co2_g": fac * reg["g_co2_per_kwh"]},
         # Duplicates from overlapping look-back windows are not new useful work.
         "J_per_useful_item": e_mean / max(1e-12, statistics.mean(d[1] for d in called) / redundancy * q_val * d_p)}
    if host:
        host_kwh = host["power_w"] * 8760 / 1000 / max(1, host.get("workflows_sharing", 1))
        r["boundary"] = {"host_kWh_per_year": host_kwh, "llm_share_of_boundary": fac / (fac + host_kwh),
                         "eta_boundary": r["eta_system_delivered"] * fac / (fac + host_kwh)}
    return r


def run(cfg_path: Path) -> dict:
    """Execute a config (paths inside are relative to the config file)."""
    cfg_path = Path(cfg_path)
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    base = cfg_path.parent
    wf = json.loads((base / cfg["workflow"]).read_text(encoding="utf-8"))
    year = cfg.get("year", dt.date.today().year)
    sc_all = load_scenarios(base / cfg["scenarios_file"] if cfg.get("scenarios_file") else None)
    rates = load_utility_rates()["profiles"]
    audit = scan(wf, cfg.get("jurisdiction", "gdpr"), year)
    site = next(s for s in audit["inference_sites"] if s["node"] == cfg["inference_node"])
    model = cfg.get("model") or site["model"]
    tok = Tokenizer(model)

    static_tokens = CHAT_REPLY_PRIMING
    for m in site["prompts"]:
        static_tokens += tok.count(split_template(m["content"])[0]) + CHAT_OVERHEAD_PER_MSG
    samples = cfg.get("item_samples") or json.loads(
        (base / cfg["item_samples_file"]).read_text(encoding="utf-8"))
    item_tok = [tok.count(s) + 1 for s in samples]  # +1 for the join newline
    out_cfg = _calibrate_output(cfg["output_model"], tok)
    runs = cfg.get("runs_per_year") or audit["schedule"]["runs_per_year"]
    q_val, q_adj = quality(cfg.get("quality"))
    d_p = cfg.get("delivery", {}).get("p_success", 1.0)
    price = cfg.get("pricing", {"input_per_m": 0.15, "output_per_m": 0.60})

    draws = simulate(cfg, site, tok, item_tok, static_tokens, out_cfg)
    called = [d for d in draws if d]
    if not called:
        raise ValueError("No run reached the inference node; raise items.mean.")
    p_call = len(called) / len(draws)
    cost = statistics.mean(d[2] * price["input_per_m"] / 1e6 + d[3] * price["output_per_m"] / 1e6
                           for d in called)
    res = {"workflow": audit["name"], "inference_node": site["node"], "model": model,
           "tokenizer": tok.method, "static_prompt_tokens": static_tokens,
           "item_tokens": {"mean": statistics.mean(item_tok), "min": min(item_tok),
                           "max": max(item_tok), "n_samples": len(item_tok)},
           "output_model": out_cfg, "items": cfg["items"],
           "runs_per_year": runs, "schedule_basis": audit["schedule"]["basis"],
           "p_inference_per_run": p_call,
           "tokens": {k: {"mean": statistics.mean(d[i] for d in called),
                          "p5": pct([d[i] for d in called], .05), "p95": pct([d[i] for d in called], .95)}
                      for k, i in (("n_in", 2), ("n_out", 3))},
           "coverage": {"temporal": audit.get("coverage"),
                        "count_cap_loss": sum(d[0] - d[1] for d in called) / max(1, sum(d[0] for d in called))},
           "quality": {"Q": q_val, "quality_adjusted": q_adj, **(cfg.get("quality") or {})},
           "delivery_p_success": d_p,
           "cost_usd": {"per_inference": cost, "per_year": cost * p_call * runs},
           "scenarios": {}, "audit": audit}
    if cfg.get("claimed_cost_per_run_usd"):
        res["cost_usd"]["claimed_per_run"] = cfg["claimed_cost_per_run_usd"]
        res["cost_usd"]["claim_overstatement_x"] = cfg["claimed_cost_per_run_usd"] / cost
    region_key = cfg.get("region", "global_baseline")
    for key in cfg.get("energy_scenarios") or list(sc_all):
        reg = rates[cfg.get("region_overrides", {}).get(key, region_key)]
        res["scenarios"][key] = scenario_result(sc_all[key], reg, called, q_val, d_p, p_call, runs,
                                                cfg.get("host"), cfg["items"].get("redundancy", 1.0))
    head = cfg.get("headline_scenario") or next(iter(res["scenarios"]))
    res["headline_scenario"] = head
    em = res["scenarios"][head]["eta_max_Q1"]
    res["sensitivity"] = {"scenario": head, "grid": [
        {"Q": q, "D": d, "eta": em * q * d}
        for q in (1.0, 0.9, 0.75, 0.5, 0.25) for d in (1.0, 0.95, 0.8, 0.0)]}
    return res
