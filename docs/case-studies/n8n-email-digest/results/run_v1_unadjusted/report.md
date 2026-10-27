# thermaudit: 03 - Rassegna email mattutina con AI

Inference node `Riassumi con AI`, model `gpt-4o-mini`, tokenizer tiktoken o200k_base.
Runs/year 261 (cron '0 8 * * 1-5' enumerated over 2026); P(inference | run) = 1.000.
Tokens per inference: n_in mean 831 (p5 520, p95 1176); n_out mean 143.
Q = 1.000 (quality_adjusted = False); D = 1.0.
Cost: $0.00021 per inference, $0.055 per year (workflow claims $0.01/run: 48x overstated).
Coverage K (service metric, not in eta): 71.3% uniform arrivals, 79.7% business hours, window redundancy 1.00; item-cap loss 0.00%.

| Scenario | Tier | E_in mean (J) | E_in p5-p95 (J) | eta_max | eta_model | eta_sys | Facility kWh/yr | CO2 g/yr |
|---|---|---|---|---|---|---|---|---|
| ThermDash energy.json (gpt-4o-mini) | C | 9.76e-03 | 6.08e-03-0.0139 | 0.659 | 0.659 | 0.659 | 8.84e-07 | 3.36e-04 |
| ThermDash energy_benchmarks.json (gpt-4o-mini) | C | 3.9 | 2.48-5.48 | 0.403 | 0.403 | 0.403 | 3.53e-04 | 0.134 |
| ThermDash ecologits_bridge default rate | C | 3.94e+03 | 2.5e+03-5.53e+03 | 0.392 | 0.392 | 0.392 | 0.357 | 136 |
| Jegham et al. 2025 top-down fit (GPT-4o mini, DGX A100) | B | 996 | 619-1.41e+03 | 0.852 | 0.852 | 0.852 | 0.0902 | 34.3 |
| Cavagna et al. 2026 theta-model (Llama 3.1 8B, H100, batched TensorRT-LLM) | A | 21.8 | 12.9-32.6 | 0.356 | 0.356 | 0.356 | 1.82e-03 | 3.09e-03 |

| Scenario | J per useful item | Host kWh/yr | LLM share of boundary | eta_boundary |
|---|---|---|---|---|
| ThermDash energy.json (gpt-4o-mini) | 8.11e-04 | 87.6 | 1.01e-06% | 6.66e-09 |
| ThermDash energy_benchmarks.json (gpt-4o-mini) | 0.324 | 87.6 | 4.03e-04% | 1.63e-06 |
| ThermDash ecologits_bridge default rate | 327 | 87.6 | 0.406% | 1.59e-03 |
| Jegham et al. 2025 top-down fit (GPT-4o mini, DGX A100) | 82.8 | 87.6 | 0.103% | 8.77e-04 |
| Cavagna et al. 2026 theta-model (Llama 3.1 8B, H100, batched TensorRT-LLM) | 1.81 | 87.6 | 2.07e-03% | 7.39e-06 |

## Flags
- [red] No error trigger / errorWorkflow: failures after inference are silent and their energy is unaccounted. [GDPR Arts. 5(2) and 32: accountability and processing integrity (no error path)]
- [red] Personal data (sender names, message text) reaches cloud model 'Riassumi con AI' (openai). [GDPR Chapter V (Arts. 44-49): transfer of personal data to third countries needs a transfer mechanism]
- [red] Unconfigured placeholder in 'Invia rassegna su Telegram' (INSERISCI): delivery gate D = 0 until set.
- [yellow] 'Invia rassegna su Telegram' uses legacy Telegram Markdown on LLM text: an unescaped '_', '*', '`' or '[' rejects the whole message (D < 1).
- [yellow] Look-back 'is:unread newer_than:1d -category:promotions -category:social' with cron '0 8 * * 1-5' leaves 2512 h/yr unread by the workflow (K = 71.3% uniform, 79.7% business hours). Fix with an 'after:' last-run timestamp, not a wider newer_than.
- [yellow] Single AI vendor, no fallback model: one outage = zero useful work.

```mermaid
graph LR
  classDef cloud fill:#fdecea,stroke:#c62828,color:#5d0f0f
  classDef ai fill:#fff4e5,stroke:#e65100,stroke-width:3px,color:#4a2600
  classDef local fill:#e8f5e9,stroke:#2e7d32,color:#0b3d12
  N0["Ogni mattina alle 8 (lun-ven)"]:::local
  N1["Prendi email non lette"]:::cloud
  N2["Prepara testo per AI"]:::local
  N3["Ci sono email?"]:::local
  N4["Riassumi con AI [E_in]"]:::ai
  N5["Invia rassegna su Telegram"]:::cloud
  N0 -->|main| N1
  N1 -->|main| N2
  N2 -->|main| N3
  N3 -->|main| N4
  N4 -->|main| N5
```
