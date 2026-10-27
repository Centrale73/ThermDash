"""n8n workflow graph primitives: vendor map, edges, prompts, Mermaid.

Ported from ThermDash-AutoAudit/src/nexus_waste_detector.py and extended with
langchain model-node resolution (ai_languageModel edges) and modelId pinning.
"""
from __future__ import annotations

import re

# 1. Graph scan (ported from ThermDash-AutoAudit/src/nexus_waste_detector.py)
VENDOR_ALIASES = {
    "lmChatOpenAi": "openai", "openAi": "openai", "lmChatAzureOpenAi": "openai",
    "lmChatAnthropic": "anthropic", "lmChatOllama": "local_ollama",
    "lmChatLmStudio": "local_lmstudio", "lmChatGoogleGemini": "google",
    "lmChatMistralCloud": "mistral", "mistralAi": "mistral", "lmChatGroq": "groq",
    "lmChatDeepSeek": "deepseek", "lmChatOpenRouter": "openrouter",
    "telegram": "telegram", "telegramTrigger": "telegram", "httpRequest": "custom_api",
    "agent": "langchain_agent", "chainLlm": "langchain_logic",
    "outputParserStructured": "langchain_logic", "memoryBufferWindow": "n8n_memory",
    "set": "native_data", "if": "native_logic", "switch": "native_logic",
    "code": "native_logic", "limit": "native_logic", "wait": "native_logic",
    "baserowTool": "database", "gmailTool": "google", "gmail": "google",
    "googleCalendarTool": "google", "googleSheets": "google", "cognee": "local_memory",
    "stickyNote": "native_ui", "manualTrigger": "native_trigger",
    "scheduleTrigger": "native_trigger", "formTrigger": "native_trigger",
    "errorTrigger": "native_trigger", "webhook": "native_trigger",
}
CLOUD_VENDORS = {"openai", "anthropic", "google", "telegram", "mistral", "groq", "deepseek", "openrouter"}
# Nodes that execute a model forward pass themselves.
MODEL_NODE_TYPES = {"openAi", "lmChatOpenAi", "lmChatAzureOpenAi", "lmChatAnthropic",
                    "lmChatOllama", "lmChatLmStudio", "lmChatGoogleGemini",
                    "lmChatMistralCloud", "mistralAi", "lmChatGroq", "lmChatDeepSeek",
                    "lmChatOpenRouter"}
# Nodes that orchestrate a connected model (prompt lives here, model lives in sub-node).
ORCHESTRATOR_TYPES = {"chainLlm", "agent"}
CLOUD_AI_DOMAINS = ["api.openai.com", "api.anthropic.com", "api.cohere.ai", "api.mistral.ai",
                    "generativelanguage.googleapis.com", "api.together.xyz", "openrouter.ai",
                    "api.groq.com", "api.vapi.ai", "api.retellai.com", "api.bland.ai"]
PLACEHOLDER_RE = re.compile(r"(INSERISCI|INSERT|YOUR[_-]|REPLACE|CHANGE[_-]?ME|<[^>]+>|XXX+|TODO)", re.I)
PII_HINTS = ["email", "from", "name", "phone", "dob", "address", "postal", "birth", "ssn",
             "sin", "patient", "mittente", "da:"]
LEGAL = {
    "law25": {
        "transfer": "Quebec private-sector Act s.17 (Law 25): privacy impact assessment before communicating personal information outside Quebec",
        "accountability": "Law 25: accountability for automated processing (no error path)",
    },
    "gdpr": {
        "transfer": "GDPR Chapter V (Arts. 44-49): transfer of personal data to third countries needs a transfer mechanism",
        "accountability": "GDPR Arts. 5(2) and 32: accountability and processing integrity (no error path)",
    },
}


def rtype(node: dict) -> str:
    return (node.get("type") or "").split(".")[-1]


def model_id(node: dict) -> str | None:
    """Return the pinned model id. Fixes AutoAudit's false 'unpinned' flag: n8n's
    @n8n/n8n-nodes-langchain.openAi stores it in parameters.modelId.__rl.value."""
    p = node.get("parameters", {}) or {}
    for key in ("modelId", "model", "modelName"):
        v = p.get(key)
        if isinstance(v, dict):
            v = v.get("value") or v.get("cachedResultName")
        if isinstance(v, str) and v.strip():
            return v.strip()
    v = (p.get("options") or {}).get("model")
    return v if isinstance(v, str) and v else None


def walk_connections(wf: dict):
    """Yield (source, kind, target) for every edge."""
    for src, outs in (wf.get("connections") or {}).items():
        for kind, groups in outs.items():
            for group in groups or []:
                for t in group or []:
                    yield src, kind, t.get("node")


def downstream(wf: dict, start: str) -> list[str]:
    seen, stack = [], [start]
    edges = {}
    for s, k, t in walk_connections(wf):
        if k == "main":
            edges.setdefault(s, []).append(t)
    while stack:
        n = stack.pop()
        for t in edges.get(n, []):
            if t not in seen:
                seen.append(t)
                stack.append(t)
    return seen


def upstream(wf: dict, target: str) -> list[str]:
    rev = {}
    for s, k, t in walk_connections(wf):
        if k == "main":
            rev.setdefault(t, []).append(s)
    seen, stack = [], [target]
    while stack:
        n = stack.pop()
        for s in rev.get(n, []):
            if s not in seen:
                seen.append(s)
                stack.append(s)
    return seen


def extract_prompts(node: dict) -> list[dict]:
    p = node.get("parameters", {}) or {}
    msgs = []
    for m in ((p.get("messages") or {}).get("values") or []):
        msgs.append({"role": m.get("role", "user"), "content": m.get("content", "")})
    for m in ((p.get("messages") or {}).get("messageValues") or []):
        msgs.append({"role": m.get("type", "system"), "content": m.get("message", "")})
    sysmsg = (p.get("options") or {}).get("systemMessage")
    if sysmsg:
        msgs.append({"role": "system", "content": sysmsg})
    for key in ("text", "prompt"):
        if isinstance(p.get(key), str) and p[key]:
            msgs.append({"role": "user", "content": p[key]})
    return msgs


def split_template(s: str) -> tuple[str, list[str]]:
    """Separate static text from n8n {{ expressions }}."""
    s = s[1:] if s.startswith("=") else s
    exprs = re.findall(r"\{\{(.*?)\}\}", s, re.S)
    static = re.sub(r"\{\{.*?\}\}", "", s, flags=re.S)
    return static, [e.strip() for e in exprs]


def mermaid(wf: dict, sites: list[dict]) -> str:
    ai = {s["node"] for s in sites}
    lines = ["graph LR",
             "  classDef cloud fill:#fdecea,stroke:#c62828,color:#5d0f0f",
             "  classDef ai fill:#fff4e5,stroke:#e65100,stroke-width:3px,color:#4a2600",
             "  classDef local fill:#e8f5e9,stroke:#2e7d32,color:#0b3d12"]
    ids = {}
    for i, n in enumerate(x for x in wf.get("nodes", []) if rtype(x) != "stickyNote"):
        ids[n["name"]] = f"N{i}"
        v = VENDOR_ALIASES.get(rtype(n), "unknown")
        label = n["name"].replace('"', "'")
        cls = "ai" if n["name"] in ai else ("cloud" if v in CLOUD_VENDORS else "local")
        tag = " [E_in]" if cls == "ai" else ""
        lines.append(f'  N{i}["{label}{tag}"]:::{cls}')
    for s, k, t in walk_connections(wf):
        if s in ids and t in ids:
            lines.append(f"  {ids[s]} -->|{k}| {ids[t]}")
    return "\n".join(lines)


