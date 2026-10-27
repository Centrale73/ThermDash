"""Markdown report and matplotlib figures (matplotlib optional) for n8n audits."""
from __future__ import annotations

from pathlib import Path


def _coverage_line(r: dict) -> str:
    t = (r.get("coverage") or {}).get("temporal")
    cap = (r.get("coverage") or {}).get("count_cap_loss", 0.0)
    if not t:
        return f"Coverage: item-cap loss {cap:.2%}; no look-back window detected."
    return (f"Coverage K (service metric, not in eta): {t['uniform']:.1%} uniform arrivals, "
            f"{t['business_hours']:.1%} business hours, window redundancy {t['redundancy']:.2f}; "
            f"item-cap loss {cap:.2%}.")


def fmt(x: float) -> str:
    if x == 0:
        return "0"
    if abs(x) >= 1e4 or abs(x) < 1e-2:
        return f"{x:.2e}"
    return f"{x:.3g}"


def report_md(r: dict) -> str:
    L = [f"# thermaudit: {r['workflow']}", "",
         f"Inference node `{r['inference_node']}`, model `{r['model']}`, tokenizer {r['tokenizer']}.",
         f"Runs/year {r['runs_per_year']} ({r['schedule_basis']}); P(inference | run) = {r['p_inference_per_run']:.3f}.",
         f"Tokens per inference: n_in mean {r['tokens']['n_in']['mean']:.0f} (p5 {r['tokens']['n_in']['p5']}, p95 {r['tokens']['n_in']['p95']}); "
         f"n_out mean {r['tokens']['n_out']['mean']:.0f}.",
         f"Q = {r['quality']['Q']:.3f} (quality_adjusted = {r['quality']['quality_adjusted']}); D = {r['delivery_p_success']}.",
         f"Cost: ${r['cost_usd']['per_inference']:.5f} per inference, ${r['cost_usd']['per_year']:.3f} per year"
         + (f" (workflow claims ${r['cost_usd']['claimed_per_run']}/run: {r['cost_usd']['claim_overstatement_x']:.0f}x overstated)."
            if r['cost_usd'].get('claimed_per_run') else "."),
         _coverage_line(r), "",
         "| Scenario | Tier | E_in mean (J) | E_in p5-p95 (J) | eta_max | eta_model | eta_sys | Facility kWh/yr | CO2 g/yr |",
         "|---|---|---|---|---|---|---|---|---|"]
    for k, s in r["scenarios"].items():
        a = s["annual"]
        L.append(f"| {s['label']} | {s['tier']} | {fmt(s['E_in_J']['mean'])} | {fmt(s['E_in_J']['p5'])}-{fmt(s['E_in_J']['p95'])} | "
                 f"{s['eta_max_Q1']:.3f} | {s['eta_model']:.3f} | {s['eta_system_delivered']:.3f} | {fmt(a['facility_kWh'])} | {fmt(a['co2_g'])} |")
    L += ["", "| Scenario | J per useful item | Host kWh/yr | LLM share of boundary | eta_boundary |", "|---|---|---|---|---|"]
    for s in r["scenarios"].values():
        b = s.get("boundary")
        L.append(f"| {s['label']} | {fmt(s['J_per_useful_item'])} | "
                 + (f"{fmt(b['host_kWh_per_year'])} | {fmt(100 * b['llm_share_of_boundary'])}% | {fmt(b['eta_boundary'])} |" if b else "- | - | - |"))
    L += ["", "## Flags"]
    for lvl, items in r["audit"]["flags"].items():
        for i in items:
            L.append(f"- [{lvl}] {i}")
    L += ["", "```mermaid", r["audit"]["mermaid"], "```"]
    return "\n".join(L) + "\n"


def figures(r: dict, out: Path) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    plt.rcParams.update({"font.size": 10, "axes.spines.top": False, "axes.spines.right": False})
    sc = r["scenarios"]
    labels = [s.get("short_label") or s["label"].split(" (")[0].replace("ThermDash ", "TD ") for s in sc.values()]
    tiers = [s["tier"] for s in sc.values()]
    colors = {"A": "#1b5e20", "B": "#1565c0", "C": "#9e9e9e"}
    # Fig 1: energy per inference, log scale, with p5-p95 whiskers
    fig, ax = plt.subplots(figsize=(7.2, 3.8))
    m = [s["E_in_J"]["mean"] for s in sc.values()]
    lo = [s["E_in_J"]["mean"] - s["E_in_J"]["p5"] for s in sc.values()]
    hi = [s["E_in_J"]["p95"] - s["E_in_J"]["mean"] for s in sc.values()]
    ax.barh(labels, m, xerr=[lo, hi], color=[colors[t] for t in tiers], capsize=3)
    ax.set_xscale("log"); ax.invert_yaxis()
    ax.set_xlabel("Server-side energy per inference, E_in (J, log scale)")
    for i, v in enumerate(m):
        ax.text(v * 1.6, i, f"{fmt(v)} J  [tier {tiers[i]}]", va="center", fontsize=8)
    ax.set_xlim(min(m) / 5, max(m) * 80)
    fig.tight_layout(); fig.savefig(out / "fig1_energy_per_inference.png", dpi=200); plt.close(fig)
    # Fig 2: eta decomposition
    fig, ax = plt.subplots(figsize=(7.2, 3.6))
    x = range(len(sc))
    w = 0.27
    ax.bar([i - w for i in x], [s["eta_max_Q1"] for s in sc.values()], w, label="η_max (Q=1, D=1)", color="#90a4ae")
    ax.bar(list(x), [s["eta_model"] for s in sc.values()], w, label="η_model (Q prior)", color="#ef6c00")
    ax.bar([i + w for i in x], [s["eta_system_delivered"] for s in sc.values()], w, label="η_sys (Q prior, D)", color="#b71c1c")
    ax.set_xticks(list(x)); ax.set_xticklabels([l.replace("\n", " ") for l in labels], rotation=18, ha="right", fontsize=8)
    ax.set_ylim(0, 1); ax.set_ylabel("Efficiency η = W_useful / E_in")
    ax.legend(fontsize=8, frameon=False, loc="upper left")
    fig.tight_layout(); fig.savefig(out / "fig2_eta_by_scenario.png", dpi=200); plt.close(fig)
    # Fig 3: system boundary (annual facility kWh): LLM vs always-on n8n host, side by side
    if all("boundary" in s for s in sc.values()):
        fig, ax = plt.subplots(figsize=(7.2, 4.0))
        llm = [s["annual"]["facility_kWh"] for s in sc.values()]
        hostk = [s["boundary"]["host_kWh_per_year"] for s in sc.values()]
        y = list(range(len(sc)))
        h = 0.38
        ax.barh([i - h / 2 for i in y], llm, h, color=[colors[t] for t in tiers], label="LLM inference (facility, PUE applied)")
        ax.barh([i + h / 2 for i in y], hostk, h, color="#546e7a", label="n8n host, always-on (assumed)")
        for i in y:
            ax.text(llm[i] * 1.5, i - h / 2, f"{fmt(llm[i])} kWh", va="center", fontsize=7.5)
            ax.text(hostk[i] * 1.5, i + h / 2, f"{fmt(hostk[i])} kWh", va="center", fontsize=7.5)
        ax.set_yticks(y); ax.set_yticklabels(labels, fontsize=8.5)
        ax.set_xscale("log"); ax.invert_yaxis()
        ax.set_xlim(min(llm) / 5, max(hostk) * 60)
        ax.set_xlabel("Annual energy (kWh, log scale)")
        ax.legend(fontsize=8, frameon=False, loc="lower center", bbox_to_anchor=(0.5, 1.0), ncol=2)
        fig.tight_layout(); fig.savefig(out / "fig3_system_boundary.png", dpi=200); plt.close(fig)
