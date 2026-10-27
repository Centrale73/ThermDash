# thermaudit: 03 - Rassegna email mattutina con AI

Inference node `Riassumi con AI`, model `gpt-4o-mini`, tokenizer tiktoken o200k_base.
Runs/year 261 (cron '0 8 * * 1-5' enumerated over 2026); P(inference | run) = 0.949.
Tokens per inference: n_in mean 326 (p5 200, p95 497); n_out mean 74.
Q = 0.875 (quality_adjusted = True); D = 0.95.
Cost: $0.00009 per inference, $0.023 per year (workflow claims $0.01/run: 107x overstated).
Coverage K (service metric, not in eta): 71.3% uniform arrivals, 79.7% business hours, window redundancy 1.00; item-cap loss 0.00%.

| Scenario | Tier | E_in mean (J) | E_in p5-p95 (J) | eta_max | eta_model | eta_sys | Facility kWh/yr | CO2 g/yr |
|---|---|---|---|---|---|---|---|---|
| ThermDash energy.json (gpt-4o-mini) | C | 4.64e-03 | 3.07e-03-6.83e-03 | 0.719 | 0.629 | 0.598 | 3.99e-07 | 1.52e-04 |
| ThermDash energy_benchmarks.json (gpt-4o-mini) | C | 1.73 | 1.12-2.53 | 0.472 | 0.413 | 0.393 | 1.49e-04 | 0.0564 |
| ThermDash ecologits_bridge default rate | C | 1.74e+03 | 1.13e+03-2.54e+03 | 0.461 | 0.403 | 0.383 | 0.149 | 56.8 |
| Jegham et al. 2025 top-down fit (GPT-4o mini, DGX A100) | B | 537 | 382-730 | 0.819 | 0.717 | 0.681 | 0.0462 | 17.5 |
| Cavagna et al. 2026 theta-model (Llama 3.1 8B, H100, batched TensorRT-LLM) | A | 8.48 | 5.48-12.7 | 0.295 | 0.258 | 0.245 | 6.71e-04 | 1.14e-03 |

| Scenario | J per useful item | Host kWh/yr | LLM share of boundary | eta_boundary |
|---|---|---|---|---|
| ThermDash energy.json (gpt-4o-mini) | 1.77e-03 | 87.6 | 4.55e-07% | 2.72e-09 |
| ThermDash energy_benchmarks.json (gpt-4o-mini) | 0.66 | 87.6 | 1.70e-04% | 6.66e-07 |
| ThermDash ecologits_bridge default rate | 665 | 87.6 | 0.17% | 6.53e-04 |
| Jegham et al. 2025 top-down fit (GPT-4o mini, DGX A100) | 205 | 87.6 | 0.0527% | 3.59e-04 |
| Cavagna et al. 2026 theta-model (Llama 3.1 8B, H100, batched TensorRT-LLM) | 3.24 | 87.6 | 7.66e-04% | 1.88e-06 |

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
