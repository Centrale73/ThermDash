# thermaudit: 03 - Rassegna email mattutina con AI

Inference node `Riassumi con AI`, model `gpt-4o-mini`, tokenizer tiktoken o200k_base.
Runs/year 261 (cron '0 8 * * 1-5' enumerated over 2026); P(inference | run) = 1.000.
Tokens per inference: n_in mean 1540 (p5 1352, p95 1632); n_out mean 240.
Q = 0.875 (quality_adjusted = True); D = 0.95.
Cost: $0.00037 per inference, $0.098 per year (workflow claims $0.01/run: 27x overstated).
Coverage K (service metric, not in eta): 71.3% uniform arrivals, 79.7% business hours, window redundancy 1.00; item-cap loss 18.19%.

| Scenario | Tier | E_in mean (J) | E_in p5-p95 (J) | eta_max | eta_model | eta_sys | Facility kWh/yr | CO2 g/yr |
|---|---|---|---|---|---|---|---|---|
| ThermDash energy.json (gpt-4o-mini) | C | 0.017 | 0.0135-0.0202 | 0.637 | 0.557 | 0.528 | 1.54e-06 | 5.84e-04 |
| ThermDash energy_benchmarks.json (gpt-4o-mini) | C | 6.95 | 5.92-7.83 | 0.380 | 0.332 | 0.315 | 6.30e-04 | 0.239 |
| ThermDash ecologits_bridge default rate | C | 7.02e+03 | 6e+03-7.89e+03 | 0.369 | 0.323 | 0.306 | 0.637 | 242 |
| Jegham et al. 2025 top-down fit (GPT-4o mini, DGX A100) | B | 1.64e+03 | 1.22e+03-2.05e+03 | 0.867 | 0.759 | 0.720 | 0.149 | 56.5 |
| Cavagna et al. 2026 theta-model (Llama 3.1 8B, H100, batched TensorRT-LLM) | A | 44.9 | 36.7-52 | 0.430 | 0.377 | 0.357 | 3.74e-03 | 6.36e-03 |

| Scenario | J per useful item | Host kWh/yr | LLM share of boundary | eta_boundary |
|---|---|---|---|---|
| ThermDash energy.json (gpt-4o-mini) | 8.32e-04 | 87.6 | 1.75e-06% | 9.27e-09 |
| ThermDash energy_benchmarks.json (gpt-4o-mini) | 0.341 | 87.6 | 7.19e-04% | 2.26e-06 |
| ThermDash ecologits_bridge default rate | 345 | 87.6 | 0.721% | 2.21e-03 |
| Jegham et al. 2025 top-down fit (GPT-4o mini, DGX A100) | 80.5 | 87.6 | 0.169% | 1.22e-03 |
| Cavagna et al. 2026 theta-model (Llama 3.1 8B, H100, batched TensorRT-LLM) | 2.2 | 87.6 | 4.27e-03% | 1.53e-05 |

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
