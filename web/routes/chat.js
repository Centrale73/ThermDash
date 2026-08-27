import { api, fmt, keys, $, $$ } from '/web/app.js';

const MODELS = [
  { group: 'Perplexity AI', models: [
    { id: 'sonar', name: 'Sonar (8B Online)', provider: 'perplexity', desc: 'Fast, online search grounding, ~8B parameters' },
    { id: 'sonar-pro', name: 'Sonar Pro (70B Online)', provider: 'perplexity', desc: 'High capability online reasoning, ~70B parameters' },
    { id: 'sonar-reasoning', name: 'Sonar Reasoning (8B Thinking)', provider: 'perplexity', desc: 'Step-by-step reasoning with live search' },
    { id: 'sonar-reasoning-pro', name: 'Sonar Reasoning Pro (70B)', provider: 'perplexity', desc: 'Advanced reasoning, deep chain-of-thought' },
    { id: 'llama-3.1-sonar-small-128k-online', name: 'Llama 3.1 Sonar Small', provider: 'perplexity', desc: '128k context, online search' },
    { id: 'llama-3.1-sonar-large-128k-online', name: 'Llama 3.1 Sonar Large', provider: 'perplexity', desc: '70B 128k context, online search' },
    { id: 'llama-3.1-sonar-huge-128k-online', name: 'Llama 3.1 Sonar Huge (405B)', provider: 'perplexity', desc: '405B flagship model' },
  ]},
  { group: 'Anthropic Claude', models: [
    { id: 'claude-3-5-sonnet-latest', name: 'Claude 3.5 Sonnet', provider: 'anthropic', desc: 'Industry-leading coding & reasoning' },
    { id: 'claude-3-opus-latest', name: 'Claude 3 Opus', provider: 'anthropic', desc: 'Deep synthesis & complex analysis' },
    { id: 'claude-3-5-haiku-latest', name: 'Claude 3.5 Haiku', provider: 'anthropic', desc: 'Ultra-fast, cost-efficient intelligence' },
  ]},
  { group: 'OpenAI', models: [
    { id: 'gpt-4o', name: 'GPT-4o (Omni)', provider: 'openai', desc: 'High intelligence flagship model' },
    { id: 'gpt-4o-mini', name: 'GPT-4o mini', provider: 'openai', desc: 'Fast, affordable small model' },
    { id: 'o3-mini', name: 'o3-mini (Reasoning)', provider: 'openai', desc: 'STEM reasoning and deep analysis' },
  ]},
];

const SUGGESTIONS = [
  'Calculate the environmental impact of training vs inference in LLMs',
  'Explain how clean hydroelectric power reduces AI data center carbon footprint',
  'Write a Python script to monitor API tokens and calculate energy in Wh',
  'Compare Perplexity Sonar search grounding vs standard LLM retrieval',
];

// Lightweight, safe Markdown renderer
function renderMarkdown(text) {
  if (!text) return '';
  let s = fmt.htmlSafe(text);

  // Code blocks: ```lang ... ```
  s = s.replace(/```(\w*)\n([\s\S]*?)```/g, (match, lang, code) => {
    const language = lang || 'code';
    return `
      <div class="chat-code-block">
        <div class="chat-code-header">
          <span class="chat-code-lang">${language}</span>
          <button class="chat-copy-btn" onclick="navigator.clipboard.writeText(decodeURIComponent('${encodeURIComponent(code.trim())}')).then(()=>{this.textContent='Copied!';setTimeout(()=>this.textContent='Copy',2000);})">Copy</button>
        </div>
        <pre class="chat-code-content"><code>${code.trim()}</code></pre>
      </div>`;
  });

  // Inline code: `code`
  s = s.replace(/`([^`]+)`/g, '<code class="chat-inline-code">$1</code>');

  // Headings
  s = s.replace(/^### (.*$)/gim, '<h4 class="chat-h">$1</h4>');
  s = s.replace(/^## (.*$)/gim, '<h3 class="chat-h">$1</h3>');
  s = s.replace(/^# (.*$)/gim, '<h2 class="chat-h">$1</h2>');

  // Bold & Italic
  s = s.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
  s = s.replace(/\*([^*]+)\*/g, '<em>$1</em>');

  // Blockquotes
  s = s.replace(/^> (.*$)/gim, '<blockquote class="chat-quote">$1</blockquote>');

  // Unordered lists
  s = s.replace(/^\s*[-*•]\s+(.*$)/gim, '<li class="chat-li">$1</li>');
  s = s.replace(/(<li class="chat-li">.*<\/li>)/gms, '<ul class="chat-ul">$1</ul>');

  // Paragraphs
  s = s.split('\n\n').map(p => {
    if (p.startsWith('<div class="chat-code-block"') || p.startsWith('<h') || p.startsWith('<ul') || p.startsWith('<blockquote')) {
      return p;
    }
    return `<p class="chat-p">${p.replace(/\n/g, '<br>')}</p>`;
  }).join('');

  return s;
}

// Session state
let chatSession = {
  id: 'chat-' + Math.random().toString(36).slice(2, 10),
  model: localStorage.getItem('td.chat.model') || 'sonar',
  temperature: 0.7,
  maxTokens: 1000,
  zone: 'USA',
  logToDb: true,
  systemPrompt: '',
  messages: [], // { role, content, usage, latency_s, cost_usd, impacts, timestamp, citations, simulated }
  totals: {
    inputTokens: 0,
    outputTokens: 0,
    energyWh: 0.0,
    gwpG: 0.0,
    wcfMl: 0.0,
    costUsd: 0.0,
  }
};

export default async function (root) {
  const serverKeys = await api('/api/keys/status').catch(() => ({ perplexity: false, anthropic: false, openai: false }));

  function getActiveModelMeta() {
    for (const g of MODELS) {
      for (const m of g.models) {
        if (m.id === chatSession.model) return m;
      }
    }
    return MODELS[0].models[0];
  }

  function hasKeyForModel(modelMeta) {
    const p = modelMeta.provider;
    return Boolean(keys.get(p) || serverKeys[p]);
  }

  function renderTopbar() {
    const meta = getActiveModelMeta();
    const hasKey = hasKeyForModel(meta);
    const keyStatusText = hasKey
      ? (keys.get(meta.provider) ? 'Key Configured (Browser)' : 'Key Configured (Env)')
      : 'No API Key (Dry-Run Mode)';

    return `
      <div class="chat-control-bar">
        <div class="chat-model-select-wrap">
          <label for="chat-model-select">Model</label>
          <select id="chat-model-select" class="chat-select">
            ${MODELS.map(g => `
              <optgroup label="${g.group}">
                ${g.models.map(m => `<option value="${m.id}" ${m.id === chatSession.model ? 'selected' : ''}>${m.name}</option>`).join('')}
              </optgroup>
            `).join('')}
          </select>
        </div>

        <button id="btn-open-keys" class="chat-key-badge ${hasKey ? 'active' : 'warn'}" title="Configure API Keys">
          <span class="chat-key-dot"></span>
          <span>${meta.provider.toUpperCase()} Key: ${hasKey ? 'Active' : 'Missing'}</span>
          <span class="chat-key-action">⚙️</span>
        </button>

        <button id="btn-toggle-drawer" class="chat-icon-btn" title="Model & EcoLogits Settings">
          <span>⚙️ Parameters</span>
        </button>

        <div class="spacer"></div>

        <button id="btn-new-chat" class="chat-action-btn" title="Start a fresh chat session">
          <span>＋ New Chat</span>
        </button>

        <button id="btn-export-chat" class="chat-action-btn ghost" title="Export conversation as Markdown">
          <span>↓ Export</span>
        </button>
      </div>

      <!-- Collapsible Settings Drawer -->
      <div id="chat-drawer" class="chat-drawer card" style="display:none">
        <div class="chat-drawer-grid">
          <div>
            <label>System Prompt</label>
            <textarea id="drawer-system-prompt" class="chat-textarea-small" placeholder="You are a helpful AI assistant with deep expertise in energy efficiency and sustainability...">${fmt.htmlSafe(chatSession.systemPrompt)}</textarea>
          </div>
          <div class="chat-drawer-params">
            <div>
              <div class="flex" style="justify-content:space-between">
                <label>Temperature: <span id="val-temp">${chatSession.temperature}</span></label>
              </div>
              <input type="range" id="drawer-temp" min="0" max="1" step="0.05" value="${chatSession.temperature}" style="width:100%">
            </div>
            <div>
              <label>Max Tokens</label>
              <input type="number" id="drawer-max-tokens" value="${chatSession.maxTokens}" min="50" max="4000" step="50" class="chat-input-small">
            </div>
            <div>
              <label>EcoLogits Grid Zone</label>
              <select id="drawer-zone" class="chat-select">
                <option value="USA" ${chatSession.zone==='USA'?'selected':''}>USA (380 gCO₂/kWh)</option>
                <option value="QC" ${chatSession.zone==='QC'?'selected':''}>Quebec Hydro-Québec (30 gCO₂/kWh)</option>
                <option value="CAN" ${chatSession.zone==='CAN'?'selected':''}>Canada Mix (120 gCO₂/kWh)</option>
                <option value="FRA" ${chatSession.zone==='FRA'?'selected':''}>France Nuclear Mix (56 gCO₂/kWh)</option>
                <option value="WOR" ${chatSession.zone==='WOR'?'selected':''}>World Average (475 gCO₂/kWh)</option>
              </select>
            </div>
            <div style="margin-top:14px">
              <label class="flex" style="cursor:pointer;font-weight:normal">
                <input type="checkbox" id="drawer-log-db" ${chatSession.logToDb ? 'checked' : ''} style="margin-right:6px">
                <span>Log to Dashboard SQLite (live analytics)</span>
              </label>
            </div>
          </div>
        </div>
      </div>

      <!-- Cumulative Session Impact Bar -->
      <div class="chat-session-impact-bar">
        <div class="chat-impact-stat">
          <span class="chat-impact-icon">⚡</span>
          <span class="chat-impact-val">${chatSession.totals.energyWh.toFixed(3)} Wh</span>
          <span class="chat-impact-lbl">Energy</span>
        </div>
        <div class="chat-impact-stat">
          <span class="chat-impact-icon">🌿</span>
          <span class="chat-impact-val">${chatSession.totals.gwpG.toFixed(3)} gCO₂eq</span>
          <span class="chat-impact-lbl">Carbon (LCA)</span>
        </div>
        <div class="chat-impact-stat">
          <span class="chat-impact-icon">💧</span>
          <span class="chat-impact-val">${chatSession.totals.wcfMl.toFixed(2)} mL</span>
          <span class="chat-impact-lbl">Water</span>
        </div>
        <div class="chat-impact-stat">
          <span class="chat-impact-icon">🪙</span>
          <span class="chat-impact-val">${fmt.int(chatSession.totals.inputTokens + chatSession.totals.outputTokens)}</span>
          <span class="chat-impact-lbl">Total Tokens</span>
        </div>
        <div class="chat-impact-stat">
          <span class="chat-impact-icon">💰</span>
          <span class="chat-impact-val">$${chatSession.totals.costUsd.toFixed(4)}</span>
          <span class="chat-impact-lbl">Est. Cost</span>
        </div>
      </div>
    `;
  }

  function renderMessage(msg) {
    const isUser = msg.role === 'user';
    if (isUser) {
      return `
        <div class="chat-msg user">
          <div class="chat-bubble user">
            <div class="chat-body">${fmt.htmlSafe(msg.content).replace(/\n/g, '<br>')}</div>
            <div class="chat-meta">${msg.timestamp ? msg.timestamp.slice(11, 19) : ''}</div>
          </div>
        </div>
      `;
    }

    const imp = msg.impacts || {};
    const usage = msg.usage || {};
    const simulatedBadge = msg.simulated ? '<span class="badge warn" style="margin-right:6px">Dry-Run Simulation</span>' : '';

    let citationsHtml = '';
    if (msg.citations && msg.citations.length) {
      citationsHtml = `
        <div class="chat-citations">
          <div class="chat-citations-title">Sources & Citations:</div>
          <ol>
            ${msg.citations.map((c, i) => `<li><a href="${fmt.htmlSafe(c)}" target="_blank" rel="noopener">${fmt.htmlSafe(c)}</a></li>`).join('')}
          </ol>
        </div>`;
    }

    return `
      <div class="chat-msg assistant">
        <div class="chat-bubble assistant">
          <div class="chat-sender-header">
            <span class="chat-model-badge ${fmt.modelClass(msg.model)}">${msg.model || chatSession.model}</span>
            ${simulatedBadge}
            <span class="spacer"></span>
            <span class="chat-latency">${msg.latency_s ? `${msg.latency_s}s` : ''}</span>
          </div>

          <div class="chat-body markdown-body">
            ${renderMarkdown(msg.content)}
            ${citationsHtml}
          </div>

          <!-- EcoLogits Environmental & Token Impact Bar -->
          <div class="chat-impact-footer">
            <div class="chat-impact-pill" title="Electrical Energy Consumption (Scope 2)">
              <span class="pill-icon">⚡</span>
              <span class="pill-val">${imp.energy_wh ? imp.energy_wh.toFixed(3) : '0.000'} Wh</span>
            </div>
            <div class="chat-impact-pill" title="Global Warming Potential (Carbon Footprint Scope 2+3 LCA)">
              <span class="pill-icon">🌿</span>
              <span class="pill-val">${imp.gwp_gco2eq ? imp.gwp_gco2eq.toFixed(3) : '0.000'} gCO₂</span>
            </div>
            <div class="chat-impact-pill" title="Water Consumption Footprint (Datacenter cooling + generation)">
              <span class="pill-icon">💧</span>
              <span class="pill-val">${imp.wcf_ml ? imp.wcf_ml.toFixed(2) : '0.00'} mL</span>
            </div>
            <div class="chat-impact-pill" title="Token Usage (Prompt / Completion)">
              <span class="pill-icon">🪙</span>
              <span class="pill-val">${fmt.int(usage.input_tokens || 0)} in / ${fmt.int(usage.output_tokens || 0)} out</span>
            </div>
            <div class="chat-impact-pill" title="Estimated Inference Cost (USD)">
              <span class="pill-icon">💵</span>
              <span class="pill-val">${fmt.usd4(msg.cost_usd)}</span>
            </div>
            <span class="spacer"></span>
            <span class="chat-time-tag">${msg.timestamp ? msg.timestamp.slice(11, 19) : ''}</span>
          </div>
        </div>
      </div>
    `;
  }

  function renderChatPane() {
    if (chatSession.messages.length === 0) {
      const meta = getActiveModelMeta();
      const hasKey = hasKeyForModel(meta);
      return `
        <div class="chat-welcome">
          <div class="chat-welcome-icon">💬</div>
          <h2>Interactive AI Chatbot</h2>
          <p class="muted">
            Send prompts to <strong>${meta.name}</strong> with real-time EcoLogits environmental impact estimation, token counters, and cost tracking.
          </p>

          ${!hasKey ? `
            <div class="chat-key-notice">
              <strong>⚠️ No API Key Detected for ${meta.provider.toUpperCase()}</strong>
              <p>You can chat in <em>dry-run simulation mode</em> or enter your API key to enable live LLM inference.</p>
              <button class="primary" id="btn-welcome-key" style="margin-top:8px">Enter ${meta.provider.toUpperCase()} API Key</button>
            </div>
          ` : ''}

          <div class="chat-suggestions-title">Try an example prompt:</div>
          <div class="chat-suggestions">
            ${SUGGESTIONS.map(s => `<button class="chat-chip" data-prompt="${fmt.htmlSafe(s)}">${fmt.htmlSafe(s)}</button>`).join('')}
          </div>
        </div>
      `;
    }

    return `
      <div class="chat-transcript" id="chat-transcript">
        ${chatSession.messages.map(m => renderMessage(m)).join('')}
      </div>
    `;
  }

  function renderLayout() {
    root.innerHTML = `
      <div class="chat-layout">
        ${renderTopbar()}

        <div class="chat-pane-wrapper card" id="chat-pane-wrapper">
          ${renderChatPane()}
        </div>

        <div class="chat-input-container">
          <div class="chat-input-bar">
            <textarea
              id="chat-input"
              rows="1"
              placeholder="Ask a question, write code, or analyze sustainability... (Enter to send, Shift+Enter for new line)"
            ></textarea>
            <button id="btn-send" class="primary chat-send-btn" title="Send message">
              <span id="send-btn-icon">➤</span>
            </button>
          </div>
          <div class="chat-input-footer">
            <span class="muted" style="font-size:11px">Press <strong>Enter</strong> to send, <strong>Shift+Enter</strong> for newline</span>
            <div class="spacer"></div>
            <span id="chat-char-counter" class="muted" style="font-size:11px">0 chars (~0 tokens)</span>
          </div>
        </div>
      </div>
    `;

    bindEvents();
    scrollToBottom();
  }

  function scrollToBottom() {
    const el = document.getElementById('chat-transcript');
    if (el) el.scrollTop = el.scrollHeight;
  }

  function bindEvents() {
    // Model Select
    $('#chat-model-select').addEventListener('change', e => {
      chatSession.model = e.target.value;
      localStorage.setItem('td.chat.model', chatSession.model);
      renderLayout();
    });

    // Toggle Drawer
    $('#btn-toggle-drawer').addEventListener('click', () => {
      const d = $('#chat-drawer');
      d.style.display = d.style.display === 'none' ? 'block' : 'none';
    });

    // Drawer inputs
    if ($('#drawer-system-prompt')) {
      $('#drawer-system-prompt').addEventListener('input', e => {
        chatSession.systemPrompt = e.target.value;
      });
      $('#drawer-temp').addEventListener('input', e => {
        chatSession.temperature = parseFloat(e.target.value);
        $('#val-temp').textContent = chatSession.temperature;
      });
      $('#drawer-max-tokens').addEventListener('change', e => {
        chatSession.maxTokens = parseInt(e.target.value) || 1000;
      });
      $('#drawer-zone').addEventListener('change', e => {
        chatSession.zone = e.target.value;
      });
      $('#drawer-log-db').addEventListener('change', e => {
        chatSession.logToDb = e.target.checked;
      });
    }

    // New Chat
    $('#btn-new-chat').addEventListener('click', () => {
      chatSession.id = 'chat-' + Math.random().toString(36).slice(2, 10);
      chatSession.messages = [];
      chatSession.totals = { inputTokens: 0, outputTokens: 0, energyWh: 0.0, gwpG: 0.0, wcfMl: 0.0, costUsd: 0.0 };
      renderLayout();
    });

    // Export Chat
    $('#btn-export-chat').addEventListener('click', () => {
      if (!chatSession.messages.length) return alert('No messages to export.');
      let md = `# AI Chatbot Export — ${new Date().toISOString()}\n\n`;
      md += `**Model**: ${chatSession.model} | **Session ID**: ${chatSession.id}\n\n`;
      md += `**Total Energy**: ${chatSession.totals.energyWh.toFixed(3)} Wh | **Total Carbon**: ${chatSession.totals.gwpG.toFixed(3)} gCO₂eq | **Est Cost**: $${chatSession.totals.costUsd.toFixed(4)}\n\n---\n\n`;
      for (const m of chatSession.messages) {
        md += `### ${m.role === 'user' ? 'User' : `Assistant (${m.model || chatSession.model})`}\n\n${m.content}\n\n`;
      }
      const blob = new Blob([md], { type: 'text/markdown' });
      const a = document.createElement('a');
      a.href = URL.createObjectURL(blob);
      a.download = `chat-export-${chatSession.id}.md`;
      a.click();
    });

    // API Key Modal buttons
    $('#btn-open-keys').addEventListener('click', () => openApiKeyModal());
    if ($('#btn-welcome-key')) {
      $('#btn-welcome-key').addEventListener('click', () => openApiKeyModal());
    }

    // Suggestion chips
    $$('.chat-chip').forEach(chip => {
      chip.addEventListener('click', () => {
        const p = chip.dataset.prompt;
        $('#chat-input').value = p;
        sendMessage();
      });
    });

    // Input character counter & auto-resize
    const input = $('#chat-input');
    input.addEventListener('input', () => {
      input.style.height = 'auto';
      input.style.height = Math.min(input.scrollHeight, 180) + 'px';
      const len = input.value.length;
      const estTokens = Math.ceil(len / 4);
      $('#chat-char-counter').textContent = `${len} chars (~${estTokens} tokens)`;
    });

    // Keyboard send
    input.addEventListener('keydown', e => {
      if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        sendMessage();
      }
    });

    // Send button
    $('#btn-send').addEventListener('click', sendMessage);
  }

  async function sendMessage() {
    const input = $('#chat-input');
    const text = (input.value || '').trim();
    if (!text) return;

    input.value = '';
    input.style.height = 'auto';
    $('#chat-char-counter').textContent = '0 chars (~0 tokens)';

    const userMsg = {
      role: 'user',
      content: text,
      timestamp: new Date().toISOString(),
    };
    chatSession.messages.push(userMsg);
    renderLayout();

    // Show loading state
    const transcript = $('#chat-transcript');
    const loadingDiv = document.createElement('div');
    loadingDiv.className = 'chat-msg assistant';
    loadingDiv.id = 'chat-loading-bubble';
    loadingDiv.innerHTML = `
      <div class="chat-bubble assistant" style="padding:14px">
        <div class="flex" style="gap:10px">
          <div class="chat-spinner"></div>
          <span class="muted" style="font-size:13px">${getActiveModelMeta().name} is thinking & calculating impact...</span>
        </div>
      </div>
    `;
    if (transcript) {
      transcript.appendChild(loadingDiv);
      scrollToBottom();
    }

    $('#btn-send').disabled = true;
    $('#send-btn-icon').textContent = '⏳';

    const meta = getActiveModelMeta();
    const clientKey = keys.get(meta.provider);

    const payload = {
      model: chatSession.model,
      provider: meta.provider,
      messages: chatSession.messages.map(m => ({ role: m.role, content: m.content })),
      system: chatSession.systemPrompt || undefined,
      max_tokens: chatSession.maxTokens,
      temperature: chatSession.temperature,
      zone: chatSession.zone,
      session_id: chatSession.id,
      log_to_db: chatSession.logToDb,
      api_key: clientKey || undefined,
    };

    try {
      const resp = await fetch('/api/chat', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });

      if (!resp.ok) {
        const errJson = await resp.json().catch(() => ({}));
        throw new Error(errJson.error || `Server responded with ${resp.status}`);
      }

      const res = await resp.json();
      const asstMsg = {
        role: 'assistant',
        model: res.model,
        content: res.message.content,
        usage: res.usage,
        latency_s: res.latency_s,
        cost_usd: res.cost_usd,
        impacts: res.impacts,
        citations: res.citations || [],
        simulated: res.simulated,
        timestamp: new Date().toISOString(),
      };

      chatSession.messages.push(asstMsg);

      // Accumulate totals
      if (res.usage) {
        chatSession.totals.inputTokens += res.usage.input_tokens || 0;
        chatSession.totals.outputTokens += res.usage.output_tokens || 0;
      }
      if (res.impacts) {
        chatSession.totals.energyWh += res.impacts.energy_wh || 0.0;
        chatSession.totals.gwpG += res.impacts.gwp_gco2eq || 0.0;
        chatSession.totals.wcfMl += res.impacts.wcf_ml || 0.0;
      }
      if (res.cost_usd) {
        chatSession.totals.costUsd += res.cost_usd;
      }
    } catch (err) {
      chatSession.messages.push({
        role: 'assistant',
        content: `**Error communicating with LLM:**\n\n\`${fmt.htmlSafe(err.message)}\`\n\nPlease check your API key in settings or top bar.`,
        timestamp: new Date().toISOString(),
        impacts: {},
        usage: {},
      });
    } finally {
      $('#btn-send').disabled = false;
      $('#send-btn-icon').textContent = '➤';
      renderLayout();
    }
  }

  function openApiKeyModal() {
    const meta = getActiveModelMeta();
    const curPplx = keys.get('perplexity');
    const curAnth = keys.get('anthropic');
    const curOpenAi = keys.get('openai');

    const overlay = document.createElement('div');
    overlay.className = 'modal-overlay';
    overlay.innerHTML = `
      <div class="modal" style="max-width:540px;width:90%">
        <h2>API Key Configuration</h2>
        <p class="muted" style="margin-bottom:16px">
          Enter your API keys to enable live inference. Keys are stored locally in your browser (<code>localStorage</code>) and are never sent anywhere except directly to the provider endpoints.
        </p>

        <div style="margin-bottom:14px">
          <label style="display:block;margin-bottom:4px;font-weight:600">Perplexity API Key (${serverKeys.perplexity ? '✓ Env configured' : 'Not in env'})</label>
          <div class="flex">
            <input type="password" id="modal-key-pplx" value="${fmt.htmlSafe(curPplx)}" placeholder="pplx-..." class="chat-input-full">
            <button id="modal-test-pplx" class="chat-action-btn">Test</button>
          </div>
          <span id="status-pplx" class="muted" style="font-size:11px"></span>
        </div>

        <div style="margin-bottom:14px">
          <label style="display:block;margin-bottom:4px;font-weight:600">Anthropic Claude API Key (${serverKeys.anthropic ? '✓ Env configured' : 'Not in env'})</label>
          <div class="flex">
            <input type="password" id="modal-key-anth" value="${fmt.htmlSafe(curAnth)}" placeholder="sk-ant-..." class="chat-input-full">
            <button id="modal-test-anth" class="chat-action-btn">Test</button>
          </div>
          <span id="status-anth" class="muted" style="font-size:11px"></span>
        </div>

        <div style="margin-bottom:14px">
          <label style="display:block;margin-bottom:4px;font-weight:600">OpenAI API Key (${serverKeys.openai ? '✓ Env configured' : 'Not in env'})</label>
          <div class="flex">
            <input type="password" id="modal-key-openai" value="${fmt.htmlSafe(curOpenAi)}" placeholder="sk-..." class="chat-input-full">
            <button id="modal-test-openai" class="chat-action-btn">Test</button>
          </div>
          <span id="status-openai" class="muted" style="font-size:11px"></span>
        </div>

        <div class="actions" style="margin-top:20px">
          <button class="ghost" id="modal-btn-clear">Clear All</button>
          <div class="spacer"></div>
          <button class="ghost" id="modal-btn-cancel">Close</button>
          <button class="primary" id="modal-btn-save">Save Keys</button>
        </div>
      </div>
    `;

    document.body.appendChild(overlay);

    async function testKey(provider, keyInputId, statusId) {
      const val = $(keyInputId, overlay).value.trim();
      const statusEl = $(statusId, overlay);
      if (!val) {
        statusEl.textContent = 'Please enter a key first.';
        statusEl.style.color = 'var(--bad)';
        return;
      }
      statusEl.textContent = 'Testing connection...';
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

    $('#modal-test-pplx', overlay).addEventListener('click', () => testKey('perplexity', '#modal-key-pplx', '#status-pplx'));
    $('#modal-test-anth', overlay).addEventListener('click', () => testKey('anthropic', '#modal-key-anth', '#status-anth'));
    $('#modal-test-openai', overlay).addEventListener('click', () => testKey('openai', '#modal-key-openai', '#status-openai'));

    $('#modal-btn-clear', overlay).addEventListener('click', () => {
      keys.set('perplexity', '');
      keys.set('anthropic', '');
      keys.set('openai', '');
      overlay.remove();
      renderLayout();
    });

    $('#modal-btn-cancel', overlay).addEventListener('click', () => overlay.remove());

    $('#modal-btn-save', overlay).addEventListener('click', () => {
      keys.set('perplexity', $('#modal-key-pplx', overlay).value);
      keys.set('anthropic', $('#modal-key-anth', overlay).value);
      keys.set('openai', $('#modal-key-openai', overlay).value);
      overlay.remove();
      renderLayout();
    });
  }

  renderLayout();
}
