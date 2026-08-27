import { api, state, keys, fmt, $ } from '/web/app.js';

export default async function (root) {
  const [cur, serverKeys] = await Promise.all([
    api('/api/plan'),
    api('/api/keys/status').catch(() => ({ perplexity: false, anthropic: false, openai: false })),
  ]);
  const plans = Object.entries(cur.pricing.plans);

  const curPplx = keys.get('perplexity');
  const curAnth = keys.get('anthropic');
  const curOpenAi = keys.get('openai');

  root.innerHTML = `
    <div class="card">
      <h2>Settings</h2>
      
      <!-- Plan Section -->
      <h3 style="margin-top:16px">Plan</h3>
      <p class="muted" style="margin:0 0 12px">Sets how cost is displayed. API mode shows pay-per-token rates. Subscription modes show what you actually pay each month.</p>
      <div class="flex">
        <select id="plan">
          ${plans.map(([k,v]) => `<option value="${k}" ${k===cur.plan?'selected':''}>${v.label}${v.monthly?` — $${v.monthly}/mo`:''}</option>`).join('')}
        </select>
        <button class="primary" id="save">Save Plan</button>
        <span id="msg" class="muted"></span>
      </div>

      <hr class="divider">

      <!-- API Keys Section -->
      <h3>API Keys for Chat & Inference</h3>
      <p class="muted" style="margin:0 0 16px">
        Provide your API keys to enable live inference in the <strong>Chat</strong> tab.
        Keys saved here stay entirely local in your browser (<code>localStorage</code>) and are sent only to the provider endpoints.
      </p>

      <div class="settings-keys-grid">
        <!-- Perplexity -->
        <div class="key-field-card">
          <div class="flex" style="justify-content:space-between;margin-bottom:6px">
            <strong>Perplexity AI API Key</strong>
            <span class="badge ${serverKeys.perplexity ? 'haiku' : ''}">${serverKeys.perplexity ? '✓ In Server Env' : 'Local Only'}</span>
          </div>
          <div class="flex">
            <input type="password" id="key-pplx" value="${fmt.htmlSafe(curPplx)}" placeholder="pplx-..." class="chat-input-full">
            <button id="toggle-pplx" class="chat-action-btn ghost" title="Show/Hide">👁</button>
            <button id="test-pplx" class="chat-action-btn">Test</button>
          </div>
          <div id="status-pplx" class="muted" style="font-size:11px;margin-top:4px"></div>
        </div>

        <!-- Anthropic -->
        <div class="key-field-card" style="margin-top:12px">
          <div class="flex" style="justify-content:space-between;margin-bottom:6px">
            <strong>Anthropic Claude API Key</strong>
            <span class="badge ${serverKeys.anthropic ? 'haiku' : ''}">${serverKeys.anthropic ? '✓ In Server Env' : 'Local Only'}</span>
          </div>
          <div class="flex">
            <input type="password" id="key-anth" value="${fmt.htmlSafe(curAnth)}" placeholder="sk-ant-..." class="chat-input-full">
            <button id="toggle-anth" class="chat-action-btn ghost" title="Show/Hide">👁</button>
            <button id="test-anth" class="chat-action-btn">Test</button>
          </div>
          <div id="status-anth" class="muted" style="font-size:11px;margin-top:4px"></div>
        </div>

        <!-- OpenAI -->
        <div class="key-field-card" style="margin-top:12px">
          <div class="flex" style="justify-content:space-between;margin-bottom:6px">
            <strong>OpenAI API Key</strong>
            <span class="badge ${serverKeys.openai ? 'haiku' : ''}">${serverKeys.openai ? '✓ In Server Env' : 'Local Only'}</span>
          </div>
          <div class="flex">
            <input type="password" id="key-openai" value="${fmt.htmlSafe(curOpenAi)}" placeholder="sk-..." class="chat-input-full">
            <button id="toggle-openai" class="chat-action-btn ghost" title="Show/Hide">👁</button>
            <button id="test-openai" class="chat-action-btn">Test</button>
          </div>
          <div id="status-openai" class="muted" style="font-size:11px;margin-top:4px"></div>
        </div>
      </div>

      <div class="flex" style="margin-top:16px;gap:8px">
        <button class="primary" id="save-keys">Save API Keys</button>
        <button class="ghost" id="clear-keys">Clear Stored Keys</button>
        <span id="keys-msg" class="muted"></span>
      </div>

      <hr class="divider">

      <!-- Pricing Table -->
      <h3>Pricing table</h3>
      <p class="muted" style="margin:0 0 12px">Edit <code>pricing.json</code> in the project root to change rates. Reload the page after editing.</p>
      <table>
        <thead><tr><th>model</th><th class="num">input</th><th class="num">output</th><th class="num">cache read</th><th class="num">cache 5m</th><th class="num">cache 1h</th></tr></thead>
        <tbody>
          ${Object.entries(cur.pricing.models).map(([k,v]) => `
            <tr><td><span class="badge ${v.tier}">${k}</span></td>
              <td class="num">$${v.input.toFixed(2)}</td>
              <td class="num">$${v.output.toFixed(2)}</td>
              <td class="num">$${v.cache_read.toFixed(2)}</td>
              <td class="num">$${v.cache_create_5m.toFixed(2)}</td>
              <td class="num">$${v.cache_create_1h.toFixed(2)}</td>
            </tr>`).join('')}
        </tbody>
      </table>
      <p class="muted" style="margin-top:8px;font-size:11px">Rates per 1M tokens, USD.</p>

      <hr class="divider">

      <h3>Privacy</h3>
      <p class="muted">Press <code>Cmd/Ctrl + B</code> anywhere to blur prompt text and other sensitive content for screenshots.</p>
    </div>`;

  // Save Plan
  $('#save').addEventListener('click', async () => {
    const plan = $('#plan').value;
    await fetch('/api/plan', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ plan }) });
    state.plan = plan;
    document.getElementById('plan-pill').textContent = plan;
    $('#msg').textContent = 'Saved.';
    $('#msg').style.color = 'var(--good)';
  });

  // Password toggles
  function setupToggle(btnId, inputId) {
    $(btnId).addEventListener('click', () => {
      const inp = $(inputId);
      inp.type = inp.type === 'password' ? 'text' : 'password';
    });
  }
  setupToggle('#toggle-pplx', '#key-pplx');
  setupToggle('#toggle-anth', '#key-anth');
  setupToggle('#toggle-openai', '#key-openai');

  // Key testing
  async function testKey(provider, inputId, statusId) {
    const val = $(inputId).value.trim();
    const statusEl = $(statusId);
    if (!val) {
      statusEl.textContent = 'Please enter a key to test.';
      statusEl.style.color = 'var(--bad)';
      return;
    }
    statusEl.textContent = 'Verifying key with provider...';
    statusEl.style.color = 'var(--muted)';
    try {
      const res = await fetch('/api/keys/test', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ provider, api_key: val }),
      });
      const data = await res.json();
      if (data.ok) {
        statusEl.textContent = '✓ Verified successfully!';
        statusEl.style.color = 'var(--good)';
      } else {
        statusEl.textContent = `✗ ${data.error || 'Verification failed'}`;
        statusEl.style.color = 'var(--bad)';
      }
    } catch (e) {
      statusEl.textContent = `✗ ${e.message}`;
      statusEl.style.color = 'var(--bad)';
    }
  }

  $('#test-pplx').addEventListener('click', () => testKey('perplexity', '#key-pplx', '#status-pplx'));
  $('#test-anth').addEventListener('click', () => testKey('anthropic', '#key-anth', '#status-anth'));
  $('#test-openai').addEventListener('click', () => testKey('openai', '#key-openai', '#status-openai'));

  // Save API Keys
  $('#save-keys').addEventListener('click', () => {
    keys.set('perplexity', $('#key-pplx').value);
    keys.set('anthropic', $('#key-anth').value);
    keys.set('openai', $('#key-openai').value);
    const msg = $('#keys-msg');
    msg.textContent = 'API Keys saved locally in browser.';
    msg.style.color = 'var(--good)';
    setTimeout(() => { msg.textContent = ''; }, 3000);
  });

  // Clear API Keys
  $('#clear-keys').addEventListener('click', () => {
    if (confirm('Clear all stored API keys from this browser?')) {
      keys.set('perplexity', '');
      keys.set('anthropic', '');
      keys.set('openai', '');
      $('#key-pplx').value = '';
      $('#key-anth').value = '';
      $('#key-openai').value = '';
      const msg = $('#keys-msg');
      msg.textContent = 'Keys cleared.';
      msg.style.color = 'var(--muted)';
    }
  });
}
