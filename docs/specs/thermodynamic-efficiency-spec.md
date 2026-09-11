# Delegation Brief: Thermodynamic Efficiency Engine for ThermDash

> **How to use this document:** Open the ThermDash workspace in Antigravity. Paste this file at `docs/specs/thermodynamic-efficiency-spec.md`, add the pointer line from §9 into `AGENTS.md` (or `CLAUDE.md`), then start a Planning-mode session and tell the agent: *"Read `docs/specs/thermodynamic-efficiency-spec.md` and execute it."* Review its implementation plan and task list before approving execution.

---

## 1. Objective

Upgrade ThermDash from an EcoLogiT-style environmental-impact estimator into a **quality-adjusted thermodynamic efficiency auditor** for LLM inference. The core deliverable is a new computation layer that implements, per response:

- **Energy input** \(E_{in}\) — joules actually consumed, computed from model identity, separate input/output token counts, and per-model-GPU energy constants.
- **Useful work** \(W_{useful}\) — energy gated by output quality, so hallucinated, padded, or irrelevant output is discounted toward zero.
- **Efficiency** \(\eta = W_{useful} / E_{in}\) — the headline metric.

Do **not** remove or break the existing EcoLogiT bridge; layer the new engine alongside it and make it the default when energy constants are available.

## 2. The Formulas (implement exactly)

### 2.1 Energy input

$$E_{in} = \kappa_{model} \cdot \left( n_{in} \cdot e_{prefill} + n_{out} \cdot e_{decode} \right)$$

Where:

- \(n_{in}\), \(n_{out}\): input and output token counts, tracked **separately** (prefill and decode have different energy signatures; decode dominates — output-heavy prompts can consume up to 11× the energy of input-heavy ones on an A100).
- \(e_{prefill}\), \(e_{decode}\): empirically measured energy per token (J/token) for the specific model–GPU pair. These are lookup constants, **never guessed** — energy per token spans ~3 orders of magnitude (0.003–1 J) across LLM/GPU combinations.
- \(\kappa_{model}\): model-size scaling factor accounting for per-forward-pass power draw; defaults to 1.0 when the table has direct measurements for the model, otherwise interpolated from parameter count.

### 2.2 Useful work

$$W_{useful} = Q \cdot E_{out}$$

Where \(E_{out} = \kappa_{model} \cdot n_{out} \cdot e_{decode}\) (energy attributable to generating the output tokens), and \(Q \in [0,1]\) is a quality-gating function:

$$Q = Q(\alpha, \rho) = w_a \cdot \alpha + w_p \cdot \rho, \quad w_a + w_p = 1, \quad \text{defaults } w_a = w_p = 0.5$$

- \(\alpha\) — **accuracy/correctness**: score against ground truth, a task benchmark (exact match, functional correctness for code, human-graded factuality), or an LLM-judge rubric.
- \(\rho\) — **precision/relevance**: fraction of output actually requested, versus padding, repetition, sycophantic filler, or irrelevant elaboration.

**v1 default (must be supported):** when no scoring data exists, set \(Q = 1\) and mark every \(\eta\) computed this way with `quality_adjusted: false`. This is the documented "simplified v1" — never silently mix quality-adjusted and unadjusted values in an aggregate.

### 2.3 Efficiency

$$\eta = \frac{W_{useful}}{E_{in}} = \frac{Q \cdot E_{out}}{E_{in}}$$

Derived diagnostics to expose alongside: **waste** \(= E_{in} - W_{useful}\) (joules), and **waste share** \(= 1 - \eta\).

## 3. Repository Context (current state)

The repo is Python (MIT), single `main` branch. Relevant existing modules:

| File | Role | What changes |
|---|---|---|
| `token_dashboard/ecologits_bridge.py` | Current impact estimator | Keep; add fallback chain: thermodynamic engine → EcoLogiT → null |
| `token_dashboard/db.py` | Storage (~20 KB) | Schema migration + new query functions |
| `token_dashboard/scanner.py` | Walks/parses local session logs, with dedup/rescan | Extract and persist \(n_{in}\), \(n_{out}\), model id per message if not already captured |
| `token_dashboard/server.py` | Web server | New endpoints (see §6) |
| `token_dashboard/pricing.py`, `pricing.json` | Cost lookup | Pattern reference for the new energy table |
| `token_dashboard/tips.py`, `skills.py` | User guidance | Extend tips to reference \(\eta\) and waste |
| `cli.py` | CLI entry | New `thermo report` subcommand |
| `web/routes/environmental.js` | Environmental dashboard route | New efficiency panel |
| `web/charts.js`, `web/app.js` | ECharts rendering | New chart types |
| `tests/` | pytest, 15 files | New test modules following existing style |

Before implementing, read `CLAUDE.md`, `CONTRIBUTING.md`, and `docs/KNOWN_LIMITATIONS.md` and respect every constraint stated there.

## 4. New Module: `token_dashboard/thermodynamics.py`

Pure computation, no I/O, fully typed and documented. Public API (names may be adjusted to house style):

```python
@dataclass(frozen=True)
class EnergyConstants:
    model: str
    gpu: str
    e_prefill: float        # J per input token
    e_decode: float         # J per output token
    kappa: float = 1.0
    source: str             # citation for the measurement, required
    measured: bool          # False => interpolated/estimated

def energy_input(n_in: int, n_out: int, c: EnergyConstants) -> float: ...
def useful_work(n_out: int, c: EnergyConstants, q: float) -> float: ...
def efficiency(n_in: int, n_out: int, c: EnergyConstants, q: float | None) -> EfficiencyReport: ...

@dataclass(frozen=True)
class EfficiencyReport:
    e_in: float
    e_out: float
    q: float | None                # None => Q unknown, treated as 1.0
    quality_adjusted: bool
    eta: float
    waste_joules: float
    waste_share: float
    constants: EnergyConstants
```

Rules:

- `q=None` ⇒ treat as 1.0, set `quality_adjusted=False`.
- Raise `MissingEnergyConstantsError` (new exception) when the model has no constants and no interpolation is possible; the caller decides whether to fall back to `ecologits_bridge`.
- All floats in SI units (joules); convert to kWh only at the presentation layer.
- Never mutate shared state; this module is a pure library.

## 5. Data Layer

### 5.1 Energy constants table

New file `energy.json` at repo root, mirroring the shape of `pricing.json`:

```json
{
  "sources": {
    "luccioni2024": "https://arxiv.org/abs/2311.16863"
  },
  "models": {
    "gpt-4o": {
      "gpu": "H100",
      "e_prefill": 0.00003,
      "e_decode": 0.00035,
      "kappa": 1.0,
      "measured": false,
      "source": "interpolated from luccioni2024"
    }
  }
}
```

- Ship with at least 5 seeded entries drawn from published per-model J/token measurements; each entry **must** carry a `source` citation. Do not invent plausible-looking numbers without marking `measured: false`.
- Ship `token_dashboard/energy_table.py` for loading, lookup by model id (with alias/fuzzy matching, e.g. `gpt-4o-2024-08-06` → `gpt-4o`), and log a warning on fallback rather than failing silently.

### 5.2 Quality scores

- New optional JSON file `quality_scores.json` (gitignored, user-supplied) or DB table: per message/session id, values of \(\alpha\) and \(\rho\) in [0,1], plus `method` (e.g. `human`, `judge`, `exact_match`, `functional`).
- DB schema migration in `db.py`: add columns `e_in_j`, `e_out_j`, `q_alpha`, `q_rho`, `quality_adjusted`, `eta` to the message/session table (or a side table keyed by message id — follow whatever migration pattern `db.py` already uses). Must be backward compatible: existing databases migrate in place on first run.

### 5.3 New queries

Add DB query functions for: per-model aggregate \(\eta\), waste over time, top-N most wasteful sessions, and totals split by `quality_adjusted` flag. `perplexity_impact_results.json` at repo root is a reference for the output shape users already expect — match its schema for the new efficiency results and add an `efficiency` section.

## 6. Server and CLI

`server.py` — new endpoints, following existing route conventions:

- `GET /api/efficiency/overview` — totals, \(\eta\), waste joules, share quality-adjusted
- `GET /api/efficiency/by_model` — per-model breakdown with constants provenance
- `GET /api/efficiency/sessions?limit=N` — most wasteful sessions
- `GET /api/efficiency/by_day` — time series for charts

`cli.py` — new subcommand `thermo report [--model X] [--since DATE] [--json]` printing the same data as the overview endpoint.

## 7. Frontend

`web/routes/environmental.js`:

- New "Efficiency" panel above or beside the existing impact charts: gauge/number card for \(\eta\), stacked area for \(W_{useful}\) vs. waste over time (reuse ECharts patterns in `charts.js`), table of per-model efficiency with a `quality-adjusted` badge, and a footnote showing the energy-constant source per model.
- An explicit banner when results are **not** quality-adjusted: "Q=1 assumed (no quality scores configured)".
- No new frontend dependencies — the vendored `web/echarts.min.js` is the charting library; vanilla JS and the existing `style.css` conventions only.

## 8. Tests (pytest, follow existing `tests/` style)

Create, at minimum:

- `tests/test_thermodynamics.py` — formula math: prefill/decode separation, Q=None vs Q=0.3, waste arithmetic, \(\eta \le 1\) invariant, zero-token edge cases, `MissingEnergyConstantsError`.
- `tests/test_energy_table.py` — lookup, alias matching, `measured:false` provenance, missing-model fallback path.
- `tests/test_db_migration.py` — old DB file upgrades cleanly; round-trips efficiency columns.
- `tests/test_server.py` — extend with the four new endpoints (status, shape, empty-state).
- Update `tests/test_end_to_end_totals.py` so the end-to-end totals flow includes an efficiency assertion.

**Definition of done:** full suite green (`python -m pytest`), no regressions in existing tests, `cli.py thermo report` runs against a fixture DB, and `docs/KNOWN_LIMITATIONS.md` updated with a section on the new engine (notably: energy constants are model–GPU-pair estimates; Q requires user-supplied scoring; v1 defaults to Q=1).

## 9. Task Ordering (suggested checklist for the plan)

1. [ ] **P0** — `thermodynamics.py` pure functions + unit tests
2. [ ] **P0** — `energy.json` + `energy_table.py` with citations + tests
3. [ ] **P1** — DB schema migration + query functions + migration test
4. [ ] **P1** — Wire scanner capture of \(n_{in}\)/\(n_{out}\)/model if not already persisted (check `scanner.py` first; do not duplicate data)
5. [ ] **P1** — Server endpoints + `cli.py thermo report`
6. [ ] **P2** — Frontend panel in `web/routes/environmental.js` + charts
7. [ ] **P2** — Tips (`tips.py`) referencing efficiency; docs updates
8. [ ] **P2** — End-to-end test update; final full-suite run

## 10. Constraints and Deny Rules

- Do not modify `web/echarts.min.js` or introduce any new runtime dependency without asking.
- Do not delete or degrade `ecologits_bridge.py` behavior — it remains the fallback.
- Do not invent measured energy constants; unsourced values must be `measured: false` and flagged in the UI.
- Do not aggregate quality-adjusted and non-adjusted \(\eta\) values into one number; report them separately or flag the mix ratio.
- All energy math in joules (SI); conversions to kWh/Wh happen only in display code.
- Commit in small units; every P0/P1 task lands with its tests in the same commit.

## 11. Reference Sources (for the energy-constant table)

Primary published measurements to seed `energy.json` — per-model Joules/token, energy-accuracy trade-offs, and the quality-gating rationale:

- arXiv:2311.16863 — measured energy-per-token across model sizes
- arXiv:2504.03360 — energy-accuracy trade-off; Energy-per-Token as companion metric
- arXiv:2511.07698 — joint functional-accuracy/energy benchmark axis
- arXiv:2512.03024 — quantized LLM benchmarks: energy, accuracy, latency (CommonsenseQA, BBH, TruthfulQA, GSM8K, HumanEval)
- arXiv:2602.05695 — learned proportionality parameter mapping FLOPs to measured joules
- arXiv:2604.09048 — prefill vs. decode energy asymmetry (output-heavy ≈ 11× input-heavy, A100)
- arXiv:2603.20224 — quality-adjusted efficiency framing

> Add this line to `AGENTS.md` (create the file if absent): *"Before implementing any feature, read the relevant spec under `docs/specs/`. The thermodynamic efficiency engine spec is `docs/specs/thermodynamic-efficiency-spec.md`."*
