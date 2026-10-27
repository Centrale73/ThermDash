"""Static scan of an n8n workflow: inference sites, governance flags, input shaping.

Flags are energy-relevant first: anything that makes delivered work zero (placeholders,
parse failures, silent errors) is a delivery-gate (D) problem, not a cosmetic one.
"""
from __future__ import annotations

import datetime as dt
import json
import re

from .graph import (CLOUD_AI_DOMAINS, CLOUD_VENDORS, LEGAL, MODEL_NODE_TYPES, ORCHESTRATOR_TYPES,
                    PII_HINTS, PLACEHOLDER_RE, VENDOR_ALIASES, extract_prompts, mermaid, model_id,
                    rtype, upstream, walk_connections)
from .schedule import cron_run_times, lookback_minutes, schedule_info, temporal_coverage


def scan(wf: dict, jurisdiction: str = "gdpr", year: int | None = None) -> dict:
    year = year or dt.date.today().year
    nodes = [n for n in wf.get("nodes", []) if rtype(n) != "stickyNote"]
    by_name = {n.get("name"): n for n in nodes}
    law = LEGAL.get(jurisdiction, LEGAL["gdpr"])
    flags = {"red": [], "yellow": [], "green": [], "energy": []}

    # Inference sites: model nodes + orchestrators resolved through ai_languageModel edges.
    sites = []
    lm_edges = [(s, t) for s, k, t in walk_connections(wf) if k == "ai_languageModel"]
    for n in nodes:
        t = rtype(n)
        if t in ORCHESTRATOR_TYPES:
            models = [by_name.get(s) for s, tt in lm_edges if tt == n.get("name")]
            m = next((model_id(x) for x in models if x and model_id(x)), None)
            sites.append({"node": n["name"], "model_node": [x["name"] for x in models if x],
                          "model": m, "prompts": extract_prompts(n)})
        elif t in MODEL_NODE_TYPES and not any(s == n.get("name") for s, _ in lm_edges):
            res = (n.get("parameters") or {}).get("resource", "text")
            sites.append({"node": n["name"], "model_node": [n["name"]], "model": model_id(n),
                          "resource": res, "prompts": extract_prompts(n)})

    has_error_path = any(rtype(n) == "errorTrigger" for n in nodes) or \
        bool((wf.get("settings") or {}).get("errorWorkflow"))
    if not has_error_path:
        flags["red"].append(f"No error trigger / errorWorkflow: failures after inference are silent "
                            f"and their energy is unaccounted. [{law['accountability']}]")

    vendors = set()
    for n in nodes:
        t, name, p = rtype(n), n.get("name"), n.get("parameters", {}) or {}
        v = VENDOR_ALIASES.get(t, "unknown")
        if v in CLOUD_VENDORS:
            vendors.add(v)
        if t in MODEL_NODE_TYPES and not model_id(n) and v not in ("local_ollama", "local_lmstudio"):
            flags["yellow"].append(f"Model unpinned in '{name}': energy constants cannot be bound.")
        if t == "httpRequest":
            url = str(p.get("url", ""))
            for d in CLOUD_AI_DOMAINS:
                if d in url:
                    flags["red"].append(f"'{name}' calls {d} via raw HTTP: inference energy "
                                        f"hidden from node-type detection. [{law['transfer']}]")
        if t == "wait":
            flags["energy"].append(f"'{name}' is a wait/poll loop: each poll is an execution; count polls in runs.")
        if v in ("local_ollama", "local_lmstudio", "local_memory"):
            flags["green"].append(f"'{name}' runs on local infrastructure.")
        blob = json.dumps(p, ensure_ascii=False)
        if PLACEHOLDER_RE.search(blob) and t not in ("code",):
            flags["red"].append(f"Unconfigured placeholder in '{name}' "
                                f"({PLACEHOLDER_RE.search(blob).group(0)}): delivery gate D = 0 until set.")
        if t == "telegram" and (p.get("additionalFields") or {}).get("parse_mode") == "Markdown":
            flags["yellow"].append(f"'{name}' uses legacy Telegram Markdown on LLM text: an unescaped "
                                   f"'_', '*', '`' or '[' rejects the whole message (D < 1).")
    for s in sites:
        up_types = {rtype(by_name[u]) for u in upstream(wf, s["node"]) if u in by_name}
        pblob = json.dumps(s["prompts"], ensure_ascii=False).lower()
        if up_types & {"gmail", "gmailTool", "formTrigger", "telegramTrigger", "googleSheets"} or \
                any(h in pblob for h in PII_HINTS):
            v = VENDOR_ALIASES.get(rtype(by_name[s["model_node"][0]]), "unknown") if s["model_node"] else "unknown"
            if v in CLOUD_VENDORS:
                flags["red"].append(f"Personal data (sender names, message text) reaches cloud model "
                                    f"'{s['node']}' ({v}). [{law['transfer']}]")
        s["guarded_by_if"] = "if" in up_types
    if len({v for v in vendors if v not in ('telegram', 'google')}) == 1:
        flags["yellow"].append("Single AI vendor, no fallback model: one outage = zero useful work.")

    # Static input shaping (item caps, char caps) - feeds the token model.
    shaping = []
    for n in nodes:
        p = n.get("parameters", {}) or {}
        if isinstance(p.get("limit"), int) and p.get("returnAll") is False:
            shaping.append({"node": n["name"], "item_cap": p["limit"],
                            "query": (p.get("filters") or {}).get("q")})
        code = p.get("jsCode") or ""
        for m in re.finditer(r"\.slice\(\s*0\s*,\s*(\d+)\s*\)", code):
            shaping.append({"node": n["name"], "char_cap_per_field": int(m.group(1))})
    sched = schedule_info(wf, year)
    coverage = None
    q = next((s.get("query") for s in shaping if s.get("query")), None)
    win = lookback_minutes(q)
    if sched["cron"] and win:
        times = cron_run_times(sched["cron"], year)
        coverage = {"query": q, "lookback_h": win / 60, **temporal_coverage(times, win, year)}
        if coverage["uniform"] < 0.99:
            flags["yellow"].append(
                f"Look-back '{q}' with cron '{sched['cron']}' leaves {coverage['uncovered_hours_per_year']:.0f} h/yr "
                f"unread by the workflow (K = {coverage['uniform']:.1%} uniform, {coverage['business_hours']:.1%} "
                f"business hours). Fix with an 'after:' last-run timestamp, not a wider newer_than.")
    return {"name": wf.get("name"), "node_count": len(nodes), "inference_sites": sites,
            "schedule": sched, "coverage": coverage, "input_shaping": shaping,
            "flags": {k: sorted(set(v)) for k, v in flags.items()},
            "mermaid": mermaid(wf, sites)}
