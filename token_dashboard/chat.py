"""Chatbot module: Handles multi-provider LLM chat completions, EcoLogits impact estimation, and optional DB logging."""
from __future__ import annotations

import json
import os
import sqlite3
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from .ecologits_bridge import estimate_impacts, get_grid_intensity
from .pricing import cost_for

# Default pricing fallbacks for models not listed in pricing.json
EXTRA_MODEL_PRICING = {
    "sonar":                {"tier": "haiku",  "input": 1.00, "output": 1.00, "cache_read": 0.10, "cache_create_5m": 1.00, "cache_create_1h": 1.00},
    "sonar-pro":            {"tier": "sonnet", "input": 3.00, "output": 15.00, "cache_read": 0.30, "cache_create_5m": 3.00, "cache_create_1h": 3.00},
    "sonar-reasoning":      {"tier": "sonnet", "input": 1.00, "output": 5.00, "cache_read": 0.10, "cache_create_5m": 1.00, "cache_create_1h": 1.00},
    "sonar-reasoning-pro":  {"tier": "opus",   "input": 5.00, "output": 25.00, "cache_read": 0.50, "cache_create_5m": 5.00, "cache_create_1h": 5.00},
    "llama-3.1-sonar-small-128k-online": {"tier": "haiku", "input": 1.00, "output": 1.00, "cache_read": 0.10, "cache_create_5m": 1.00, "cache_create_1h": 1.00},
    "llama-3.1-sonar-large-128k-online": {"tier": "sonnet", "input": 3.00, "output": 15.00, "cache_read": 0.30, "cache_create_5m": 3.00, "cache_create_1h": 3.00},
    "llama-3.1-sonar-huge-128k-online":  {"tier": "opus", "input": 5.00, "output": 25.00, "cache_read": 0.50, "cache_create_5m": 5.00, "cache_create_1h": 5.00},
    "gpt-4o":               {"tier": "sonnet", "input": 2.50, "output": 10.00, "cache_read": 1.25, "cache_create_5m": 2.50, "cache_create_1h": 2.50},
    "gpt-4o-mini":          {"tier": "haiku",  "input": 0.15, "output": 0.60,  "cache_read": 0.075, "cache_create_5m": 0.15, "cache_create_1h": 0.15},
    "o3-mini":              {"tier": "sonnet", "input": 1.10, "output": 4.40,  "cache_read": 0.55, "cache_create_5m": 1.10, "cache_create_1h": 1.10},
    "o1":                   {"tier": "opus",   "input": 15.00, "output": 60.00, "cache_read": 7.50, "cache_create_5m": 15.00, "cache_create_1h": 15.00},
}


def get_configured_keys() -> Dict[str, bool]:
    """Check which provider API keys are configured in environment variables."""
    return {
        "perplexity": bool(os.environ.get("PPLX_API_KEY") or os.environ.get("PERPLEXITY_API_KEY")),
        "anthropic": bool(os.environ.get("ANTHROPIC_API_KEY")),
        "openai": bool(os.environ.get("OPENAI_API_KEY")),
    }


def resolve_api_key(provider: str, client_key: Optional[str] = None) -> str:
    """Resolve API key from client payload first, then environment variables."""
    if client_key and client_key.strip():
        return client_key.strip()
    
    p = (provider or "").lower().strip()
    if p in ("perplexity", "pplx", "sonar"):
        return os.environ.get("PPLX_API_KEY") or os.environ.get("PERPLEXITY_API_KEY") or ""
    elif p in ("anthropic", "claude"):
        return os.environ.get("ANTHROPIC_API_KEY") or ""
    elif p in ("openai", "gpt"):
        return os.environ.get("OPENAI_API_KEY") or ""
    return ""


def detect_provider(model: str) -> str:
    """Detect the provider based on the model name."""
    m = (model or "").lower().strip()
    if any(k in m for k in ("sonar", "pplx", "perplexity", "llama")):
        return "perplexity"
    elif any(k in m for k in ("claude", "anthropic")):
        return "anthropic"
    elif any(k in m for k in ("gpt", "o1", "o3", "openai")):
        return "openai"
    return "perplexity"


def _call_perplexity(
    api_key: str,
    model: str,
    messages: List[Dict[str, str]],
    system: Optional[str] = None,
    max_tokens: int = 1000,
    temperature: float = 0.7,
) -> Dict[str, Any]:
    url = "https://api.perplexity.ai/chat/completions"
    
    formatted_messages = []
    if system:
        formatted_messages.append({"role": "system", "content": system})
    formatted_messages.extend(messages)
    
    payload = {
        "model": model,
        "messages": formatted_messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": "TokenDashboard/1.0",
        },
    )
    
    t0 = time.perf_counter()
    with urllib.request.urlopen(req, timeout=60) as resp:
        raw = resp.read().decode("utf-8")
        latency = round(time.perf_counter() - t0, 3)
        res = json.loads(raw)
        choice = (res.get("choices") or [{}])[0]
        msg = choice.get("message") or {}
        usage = res.get("usage") or {}
        
        prompt_tokens = int(usage.get("prompt_tokens") or 0)
        completion_tokens = int(usage.get("completion_tokens") or 0)
        cached_tokens = int(
            usage.get("prompt_tokens_details", {}).get("cached_tokens", 0)
            if isinstance(usage.get("prompt_tokens_details"), dict) else 0
        )
        
        citations = res.get("citations") or []
        
        return {
            "content": msg.get("content", ""),
            "role": "assistant",
            "input_tokens": prompt_tokens,
            "output_tokens": completion_tokens,
            "cached_tokens": cached_tokens,
            "latency_s": latency,
            "citations": citations,
            "simulated": False,
        }


def _call_anthropic(
    api_key: str,
    model: str,
    messages: List[Dict[str, str]],
    system: Optional[str] = None,
    max_tokens: int = 1000,
    temperature: float = 0.7,
) -> Dict[str, Any]:
    url = "https://api.anthropic.com/v1/messages"
    
    # Filter and format messages for Anthropic (roles must alternate user/assistant)
    anthropic_messages = []
    for m in messages:
        role = m.get("role", "user")
        if role in ("user", "assistant"):
            anthropic_messages.append({"role": role, "content": m.get("content", "")})
    
    payload: Dict[str, Any] = {
        "model": model,
        "messages": anthropic_messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    if system:
        payload["system"] = system

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
            "User-Agent": "TokenDashboard/1.0",
        },
    )
    
    t0 = time.perf_counter()
    with urllib.request.urlopen(req, timeout=60) as resp:
        raw = resp.read().decode("utf-8")
        latency = round(time.perf_counter() - t0, 3)
        res = json.loads(raw)
        
        content_blocks = res.get("content") or []
        text_parts = [b.get("text", "") for b in content_blocks if isinstance(b, dict) and b.get("type") == "text"]
        content = "".join(text_parts)
        
        usage = res.get("usage") or {}
        input_tokens = int(usage.get("input_tokens") or 0)
        output_tokens = int(usage.get("output_tokens") or 0)
        cache_read = int(usage.get("cache_read_input_tokens") or 0)
        
        return {
            "content": content,
            "role": "assistant",
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cached_tokens": cache_read,
            "latency_s": latency,
            "citations": [],
            "simulated": False,
        }


def _call_openai(
    api_key: str,
    model: str,
    messages: List[Dict[str, str]],
    system: Optional[str] = None,
    max_tokens: int = 1000,
    temperature: float = 0.7,
) -> Dict[str, Any]:
    url = "https://api.openai.com/v1/chat/completions"
    
    formatted_messages = []
    if system:
        formatted_messages.append({"role": "system", "content": system})
    formatted_messages.extend(messages)
    
    payload = {
        "model": model,
        "messages": formatted_messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": "TokenDashboard/1.0",
        },
    )
    
    t0 = time.perf_counter()
    with urllib.request.urlopen(req, timeout=60) as resp:
        raw = resp.read().decode("utf-8")
        latency = round(time.perf_counter() - t0, 3)
        res = json.loads(raw)
        choice = (res.get("choices") or [{}])[0]
        msg = choice.get("message") or {}
        usage = res.get("usage") or {}
        
        prompt_tokens = int(usage.get("prompt_tokens") or 0)
        completion_tokens = int(usage.get("completion_tokens") or 0)
        cached_tokens = int(
            usage.get("prompt_tokens_details", {}).get("cached_tokens", 0)
            if isinstance(usage.get("prompt_tokens_details"), dict) else 0
        )
        
        return {
            "content": msg.get("content", ""),
            "role": "assistant",
            "input_tokens": prompt_tokens,
            "output_tokens": completion_tokens,
            "cached_tokens": cached_tokens,
            "latency_s": latency,
            "citations": [],
            "simulated": False,
        }


def _simulate_response(
    model: str,
    messages: List[Dict[str, str]],
    system: Optional[str] = None,
    max_tokens: int = 1000,
) -> Dict[str, Any]:
    """Generate an informative simulated response with token & environmental metrics when in dry-run mode."""
    t0 = time.perf_counter()
    time.sleep(0.08)
    latency = round(time.perf_counter() - t0, 3)
    
    last_user_prompt = ""
    for m in reversed(messages):
        if m.get("role") == "user":
            last_user_prompt = m.get("content", "")
            break
            
    input_tokens = max(15, len(last_user_prompt.split()) * 2 + (len(system.split()) if system else 0))
    output_tokens = min(max_tokens, 120)
    
    simulated_content = (
        f"**[Simulated Response • Model: `{model}`]**\n\n"
        f"You asked: *\"{last_user_prompt[:120]}{'...' if len(last_user_prompt) > 120 else ''}\"*\n\n"
        f"This is a simulated response running locally in dry-run mode because no live API key was provided for `{model}`. "
        f"To enable live inference with real LLM completions, add your API key in the **Chat top bar** or in the **Settings tab**.\n\n"
        f"All token analytics, cost estimation, and EcoLogits environmental impact metrics below are calculated using full LCA models."
    )
    
    return {
        "content": simulated_content,
        "role": "assistant",
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cached_tokens": 0,
        "latency_s": latency,
        "citations": [],
        "simulated": True,
    }


def test_api_key(provider: str, api_key: str) -> Dict[str, Any]:
    """Test API key connectivity by sending a lightweight 1-token test prompt."""
    p = (provider or "").lower().strip()
    key = api_key.strip()
    if not key:
        return {"ok": False, "error": "API key cannot be empty"}
    
    try:
        if p == "perplexity":
            _call_perplexity(key, "sonar", [{"role": "user", "content": "ping"}], max_tokens=2)
            return {"ok": True, "provider": "perplexity", "message": "Perplexity API key verified successfully."}
        elif p == "anthropic":
            _call_anthropic(key, "claude-3-5-haiku-20241022", [{"role": "user", "content": "ping"}], max_tokens=2)
            return {"ok": True, "provider": "anthropic", "message": "Anthropic API key verified successfully."}
        elif p == "openai":
            _call_openai(key, "gpt-4o-mini", [{"role": "user", "content": "ping"}], max_tokens=2)
            return {"ok": True, "provider": "openai", "message": "OpenAI API key verified successfully."}
        else:
            return {"ok": False, "error": f"Unknown provider: {provider}"}
    except urllib.error.HTTPError as e:
        err_msg = e.read().decode("utf-8", errors="replace")
        return {"ok": False, "error": f"HTTP {e.code}: {err_msg[:200]}"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def log_turn_to_db(
    db_path: str,
    session_id: str,
    user_prompt: str,
    assistant_response: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    cached_tokens: int,
    impacts: Dict[str, Any],
    project_slug: str = "chat-playground",
) -> None:
    """Log the chat turn into SQLite database so it seamlessly populates dashboard charts and tables."""
    now_iso = datetime.now(timezone.utc).isoformat()
    u_uuid = str(uuid.uuid4())
    a_uuid = str(uuid.uuid4())
    
    with sqlite3.connect(db_path) as conn:
        # 1. User message
        conn.execute(
            """
            INSERT OR REPLACE INTO messages (
                uuid, parent_uuid, session_id, project_slug, type,
                timestamp, model, input_tokens, output_tokens,
                cache_read_tokens, cache_create_5m_tokens, cache_create_1h_tokens,
                prompt_text, prompt_chars
            ) VALUES (?, NULL, ?, ?, 'user', ?, ?, 0, 0, 0, 0, 0, ?, ?)
            """,
            (u_uuid, session_id, project_slug, now_iso, model, user_prompt, len(user_prompt)),
        )
        
        # 2. Assistant message with EcoLogits environmental impacts
        conn.execute(
            """
            INSERT OR REPLACE INTO messages (
                uuid, parent_uuid, session_id, project_slug, type,
                timestamp, model, input_tokens, output_tokens,
                cache_read_tokens, cache_create_5m_tokens, cache_create_1h_tokens,
                energy_kwh, gwp_kgco2eq, wcf_l, adpe_kgsbeq, pe_mj, impact_source
            ) VALUES (?, ?, ?, ?, 'assistant', ?, ?, ?, ?, ?, 0, 0, ?, ?, ?, ?, ?, ?)
            """,
            (
                a_uuid, u_uuid, session_id, project_slug, now_iso, model,
                input_tokens, output_tokens, cached_tokens,
                impacts.get("energy_kwh", 0.0),
                impacts.get("gwp_kgco2eq", 0.0),
                impacts.get("wcf_l", 0.0),
                impacts.get("adpe_kgsbeq", 0.0),
                impacts.get("pe_mj", 0.0),
                impacts.get("source", "ecologits_chat"),
            ),
        )
        conn.commit()


def handle_chat_request(db_path: str, body: Dict[str, Any], pricing: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Main handler for chat requests: calls provider, estimates impacts, computes cost, and optionally logs to DB."""
    model = body.get("model") or "sonar"
    provider = body.get("provider") or detect_provider(model)
    messages = body.get("messages") or []
    system = body.get("system")
    max_tokens = int(body.get("max_tokens") or 1000)
    temperature = float(body.get("temperature") if body.get("temperature") is not None else 0.7)
    electricity_zone = body.get("zone") or "USA"
    session_id = body.get("session_id") or str(uuid.uuid4())
    log_to_db = bool(body.get("log_to_db", True))
    
    # Resolve API Key
    client_key = body.get("api_key")
    api_key = resolve_api_key(provider, client_key)
    is_dry_run = body.get("dry_run", False) or not bool(api_key)
    
    # Call Provider API or simulate
    if is_dry_run:
        result = _simulate_response(model, messages, system, max_tokens)
    else:
        try:
            p = provider.lower()
            if p in ("perplexity", "pplx", "sonar"):
                result = _call_perplexity(api_key, model, messages, system, max_tokens, temperature)
            elif p in ("anthropic", "claude"):
                result = _call_anthropic(api_key, model, messages, system, max_tokens, temperature)
            elif p in ("openai", "gpt"):
                result = _call_openai(api_key, model, messages, system, max_tokens, temperature)
            else:
                raise ValueError(f"Unsupported provider '{provider}'")
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"API Error ({provider} HTTP {e.code}): {err_body}")
        except Exception as e:
            raise RuntimeError(f"Error communicating with {provider}: {e}")

    # Environmental Impact Calculation via EcoLogits bridge
    input_tokens = result.get("input_tokens", 0)
    output_tokens = result.get("output_tokens", 0)
    cached_tokens = result.get("cached_tokens", 0)
    
    impact = estimate_impacts(
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cached_tokens,
        electricity_zone=electricity_zone,
    )
    
    energy_wh = round(impact.energy_kwh * 1000.0, 4)
    gwp_g = round(impact.gwp_kgco2eq * 1000.0, 4)
    wcf_ml = round(impact.wcf_l * 1000.0, 4)
    total_tokens = input_tokens + output_tokens
    
    # Cost estimation
    cost_usd = 0.0
    effective_pricing = pricing or {}
    models_dict = effective_pricing.get("models", {})
    if model in models_dict:
        c = cost_for(model, {"input_tokens": input_tokens, "output_tokens": output_tokens, "cache_read_tokens": cached_tokens, "cache_create_5m_tokens": 0, "cache_create_1h_tokens": 0}, effective_pricing)
        cost_usd = c.get("usd") or 0.0
    elif model in EXTRA_MODEL_PRICING:
        p_info = EXTRA_MODEL_PRICING[model]
        cost_usd = (input_tokens / 1_000_000.0) * p_info["input"] + (output_tokens / 1_000_000.0) * p_info["output"]
    else:
        # Fallback rates: $1/1M input, $3/1M output
        cost_usd = (input_tokens / 1_000_000.0) * 1.00 + (output_tokens / 1_000_000.0) * 3.00
    
    impacts_payload = {
        "energy_kwh": impact.energy_kwh,
        "energy_wh": energy_wh,
        "gwp_kgco2eq": impact.gwp_kgco2eq,
        "gwp_gco2eq": gwp_g,
        "wcf_l": impact.wcf_l,
        "wcf_ml": wcf_ml,
        "pe_mj": impact.pe_mj,
        "adpe_kgsbeq": impact.adpe_kgsbeq,
        "source": impact.source,
        "grid_zone": electricity_zone,
        "grid_gwp": get_grid_intensity(electricity_zone),
        "tokens_per_wh": round(output_tokens / energy_wh, 2) if energy_wh > 0 else 0.0,
        "wh_per_token": round(energy_wh / total_tokens, 6) if total_tokens > 0 else 0.0,
    }
    
    # Optional Database Persistence
    if log_to_db and messages:
        last_user_msg = ""
        for m in reversed(messages):
            if m.get("role") == "user":
                last_user_msg = m.get("content", "")
                break
        try:
            log_turn_to_db(
                db_path=db_path,
                session_id=session_id,
                user_prompt=last_user_msg,
                assistant_response=result.get("content", ""),
                model=model,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cached_tokens=cached_tokens,
                impacts=impacts_payload,
            )
        except Exception:
            pass  # Non-blocking DB logging
            
    return {
        "session_id": session_id,
        "model": model,
        "provider": provider,
        "message": {
            "role": "assistant",
            "content": result.get("content", ""),
        },
        "usage": {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cached_tokens": cached_tokens,
            "total_tokens": total_tokens,
        },
        "latency_s": result.get("latency_s", 0.0),
        "cost_usd": round(cost_usd, 6),
        "impacts": impacts_payload,
        "citations": result.get("citations", []),
        "simulated": result.get("simulated", False),
    }
