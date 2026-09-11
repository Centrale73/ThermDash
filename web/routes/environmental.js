import { api, fmt } from '/web/app.js';
import { barChart, donutChart, stackedBarChart } from '/web/charts.js';

const RANGES = [
  { key: '7d',  label: '7d',  days: 7 },
  { key: '30d', label: '30d', days: 30 },
  { key: '90d', label: '90d', days: 90 },
  { key: 'all', label: 'All', days: null },
];

function readRange() {
  const q = (location.hash.split('?')[1] || '');
  const m = /(?:^|&)range=([^&]+)/.exec(q);
  const k = m && decodeURIComponent(m[1]);
  return RANGES.find(r => r.key === k) || RANGES[1];
}

function writeRange(key) {
  const base = (location.hash.replace(/^#/, '').split('?')[0]) || '/environmental';
  location.hash = '#' + base + '?range=' + encodeURIComponent(key);
}

function sinceIso(range) {
  if (!range.days) return null;
  return new Date(Date.now() - range.days * 86400 * 1000).toISOString();
}

function withSince(url, since) {
  if (!since) return url;
  return url + (url.includes('?') ? '&' : '?') + 'since=' + encodeURIComponent(since);
}

// Formatting helpers for environmental units
function fmtEnergy(kwh) {
  const v = Number(kwh || 0);
  if (v <= 0) return '0 Wh';
  if (v < 0.01) return (v * 1000).toFixed(2) + ' Wh';
  if (v < 1.0)  return (v * 1000).toFixed(1) + ' Wh';
  return v.toFixed(3) + ' kWh';
}

function fmtCarbon(kg) {
  const v = Number(kg || 0);
  if (v <= 0) return '0 g';
  const g = v * 1000.0;
  if (g < 1.0)  return g.toFixed(2) + ' gCO₂eq';
  if (g < 1000) return g.toFixed(1) + ' gCO₂eq';
  return (g / 1000.0).toFixed(3) + ' kgCO₂eq';
}

function fmtWater(litres) {
  const v = Number(litres || 0);
  if (v <= 0) return '0 mL';
  const ml = v * 1000.0;
  if (ml < 1000) return ml.toFixed(1) + ' mL';
  return v.toFixed(2) + ' L';
}

export default async function (root) {
  const range = readRange();
  const since = sinceIso(range);

  const [impacts, daily, models, effOverview, effModels, effDaily] = await Promise.all([
    api(withSince('/api/impacts', since)),
    api(withSince('/api/impacts/daily', since)),
    api(withSince('/api/impacts/models', since)),
    api(withSince('/api/efficiency/overview', since)),
    api(withSince('/api/efficiency/by_model', since)),
    api(withSince('/api/efficiency/by_day', since)),
  ]);

  const totalEnergyKwh = Number(impacts.energy_kwh || 0);
  const totalEnergyWh = totalEnergyKwh * 1000.0;
  const totalGwpKg = Number(impacts.gwp_kgco2eq || 0);
  const totalGwpG = totalGwpKg * 1000.0;
  const totalWcfL = Number(impacts.wcf_l || 0);
  const totalPeMj = Number(impacts.pe_mj || 0);
  const totalAdpeMg = Number(impacts.adpe_kgsbeq || 0) * 1_000_000.0; // mgSbeq

  // Calculate total output tokens for Lean Six Sigma / Centrale 73 efficiency metrics
  const totalOutputTokens = models.reduce((acc, m) => acc + (m.output_tokens || 0), 0);
  const totalTokens = models.reduce((acc, m) => acc + (m.input_tokens || 0) + (m.output_tokens || 0) + (m.cache_create_tokens || 0), 0);

  const tokensPerWh = totalEnergyWh > 0 ? (totalOutputTokens / totalEnergyWh).toFixed(1) : '—';
  const joulesPerToken = totalOutputTokens > 0 ? ((totalEnergyWh * 3600.0) / totalOutputTokens).toFixed(2) : '—';
  const carbonPerKToken = totalTokens > 0 ? ((totalGwpG / (totalTokens / 1000.0))).toFixed(3) : '—';

  // Real-world comparisons (Centrale 73 framing)
  const googleSearches = (totalEnergyWh / 0.3).toFixed(1);
  const coffeeCups = (totalEnergyWh / 75.0).toFixed(2);
  const evMeters = (totalEnergyWh / 175.0 * 1000.0).toFixed(0);
  const phoneCharges = (totalEnergyWh / 12.0).toFixed(1);

  // Grid arbitrage: QC Hydro-Québec saves ~92.1% vs USA
  const qcEmissionsG = (totalEnergyKwh * 30.0 / 0.80).toFixed(1);
  const usaEmissionsG = totalGwpG.toFixed(1);
  const avoidedGwpG = Math.max(0, totalGwpG - Number(qcEmissionsG)).toFixed(1);

  // Thermodynamic Efficiency calculations
  const showQ1Banner = effOverview.total_rows_with_eta > 0 && effOverview.quality_adjusted_count === 0;
  const hasAdj = effOverview.avg_eta_adjusted !== null && effOverview.avg_eta_adjusted !== undefined;
  const hasUnadj = effOverview.avg_eta_unadjusted !== null && effOverview.avg_eta_unadjusted !== undefined;
  const primaryEta = hasAdj ? effOverview.avg_eta_adjusted : (hasUnadj ? effOverview.avg_eta_unadjusted : null);
  const primaryEtaLabel = hasAdj ? 'Thermodynamic Efficiency η (Quality-Adjusted)' : 'Thermodynamic Efficiency η (Unadjusted, Q=1)';
  const primaryEtaDisplay = primaryEta !== null ? (primaryEta * 100).toFixed(1) + '%' : '—';

  const sumEinJ = Number(effOverview.sum_e_in_j || 0);
  const sumWUsefulJ = Number(effOverview.sum_w_useful_j || 0);
  const sumWasteJ = Number(effOverview.sum_waste_j || 0);
  const wasteShare = sumEinJ > 0 ? ((sumWasteJ / sumEinJ) * 100).toFixed(1) + '%' : '0.0%';

  const rangeTabs = `
    <div class="range-tabs" role="tablist">
      ${RANGES.map(r => `<button data-range="${r.key}" class="${r.key === range.key ? 'active' : ''}">${r.label}</button>`).join('')}
    </div>`;

  root.innerHTML = `
    <div class="flex" style="margin-bottom:14px">
      <h2 style="margin:0;font-size:16px;letter-spacing:-0.01em">Thermodynamic Efficiency & Environmental Impact</h2>
      <span class="muted" style="font-size:12px">${range.days ? `last ${range.days} days` : 'all time'}</span>
      <div class="spacer"></div>
      ${rangeTabs}
    </div>

    ${showQ1Banner ? `
      <div class="card" style="background:var(--panel-2);border-left:4px solid var(--warn);padding:10px 14px;margin-bottom:14px;display:flex;align-items:center;justify-content:space-between">
        <div>
          <strong>⚠ Q = 1 assumed</strong> — no quality scores configured. Supply <code>quality_scores.json</code> or use <code>thermo score</code> to audit true quality-gated useful work.
        </div>
        <span class="muted" style="font-size:12px">Simplified v1 Mode</span>
      </div>
    ` : ''}

    <!-- Thermodynamic Efficiency Top KPI Row -->
    <div class="row cols-4">
      <div class="card kpi">
        <div class="label">${primaryEtaLabel}</div>
        <div class="value big" style="color:var(--good)">
          ${primaryEtaDisplay}
          ${effOverview.quality_adjusted_count > 0 && effOverview.unadjusted_count > 0 ? `
            <span class="badge haiku" style="font-size:11px;vertical-align:middle;margin-left:4px">${(effOverview.mix_ratio * 100).toFixed(0)}% Q-adj</span>
          ` : ''}
        </div>
        <div class="sub">${effOverview.quality_adjusted_count} adjusted / ${effOverview.unadjusted_count} unadjusted turns</div>
      </div>
      <div class="card kpi">
        <div class="label">Total Energy In (E_in)</div>
        <div class="value" style="color:var(--accent)" title="${sumEinJ.toFixed(6)} Joules">${sumEinJ.toFixed(3)} J</div>
        <div class="sub">${(sumEinJ / 3600.0).toFixed(4)} Wh prefill + decode</div>
      </div>
      <div class="card kpi">
        <div class="label">Useful Work (W_useful)</div>
        <div class="value" style="color:#3FB68B" title="${sumWUsefulJ.toFixed(6)} Joules">${sumWUsefulJ.toFixed(3)} J</div>
        <div class="sub">Q × E_out decode yield (${(sumWUsefulJ / 3600.0).toFixed(4)} Wh)</div>
      </div>
      <div class="card kpi">
        <div class="label">Thermodynamic Waste</div>
        <div class="value" style="color:#E5484D" title="${sumWasteJ.toFixed(6)} Joules">${sumWasteJ.toFixed(3)} J</div>
        <div class="sub">${wasteShare} waste share (${(sumWasteJ / 3600.0).toFixed(4)} Wh)</div>
      </div>
    </div>

    <!-- Efficiency Charts Row -->
    <div class="row cols-2" style="margin-top:16px">
      <div class="card">
        <h3>Useful Work vs. Waste Over Time</h3>
        <p class="muted" style="margin:-4px 0 10px;font-size:12px">Daily useful computational yield (W_useful) vs. unutilized energy waste in Joules.</p>
        <div id="ch-daily-waste" style="height:270px"></div>
      </div>
      <div class="card">
        <h3>Thermodynamic Efficiency by Model</h3>
        <p class="muted" style="margin:-4px 0 10px;font-size:12px">Average η percentage achieved across active models in this time window.</p>
        <div id="ch-model-eta" style="height:270px"></div>
      </div>
    </div>

    <!-- Per-Model Efficiency Table -->
    <div class="card" style="margin-top:16px">
      <h3>Thermodynamic Efficiency by Model</h3>
      <table>
        <thead>
          <tr>
            <th>Model</th>
            <th class="num">Turns</th>
            <th class="num">η (Quality-Adj)</th>
            <th class="num">η (Unadjusted)</th>
            <th class="num">E_in (J)</th>
            <th class="num">W_useful (J)</th>
            <th class="num">Waste (Wh)</th>
            <th class="num">Hardware / Constants</th>
          </tr>
        </thead>
        <tbody>
          ${effModels.map(m => {
            const wasteWh = (m.sum_waste_j || 0) / 3600.0;
            const adjDisplay = m.avg_eta_adjusted !== null ? (m.avg_eta_adjusted * 100).toFixed(1) + '%' : '—';
            const unadjDisplay = m.avg_eta_unadjusted !== null ? (m.avg_eta_unadjusted * 100).toFixed(1) + '%' : '—';
            const measuredBadge = m.energy_measured ? '<span class="badge" style="color:var(--good);font-size:10px">measured</span>' : '<span class="badge" style="font-size:10px">estimate</span>';
            return `
              <tr>
                <td><span class="badge ${fmt.modelClass(m.model)}">${fmt.htmlSafe(m.model)}</span></td>
                <td class="num">${fmt.int(m.turns_with_eta)}</td>
                <td class="num" style="color:#3FB68B;font-weight:600">${adjDisplay}</td>
                <td class="num">${unadjDisplay}</td>
                <td class="num">${(m.sum_e_in_j || 0).toFixed(3)}</td>
                <td class="num">${(m.sum_w_useful_j || 0).toFixed(3)}</td>
                <td class="num">${wasteWh.toFixed(4)}</td>
                <td class="num" style="font-size:12px">${fmt.htmlSafe(m.gpu || 'GPU')} · ${fmt.htmlSafe(m.energy_source || 'luccioni2024')} ${measuredBadge}</td>
              </tr>
            `;
          }).join('') || '<tr><td colspan="8" class="muted">No efficiency records in this range</td></tr>'}
        </tbody>
      </table>
      <div class="muted" style="font-size:11px;margin-top:8px">
        * Note: Energy constants are estimates based on published literature (marked estimate). Model inference energy spans 0.003–1.0 J/token across hardware architectures.
      </div>
    </div>

    <!-- EcoLogits Environmental Impact Section -->
    <div style="margin-top:24px;margin-bottom:12px;display:flex;align-items:center">
      <h3 style="margin:0;font-size:15px">Lifecycle Environmental Impact (EcoLogits LCA)</h3>
    </div>

    <div class="row cols-4">
      <div class="card kpi">
        <div class="label">Total Electricity</div>
        <div class="value" style="color:var(--accent)" title="${totalEnergyKwh.toFixed(6)} kWh">${fmtEnergy(totalEnergyKwh)}</div>
        <div class="sub">${(totalEnergyWh * 3600).toLocaleString(undefined, {maximumFractionDigits:0})} Joules energy</div>
      </div>
      <div class="card kpi">
        <div class="label">Carbon Footprint (GWP)</div>
        <div class="value" style="color:#F472B6" title="${totalGwpKg.toFixed(6)} kgCO₂eq">${fmtCarbon(totalGwpKg)}</div>
        <div class="sub">Scope 2 usage + Scope 3 embodied</div>
      </div>
      <div class="card kpi">
        <div class="label">Water Consumption (WCF)</div>
        <div class="value" style="color:#5BCEDA" title="${totalWcfL.toFixed(6)} Litres">${fmtWater(totalWcfL)}</div>
        <div class="sub">Datacenter cooling + generation</div>
      </div>
      <div class="card kpi">
        <div class="label">Primary Energy (PE)</div>
        <div class="value" style="color:var(--warn)" title="${totalPeMj.toFixed(4)} MJ">${totalPeMj.toFixed(2)} MJ</div>
        <div class="sub">ADPe: ${totalAdpeMg < 0.01 ? totalAdpeMg.toExponential(2) : totalAdpeMg.toFixed(3)} mg Sbeq</div>
      </div>
    </div>

    <!-- Lean Six Sigma & Audit Efficiency Bar -->
    <div class="row cols-3" style="margin-top:16px">
      <div class="card kpi">
        <div class="label">Inference Yield</div>
        <div class="value big" style="color:var(--good)">${tokensPerWh} <span style="font-size:14px;font-weight:400;color:var(--muted)">tok/Wh</span></div>
        <div class="sub">Useful output work per unit electricity</div>
      </div>
      <div class="card kpi">
        <div class="label">Energy Intensity</div>
        <div class="value big" style="color:var(--text)">${joulesPerToken} <span style="font-size:14px;font-weight:400;color:var(--muted)">J/token</span></div>
        <div class="sub">Joules consumed per output token</div>
      </div>
      <div class="card kpi">
        <div class="label">Calculation Engine & Grid</div>
        <div class="value" style="font-size:16px;margin-top:8px">
          <span class="badge ${impacts.ecologits_available ? 'haiku' : 'sonnet'}">
            ${impacts.ecologits_available ? 'EcoLogits LCA' : 'Standard Formula'}
          </span>
          <span class="badge" style="margin-left:6px">${impacts.grid_zone || 'USA'} (${impacts.grid_intensity || 380} g/kWh)</span>
        </div>
        <div class="sub">${impacts.fallback_count || 0} fallback, ${impacts.ecologits_count || 0} ecologits records</div>
      </div>
    </div>

    <!-- Charts Row -->
    <div class="row cols-2" style="margin-top:16px">
      <div class="card">
        <h3>Daily Electrical Energy</h3>
        <p class="muted" style="margin:-4px 0 10px;font-size:12px">Daily electrical energy consumed by Claude Code turns in Watt-hours (Wh).</p>
        <div id="ch-daily-energy" style="height:270px"></div>
      </div>
      <div class="card">
        <h3>Carbon Footprint by Model</h3>
        <p class="muted" style="margin:-4px 0 10px;font-size:12px">Distribution of greenhouse gas emissions (gCO₂eq) across Claude models.</p>
        <div id="ch-model-carbon" style="height:270px"></div>
      </div>
    </div>

    <!-- Equivalencies & Clean Grid Comparison -->
    <div class="row cols-2" style="margin-top:16px">
      <div class="card">
        <h3>Physical World Equivalents</h3>
        <p class="muted" style="margin:-4px 0 12px;font-size:12px">Real-world energy benchmarks for your ${fmtEnergy(totalEnergyKwh)} total usage:</p>
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px">
          <div style="background:var(--panel-2);padding:12px;border-radius:8px;border:1px solid var(--border)">
            <div style="font-size:20px;font-weight:600;color:var(--accent)">${googleSearches}</div>
            <div class="muted" style="font-size:12px">Google web searches (≈0.3 Wh)</div>
          </div>
          <div style="background:var(--panel-2);padding:12px;border-radius:8px;border:1px solid var(--border)">
            <div style="font-size:20px;font-weight:600;color:var(--good)">${phoneCharges}</div>
            <div class="muted" style="font-size:12px">Smartphone charges (≈12 Wh)</div>
          </div>
          <div style="background:var(--panel-2);padding:12px;border-radius:8px;border:1px solid var(--border)">
            <div style="font-size:20px;font-weight:600;color:var(--warn)">${coffeeCups}</div>
            <div class="muted" style="font-size:12px">Cups of brewed coffee (≈75 Wh)</div>
          </div>
          <div style="background:var(--panel-2);padding:12px;border-radius:8px;border:1px solid var(--border)">
            <div style="font-size:20px;font-weight:600;color:#F472B6">${evMeters} m</div>
            <div class="muted" style="font-size:12px">Electric vehicle driving (≈175 Wh/km)</div>
          </div>
        </div>
      </div>

      <div class="card">
        <h3>Grid Carbon Arbitrage (Quebec Hydro vs USA)</h3>
        <p class="muted" style="margin:-4px 0 12px;font-size:12px">Comparison between standard US grid mix and Quebec Hydro-Québec renewable power:</p>
        <div style="background:var(--panel-2);padding:14px;border-radius:8px;border:1px solid var(--border)">
          <div style="display:flex;justify-content:space-between;margin-bottom:8px">
            <span>USA Standard Grid (380 g/kWh)</span>
            <span class="mono">${usaEmissionsG} gCO₂eq</span>
          </div>
          <div style="display:flex;justify-content:space-between;margin-bottom:8px;color:var(--good)">
            <span>Hydro-Québec Renewable (30 g/kWh)</span>
            <span class="mono">${qcEmissionsG} gCO₂eq</span>
          </div>
          <hr class="divider" style="margin:8px 0">
          <div style="display:flex;justify-content:space-between;font-weight:600;color:var(--good)">
            <span>Potential Emission Reduction</span>
            <span>-92.1% (~${avoidedGwpG} g avoided)</span>
          </div>
        </div>
      </div>
    </div>

    <!-- Methodology & LCA Audit Accordion -->
    <details class="card glossary" style="margin-top:16px">
      <summary><h3 style="display:inline-block;margin:0">Audit Methodology & LCA Impact Criteria</h3><span class="muted" style="font-size:12px">— click to expand</span></summary>
      <dl>
        <dt>Thermodynamic Efficiency η</dt><dd>Ratio of useful computational work to total electrical energy input: η = W_useful / E_in. Prefill and decode have distinct physical energy profiles.</dd>
        <dt>Useful Work W_useful</dt><dd>W_useful = Q × E_out. Decode energy gated by quality factor Q(alpha, rho), penalizing hallucinations, padding, and low relevance.</dd>
        <dt>Thermodynamic Waste</dt><dd>Energy consumed minus useful work yield (Waste = E_in - W_useful). Measures prefill overhead and unutilized inference energy.</dd>
        <dt>Energy (kWh)</dt><dd>Direct electricity consumption of datacenter GPU inference, server components, and Power Usage Effectiveness (PUE) overhead.</dd>
        <dt>GWP (kgCO₂eq)</dt><dd>Global Warming Potential based on Life Cycle Assessment (LCA). Includes operational electricity emissions (80%) + embodied hardware manufacturing emissions (20%).</dd>
        <dt>Water (WCF)</dt><dd>Water Consumption Footprint (litres) from evaporative datacenter cooling towers and thermoelectric power plant cooling.</dd>
        <dt>ADPe (kgSbeq)</dt><dd>Abiotic Depletion Potential of mineral and metal elements (copper, lithium, gold, silicon) used in server hardware manufacturing.</dd>
        <dt>Primary Energy (MJ)</dt><dd>Total primary energy extracted from nature before power conversion and transmission losses (approx. 3:1 primary-to-delivered energy ratio).</dd>
      </dl>
    </details>
  `;

  // Range button listeners
  root.querySelectorAll('.range-tabs button').forEach(btn => {
    btn.addEventListener('click', () => writeRange(btn.dataset.range));
  });

  // Chart: Daily Useful Work vs Waste
  const wasteEl = document.getElementById('ch-daily-waste');
  if (wasteEl && effDaily.length > 0) {
    stackedBarChart(wasteEl, {
      categories: effDaily.map(d => d.day),
      series: [
        { name: 'Useful Work (J)', values: effDaily.map(d => d.sum_w_useful_j || 0), color: '#3FB68B' },
        { name: 'Waste (J)', values: effDaily.map(d => d.sum_waste_j || 0), color: '#E5484D' },
      ],
      formatter: v => Number(v).toFixed(4) + ' J',
    });
  }

  // Chart: Efficiency by Model (bar chart)
  const etaEl = document.getElementById('ch-model-eta');
  if (etaEl && effModels.length > 0) {
    barChart(etaEl, {
      categories: effModels.map(m => fmt.modelShort(m.model) || m.model),
      values: effModels.map(m => {
        const v = m.avg_eta_adjusted !== null ? m.avg_eta_adjusted : (m.avg_eta_unadjusted !== null ? m.avg_eta_unadjusted : 0);
        return Math.round(v * 1000) / 10;
      }),
      color: '#4A9EFF',
    });
  }

  // Chart: Daily Energy
  const dailyEl = document.getElementById('ch-daily-energy');
  if (dailyEl && daily.length > 0) {
    stackedBarChart(dailyEl, {
      categories: daily.map(d => d.day),
      series: [
        { name: 'Energy (Wh)', values: daily.map(d => (d.energy_kwh || 0) * 1000.0), color: '#4A9EFF' },
      ],
      formatter: v => Number(v).toFixed(2) + ' Wh',
    });
  }

  // Chart: Model Carbon Share Donut
  const carbonEl = document.getElementById('ch-model-carbon');
  if (carbonEl && models.length > 0) {
    donutChart(
      carbonEl,
      models.map(m => ({
        name: fmt.modelShort(m.model) || 'unknown',
        value: Math.round((m.gwp_kgco2eq || 0) * 100000) / 100,
      })).filter(d => d.value > 0),
    );
  }
}
