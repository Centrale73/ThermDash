# n8n-thermaudit: method specification

Status: v0.1 (branch `feature/n8n-thermaudit`). Package: `token_dashboard/n8n_audit/`. CLI: `python3 cli.py n8n-audit inspect|run`.

This program audits one inference node of an exported n8n workflow for energy, heat and quality-adjusted efficiency. It merges two Centrale73 codebases:

| Origin | What was reused | Where it lives now |
|---|---|---|
| ThermDash-AutoAudit `src/nexus_waste_detector.py` | vendor alias map, cloud-AI domain list, n8n edge walk, Mermaid graph, governance flags | `n8n_audit/graph.py`, `n8n_audit/scan.py` |
| ThermDash `thermodynamics.py` | `EnergyConstants`, `energy_input()` (linear E_in) | called from `n8n_audit/engine.py` |
| ThermDash `quality_scores.py` | `QualityScore`, `composite_q()` | called from `n8n_audit/engine.py` |
| ThermDash `heat.py` | `load_utility_rates()` and `data/utility_rates.json` (PUE, $/kWh, gCO2/kWh) | called from `n8n_audit/engine.py` |

Stdlib only. `tiktoken` (exact BPE counts) and `matplotlib` (figures) are used when present and skipped otherwise.

## 1. Control volume

The default control volume is the inference server serving one request (IT energy). Two optional wider boundaries are reported separately and never mixed into the headline:

1. Facility: server energy times region PUE (heat rejected to the environment).
2. Orchestration boundary: facility LLM energy plus the always-on n8n host (`host.power_w`, shared across `workflows_sharing`).

## 2. Equations

Token model (per run):

\[
n_{in} = n_{static} + \mathrm{len}(\mathrm{str}(N)) + \sum_{i=1}^{N} t_i, \qquad
n_{out} = t_{fixed} + t_{item}\cdot \mathrm{Binomial}(N, f_{listed})
\]

`n_static` counts only the literal text of each prompt (n8n `{{ }}` expressions are removed), plus 3 tokens per chat message and 3 reply-priming tokens. `t_i` is drawn from rendered item strings (the exact format of the upstream Code node). `N = min(Poisson(λ·redundancy), cap)`, or a mixture of Poissons for catch-up runs.

Energy, linear form (ThermDash spec §2 plus a fixed per-request term):

\[
E_{in} = \kappa\,(n_{in} e_{prefill} + n_{out} e_{decode}) + c
\]

Energy, θ form (Cavagna et al. 2026):

\[
E_{in} = \theta_0 n_{out} + \theta_1 n_{in}^2 + \theta_2 n_{in} n_{out} + \theta_3 n_{in} + \theta_4 n_{out}^2 + \theta_5
\]

with \(E_{out} = \theta_0 n_{out} + \theta_2 n_{in} n_{out} + \theta_4 n_{out}^2\) (decode attends over the context). Published values that include PUE are divided by `pue_included` before use.

Useful work and efficiency (PDF eqs. 4–5):

\[
W_{useful} = Q\,E_{out}, \quad Q = \frac{w_a\alpha + w_p\rho}{w_a + w_p}, \quad \eta = \frac{W_{useful}}{E_{in}}
\]

Delivery gate (extension): \(W_{delivered} = D\,W_{useful}\), with \(D \in \{0,1\}\) per run (Bernoulli `p_success`). Energy spent on output that never reaches a human is waste. A placeholder in the delivery node forces D = 0.

Reported efficiencies:

| Symbol | Definition | Meaning |
|---|---|---|
| η_max | E_out / E_in | ceiling with Q = D = 1, set by the prefill/decode split |
| η_model | Q · η_max | ThermDash η |
| η_sys | E[Q · E_out · D] / E[E_in] | η after delivery |
| η_boundary | η_sys · E_LLM / (E_LLM + E_host) | η at the orchestration boundary |
| J per useful item | E_in / (N̄ / redundancy · Q · D) | functional-unit metric, the one to compare designs on |

Annual totals: \(E_{yr} = \bar E_{in}\,P(\text{inference}\mid\text{run})\,R\), with R from cron enumeration. Facility kWh = E_yr·PUE; heat rejected (MJ) = 3.6·facility kWh (first law: all of it becomes heat); heat-dollars = facility kWh·rate; CO2 = facility kWh·g/kWh.

## 3. Coverage K (service metric, never in η)

For a cron schedule with a Gmail `newer_than:` look-back window W, K is the fraction of the year whose arrivals fall inside at least one window [t − W, t). Reported for uniform arrivals and for business hours (Mon–Fri 09–18). Unprocessed items cost no inference energy, so a coverage gap is a service defect, not energy waste. Window redundancy (mean number of runs that see a covered arrival) is the opposite case: re-sending still-unread items is pure prefill waste, and it does inflate E.

## 4. Scenario tiers

`token_dashboard/data/n8n_energy_scenarios.json` labels every constant set:

- A: measured on hardware and published, possibly for another model (use as counterfactual).
- B: published top-down estimate for the audited model.
- C: unverified constant shipped in ThermDash (missing provenance or citation that does not contain the value). Report, never headline.

## 5. Interpretation rules

1. With c = 0, η_max = r·n_out / (n_in + r·n_out), r = e_decode/e_prefill. It is independent of absolute J/token and of κ. η can look stable while E_in spans five orders of magnitude, so always report E_in and J per useful item next to η.
2. η penalises prefill-heavy tasks such as summarisation by construction. Compare designs on J per useful item, or on η/η_max = Q·D.
3. η is an allocation ratio, not a thermodynamic efficiency. The Landauer floor (kT ln 2 ≈ 2.9e-21 J/bit at 300 K) for a ~150-token output is around 1e-17 J, 18 to 20 orders of magnitude below E_in (22 J to 1 kJ).
4. Never average quality-adjusted and unadjusted η (ThermDash v1 rule). Unscored runs use Q = 1 and `quality_adjusted = false`.
5. Never headline a tier-C scenario.

## 6. Known gaps

- `heat.calculate_heat_dollars()` rounds facility kWh to 6 dp and dollars to 6 dp. Below about 1 Wh/yr it returns $0 and up to +13% kWh error. This module therefore computes heat terms directly from `load_utility_rates()`.
- Three gpt-4o-mini constant sets in ThermDash disagree by about 5 orders of magnitude (`energy.json`, `data/energy_benchmarks.json`, `ecologits_bridge.ENERGY_RATES['default']`). See the case study.
- Quebec grid intensity: `utility_rates.json` 1.7 g/kWh vs `ecologits_bridge` GRID_GWP 30 g/kWh.
- Batch-1 local inference is not modelled. The Cavagna θ fit is for batched TensorRT-LLM on an H100, so it is a lower bound for a self-hosted 8B model.
