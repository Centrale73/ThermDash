# thermaudit: 03 - Rassegna email mattutina con AI

Inference node `Riassumi con AI`, model `gpt-4o-mini`, tokenizer tiktoken o200k_base.
Runs/year 261 (cron '0 8 * * 1-5' enumerated over 2026); P(inference | run) = 1.000.
Tokens per inference: n_in mean 974 (p5 538, p95 1592); n_out mean 162.
Q = 0.875 (quality_adjusted = True); D = 0.95.
Cost: $0.00024 per inference, $0.064 per year (workflow claims $0.01/run: 41x overstated).
Coverage K (service metric, not in eta): 71.3% uniform arrivals, 79.7% business hours, window redundancy 1.00; item-cap loss 13.25%.

| Scenario | Tier | E_in mean (J) | E_in p5-p95 (J) | eta_max | eta_model | eta_sys | Facility kWh/yr | CO2 g/yr |
|---|---|---|---|---|---|---|---|---|
| ThermDash energy.json (gpt-4o-mini) | C | 0.0112 | 6.27e-03-0.0185 | 0.652 | 0.571 | 0.543 | 1.02e-06 | 3.86e-04 |
| ThermDash energy_benchmarks.json (gpt-4o-mini) | C | 4.51 | 2.57-7.37 | 0.396 | 0.346 | 0.329 | 4.09e-04 | 0.155 |
| ThermDash ecologits_bridge default rate | C | 4.56e+03 | 2.6e+03-7.45e+03 | 0.385 | 0.337 | 0.320 | 0.413 | 157 |
| Jegham et al. 2025 top-down fit (GPT-4o mini, DGX A100) | B | 1.13e+03 | 625-1.84e+03 | 0.857 | 0.750 | 0.713 | 0.102 | 38.7 |
| Cavagna et al. 2026 theta-model (Llama 3.1 8B, H100, batched TensorRT-LLM) | A | 26.5 | 13.4-48.3 | 0.382 | 0.334 | 0.318 | 2.21e-03 | 3.75e-03 |

| Scenario | J per useful item | Host kWh/yr | LLM share of boundary | eta_boundary |
|---|---|---|---|---|
| ThermDash energy.json (gpt-4o-mini) | 9.26e-04 | 87.6 | 1.16e-06% | 6.29e-09 |
| ThermDash energy_benchmarks.json (gpt-4o-mini) | 0.373 | 87.6 | 4.67e-04% | 1.54e-06 |
| ThermDash ecologits_bridge default rate | 377 | 87.6 | 0.469% | 1.50e-03 |
| Jegham et al. 2025 top-down fit (GPT-4o mini, DGX A100) | 93 | 87.6 | 0.116% | 8.28e-04 |
| Cavagna et al. 2026 theta-model (Llama 3.1 8B, H100, batched TensorRT-LLM) | 2.19 | 87.6 | 2.52e-03% | 8.00e-06 |

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
