import json
import math
import re
import time
import uuid
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple, Union

from .db import backfill_efficiency, connect
from .delta_heat import compute_cost_and_thermal_delta
from .ecologits_bridge import estimate_impacts
from .energy_table import lookup
from .quality_scores import composite_q, get_loader, init_loader
from .thermodynamics import MissingEnergyConstantsError, efficiency


@dataclass
class WasteEvent:
    event_id: str
    run_id: str
    event_type: str  # 'retry_chain' | 'redundant_call' | 'cache_miss' | 'output_padding'
    rule_id: str      # 'W001_RETRY_CHAIN' | 'W002_REDUNDANT_TOOL_CALL' | 'W003_CACHE_MISS_LEAK' | 'W004_EXCESSIVE_OUTPUT_PADDING'
    tokens_wasted: int
    cost_wasted_usd: float
    energy_wasted_kwh: float
    heat_wasted_dollars: float
    details: str



INSERT_MSG = """
INSERT OR REPLACE INTO messages (
  uuid, parent_uuid, session_id, project_slug, cwd, git_branch, cc_version, entrypoint,
  type, is_sidechain, agent_id, timestamp, model, stop_reason, prompt_id, message_id,
  input_tokens, output_tokens, cache_read_tokens, cache_create_5m_tokens, cache_create_1h_tokens,
  prompt_text, prompt_chars, tool_calls_json,
  energy_kwh, gwp_kgco2eq, wcf_l, adpe_kgsbeq, pe_mj, impact_source,
  e_in_j, e_out_j, w_useful_j, waste_j, q_alpha, q_rho, quality_adjusted, eta
) VALUES (
  :uuid, :parent_uuid, :session_id, :project_slug, :cwd, :git_branch, :cc_version, :entrypoint,
  :type, :is_sidechain, :agent_id, :timestamp, :model, :stop_reason, :prompt_id, :message_id,
  :input_tokens, :output_tokens, :cache_read_tokens, :cache_create_5m_tokens, :cache_create_1h_tokens,
  :prompt_text, :prompt_chars, :tool_calls_json,
  :energy_kwh, :gwp_kgco2eq, :wcf_l, :adpe_kgsbeq, :pe_mj, :impact_source,
  :e_in_j, :e_out_j, :w_useful_j, :waste_j, :q_alpha, :q_rho, :quality_adjusted, :eta
)
"""

INSERT_TOOL = """
INSERT INTO tool_calls (message_uuid, session_id, project_slug, tool_name, target, result_tokens, is_error, timestamp)
VALUES (:message_uuid, :session_id, :project_slug, :tool_name, :target, :result_tokens, :is_error, :timestamp)
"""


_TARGET_FIELDS = {
    "Read":      "file_path",
    "Edit":      "file_path",
    "Write":     "file_path",
    "Glob":      "pattern",
    "Grep":      "pattern",
    "Bash":      "command",
    "WebFetch":  "url",
    "WebSearch": "query",
    "Task":      "subagent_type",
    "Skill":     "skill",
}


def _usage(rec: dict) -> dict:
    u = (rec.get("message") or {}).get("usage") or {}
    cc = u.get("cache_creation") or {}
    return {
        "input_tokens":           int(u.get("input_tokens") or 0),
        "output_tokens":          int(u.get("output_tokens") or 0),
        "cache_read_tokens":      int(u.get("cache_read_input_tokens") or 0),
        "cache_create_5m_tokens": int(cc.get("ephemeral_5m_input_tokens") or 0),
        "cache_create_1h_tokens": int(cc.get("ephemeral_1h_input_tokens") or 0),
    }


def _prompt_text(rec: dict) -> Tuple[Optional[str], Optional[int]]:
    if rec.get("type") != "user":
        return None, None
    content = (rec.get("message") or {}).get("content")
    if isinstance(content, str):
        return content, len(content)
    if isinstance(content, list):
        parts = [b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"]
        text = "".join(parts) if parts else None
        return text, (len(text) if text else None)
    return None, None


def _target(name: str, inp: dict) -> Optional[str]:
    field = _TARGET_FIELDS.get(name)
    if field and isinstance(inp, dict):
        v = inp.get(field)
        if isinstance(v, str):
            return v[:500]
    return None


def _extract_tools(rec: dict) -> List[dict]:
    out = []
    content = (rec.get("message") or {}).get("content")
    if not isinstance(content, list):
        return out
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "tool_use":
            continue
        name = block.get("name") or "unknown"
        target = _target(name, block.get("input") or {})
        out.append({
            "tool_name":     name,
            "target":        target,
            "result_tokens": None,
            "is_error":      0,
            "timestamp":     rec.get("timestamp"),
        })
    return out


def _extract_results(rec: dict) -> List[dict]:
    out = []
    content = (rec.get("message") or {}).get("content")
    if not isinstance(content, list):
        return out
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "tool_result":
            continue
        body = block.get("content")
        if isinstance(body, str):
            chars = len(body)
        elif isinstance(body, list):
            chars = sum(len(p.get("text", "")) for p in body if isinstance(p, dict))
        else:
            chars = 0
        out.append({
            "tool_name":     "_tool_result",
            "target":        block.get("tool_use_id"),
            "result_tokens": chars // 4,
            "is_error":      1 if block.get("is_error") else 0,
            "timestamp":     rec.get("timestamp"),
        })
    return out


def parse_record(rec: dict, project_slug: str) -> Tuple[dict, List[dict]]:
    """Return (message_row, [tool_call_rows])."""
    msg_obj = rec.get("message") or {}
    text, chars = _prompt_text(rec)
    msg = {
        "uuid":         rec.get("uuid"),
        "parent_uuid":  rec.get("parentUuid"),
        "session_id":   rec.get("sessionId"),
        "project_slug": project_slug,
        "cwd":          rec.get("cwd"),
        "git_branch":   rec.get("gitBranch"),
        "cc_version":   rec.get("version"),
        "entrypoint":   rec.get("entrypoint"),
        "type":         rec.get("type"),
        "is_sidechain": 1 if rec.get("isSidechain") else 0,
        "agent_id":     rec.get("agentId"),
        "timestamp":    rec.get("timestamp"),
        "model":        msg_obj.get("model"),
        "stop_reason":  msg_obj.get("stop_reason"),
        "prompt_id":    rec.get("promptId"),
        "message_id":   msg_obj.get("id"),
        "prompt_text":  text,
        "prompt_chars": chars,
        "tool_calls_json": None,
        **_usage(rec),
    }
    impact = estimate_impacts(
        model=msg["model"],
        input_tokens=msg["input_tokens"],
        output_tokens=msg["output_tokens"],
        cache_read_tokens=msg["cache_read_tokens"],
        cache_create_tokens=msg["cache_create_5m_tokens"] + msg["cache_create_1h_tokens"],
        electricity_zone="USA",
    )
    msg["energy_kwh"] = impact.energy_kwh
    msg["gwp_kgco2eq"] = impact.gwp_kgco2eq
    msg["wcf_l"] = impact.wcf_l
    msg["adpe_kgsbeq"] = impact.adpe_kgsbeq
    msg["pe_mj"] = impact.pe_mj
    msg["impact_source"] = impact.source

    msg["e_in_j"] = None
    msg["e_out_j"] = None
    msg["w_useful_j"] = None
    msg["waste_j"] = None
    msg["q_alpha"] = None
    msg["q_rho"] = None
    msg["quality_adjusted"] = 0
    msg["eta"] = None

    if msg["type"] == "assistant" and msg["model"]:
        try:
            constants = lookup(msg["model"])
            loader = get_loader()
            score = loader.lookup(msg["message_id"], msg["session_id"]) if loader else None
            q = composite_q(score) if score else None
            report = efficiency(msg["input_tokens"], msg["output_tokens"], constants, q)
            msg["e_in_j"] = report.e_in
            msg["e_out_j"] = report.e_out
            msg["w_useful_j"] = report.w_useful
            msg["waste_j"] = report.waste_joules
            msg["q_alpha"] = score.alpha if score else None
            msg["q_rho"] = score.rho if score else None
            msg["quality_adjusted"] = 1 if report.quality_adjusted else 0
            msg["eta"] = report.eta
        except (MissingEnergyConstantsError, Exception):
            pass

    tools = _extract_tools(rec)
    tools.extend(_extract_results(rec))
    if tools:
        msg["tool_calls_json"] = json.dumps(
            [{"name": t["tool_name"], "target": t["target"]} for t in tools if t["tool_name"] != "_tool_result"]
        )
    for t in tools:
        t["message_uuid"] = msg["uuid"]
        t["session_id"]   = msg["session_id"]
        t["project_slug"] = project_slug
    return msg, tools


def _project_slug(file_path: Path, projects_root: Path) -> str:
    rel = file_path.relative_to(projects_root)
    return rel.parts[0]


def _evict_prior_snapshots(conn, session_id: str, message_id: str, keep_uuid: str) -> None:
    """Remove older streaming snapshots for the same (session_id, message_id).

    Claude Code writes 2–3 JSONL lines per assistant response (partial → final)
    with identical message.id but distinct top-level uuids. Only the final
    tally matches billing, so earlier snapshots must be replaced, not summed.
    """
    old = [r[0] for r in conn.execute(
        "SELECT uuid FROM messages WHERE session_id=? AND message_id=? AND uuid!=?",
        (session_id, message_id, keep_uuid),
    )]
    if not old:
        return
    placeholders = ",".join("?" * len(old))
    conn.execute(f"DELETE FROM tool_calls WHERE message_uuid IN ({placeholders})", old)
    conn.execute(f"DELETE FROM messages WHERE uuid IN ({placeholders})", old)


def scan_file(path: Path, project_slug: str, conn, start_byte: int = 0) -> dict:
    """Ingest new lines from a JSONL file starting at ``start_byte``.

    Returns message/tool counts plus ``end_offset`` — the byte offset just
    past the last fully-parsed line. Callers persist ``end_offset`` as the
    file's high-water mark so a line partially flushed at EOF gets re-read
    once it completes.
    """
    msgs = tools = 0
    end_offset = start_byte
    with open(path, "rb") as fb:
        if start_byte:
            fb.seek(start_byte)
        while True:
            raw = fb.readline()
            if not raw:
                break  # EOF
            if not raw.endswith(b"\n"):
                # Partial line — Claude Code is mid-flush. Leave the
                # high-water mark behind the line start so we re-read it
                # once the write completes.
                break
            line_end = fb.tell()
            try:
                line = raw.decode("utf-8", errors="replace").strip()
            except Exception:
                end_offset = line_end
                continue
            if not line:
                end_offset = line_end
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                end_offset = line_end
                continue
            if not isinstance(rec, dict) or "uuid" not in rec or "type" not in rec:
                end_offset = line_end
                continue
            msg, tlist = parse_record(rec, project_slug)
            if not msg["session_id"] or not msg["timestamp"]:
                end_offset = line_end
                continue
            if msg["message_id"]:
                _evict_prior_snapshots(conn, msg["session_id"], msg["message_id"], msg["uuid"])
            conn.execute(INSERT_MSG, msg)
            # tool_calls has no natural unique key; clear any prior rows for
            # this uuid so full rescans stay idempotent instead of
            # duplicating rows.
            conn.execute("DELETE FROM tool_calls WHERE message_uuid=?", (msg["uuid"],))
            for t in tlist:
                conn.execute(INSERT_TOOL, t)
                tools += 1
            msgs += 1
            end_offset = line_end
    return {"messages": msgs, "tools": tools, "end_offset": end_offset}


def scan_dir(
    projects_root: Union[str, Path],
    db_path: Union[str, Path],
    recompute: bool = False,
) -> dict:
    root = Path(projects_root)
    totals = {"messages": 0, "tools": 0, "files": 0}
    init_loader(json_path=Path("quality_scores.json"), db_path=db_path)
    if not root.is_dir():
        if recompute:
            with connect(db_path) as conn:
                backfill_efficiency(conn, get_loader(), force_all=True)
        return totals
    with connect(db_path) as conn:
        for p in root.rglob("*.jsonl"):
            try:
                stat = p.stat()
            except OSError:
                continue
            row = conn.execute(
                "SELECT mtime, bytes_read FROM files WHERE path=?", (str(p),)
            ).fetchone()
            offset = 0
            if row and row["mtime"] == stat.st_mtime and row["bytes_read"] == stat.st_size:
                continue
            if row and stat.st_size > row["bytes_read"]:
                offset = row["bytes_read"]
            slug = _project_slug(p, root)
            sub = scan_file(p, slug, conn, start_byte=offset)
            # Persist the byte offset of the last fully-parsed line (not
            # st_size) so a partial line mid-flush is retried on the next
            # scan instead of being skipped over.
            conn.execute(
                "INSERT OR REPLACE INTO files (path, mtime, bytes_read, scanned_at) VALUES (?, ?, ?, ?)",
                (str(p), stat.st_mtime, sub["end_offset"], time.time()),
            )
            totals["messages"] += sub["messages"]
            totals["tools"]    += sub["tools"]
            totals["files"]    += 1
        if recompute:
            backfill_efficiency(conn, get_loader(), force_all=True)
        conn.commit()
    return totals


def cosine_similarity(text1: str, text2: str) -> float:
    """Compute cosine similarity between two text snippets using character 3-grams

    (with word-level fallback) to robustly capture prompt thrashing and retry loops.
    """
    if not text1 or not text2:
        return 0.0
    t1 = text1.strip().lower()
    t2 = text2.strip().lower()
    if t1 == t2:
        return 1.0
    grams1 = [t1[i:i+3] for i in range(len(t1) - 2)]
    grams2 = [t2[i:i+3] for i in range(len(t2) - 2)]
    if not grams1 or not grams2:
        grams1 = re.findall(r"\w+", t1)
        grams2 = re.findall(r"\w+", t2)
    if not grams1 or not grams2:
        return 0.0
    v1 = Counter(grams1)
    v2 = Counter(grams2)
    common = set(v1.keys()) & set(v2.keys())
    numerator = sum(v1[t] * v2[t] for t in common)
    denom1 = sum(v1[t] ** 2 for t in v1)
    denom2 = sum(v2[t] ** 2 for t in v2)
    if not denom1 or not denom2:
        return 0.0
    return float(numerator) / (math.sqrt(denom1) * math.sqrt(denom2))


def eval_w001_retry_chain(
    turns: list[dict],
    run_id: str = "",
    model: str = "claude-3-5-sonnet",
    region_key: str = "quebec_commercial_hydro",
) -> list[WasteEvent]:
    """Rule W001_RETRY_CHAIN: Detects retry loops/prompt thrashing when cosine

    similarity between consecutive user prompts > 0.92. Flags 100% of prior
    failed turns as wasted tokens, cost, and energy.
    """
    events = []
    user_turns = [t for t in turns if t.get("type") == "user" and t.get("prompt_text")]
    for i in range(1, len(user_turns)):
        prev_turn = user_turns[i - 1]
        curr_turn = user_turns[i]
        sim = cosine_similarity(prev_turn.get("prompt_text", ""), curr_turn.get("prompt_text", ""))
        if sim > 0.92:
            w_in = int(prev_turn.get("input_tokens") or 0)
            w_out = int(prev_turn.get("output_tokens") or 0)
            p_uuid = prev_turn.get("uuid")
            for t in turns:
                if t.get("parent_uuid") == p_uuid and t.get("type") == "assistant":
                    w_in += int(t.get("input_tokens") or 0)
                    w_out += int(t.get("output_tokens") or 0)
            if w_in == 0 and w_out == 0:
                chars = len(prev_turn.get("prompt_text") or "")
                w_in = max(10, chars // 4)

            m_name = curr_turn.get("model") or prev_turn.get("model") or model
            delta = compute_cost_and_thermal_delta(
                model=m_name,
                total_in=w_in,
                total_out=w_out,
                useful_in=0,
                useful_out=0,
                region_key=region_key,
            )
            events.append(WasteEvent(
                event_id=str(uuid.uuid4()),
                run_id=run_id or str(curr_turn.get("session_id") or ""),
                event_type="retry_chain",
                rule_id="W001_RETRY_CHAIN",
                tokens_wasted=w_in + w_out,
                cost_wasted_usd=delta.cost_wasted_usd,
                energy_wasted_kwh=delta.energy_wasted_kwh,
                heat_wasted_dollars=delta.heat_dollars_wasted,
                details=f"Cosine similarity {sim:.3f} > 0.92 detected between consecutive prompts.",
            ))
    return events


def eval_w002_redundant_tool_call(
    tool_calls: list[dict],
    run_id: str = "",
    model: str = "claude-3-5-sonnet",
    region_key: str = "quebec_commercial_hydro",
) -> list[WasteEvent]:
    """Rule W002_REDUNDANT_TOOL_CALL: Detects duplicate tool calls within the

    same turn/session with matching name, arguments, and outputs. Flags subsequent
    calls as execution waste.
    """
    events = []
    seen = {}
    for t in tool_calls:
        name = t.get("tool_name")
        if not name or name == "_tool_result":
            continue
        target = str(t.get("target") or "")
        key = (name, target)
        if key in seen:
            res_tokens = int(t.get("result_tokens") or 50)
            delta = compute_cost_and_thermal_delta(
                model=model,
                total_in=res_tokens,
                total_out=0,
                useful_in=0,
                useful_out=0,
                region_key=region_key,
            )
            events.append(WasteEvent(
                event_id=str(uuid.uuid4()),
                run_id=run_id or str(t.get("session_id") or ""),
                event_type="redundant_call",
                rule_id="W002_REDUNDANT_TOOL_CALL",
                tokens_wasted=res_tokens,
                cost_wasted_usd=delta.cost_wasted_usd,
                energy_wasted_kwh=delta.energy_wasted_kwh,
                heat_wasted_dollars=delta.heat_dollars_wasted,
                details=f"Redundant tool call '{name}' with target '{target[:100]}'.",
            ))
        else:
            seen[key] = t
    return events


def eval_w003_cache_miss_leak(
    turns: list[dict],
    run_id: str = "",
    model: str = "claude-3-5-sonnet",
    region_key: str = "quebec_commercial_hydro",
) -> list[WasteEvent]:
    """Rule W003_CACHE_MISS_LEAK: Identifies repeated static system prompts (> 2,000 tokens)

    transmitted without prompt-caching headers (cache_read=0 and cache_create=0).
    """
    events = []
    for i, t in enumerate(turns):
        if i == 0:
            continue
        in_tok = int(t.get("input_tokens") or 0)
        c_read = int(t.get("cache_read_tokens") or 0)
        c_create = int(t.get("cache_create_5m_tokens") or 0) + int(t.get("cache_create_1h_tokens") or 0)
        if in_tok > 2000 and c_read == 0 and c_create == 0:
            wasted_tok = in_tok - 500  # baseline turns should have cached
            m_name = t.get("model") or model
            delta = compute_cost_and_thermal_delta(
                model=m_name,
                total_in=wasted_tok,
                total_out=0,
                useful_in=0,
                useful_out=0,
                region_key=region_key,
            )
            events.append(WasteEvent(
                event_id=str(uuid.uuid4()),
                run_id=run_id or str(t.get("session_id") or ""),
                event_type="cache_miss",
                rule_id="W003_CACHE_MISS_LEAK",
                tokens_wasted=wasted_tok,
                cost_wasted_usd=delta.cost_wasted_usd,
                energy_wasted_kwh=delta.energy_wasted_kwh,
                heat_wasted_dollars=delta.heat_dollars_wasted,
                details=f"Static prompt > 2,000 tokens ({in_tok} tokens) sent on turn {i+1} without prompt-caching headers.",
            ))
    return events


def eval_w004_excessive_output_padding(
    turns: list[dict],
    run_id: str = "",
    model: str = "claude-3-5-sonnet",
    region_key: str = "quebec_commercial_hydro",
) -> list[WasteEvent]:
    """Rule W004_EXCESSIVE_OUTPUT_PADDING: Detects structured tasks returning conversational

    boilerplate > 4x the requested payload size.
    """
    events = []
    for i in range(len(turns)):
        curr = turns[i]
        if curr.get("type") == "assistant":
            prompt_text = ""
            for j in range(i - 1, -1, -1):
                if turns[j].get("type") == "user":
                    prompt_text = (turns[j].get("prompt_text") or "").lower()
                    break
            structured_keywords = ["json", "yaml", "boolean", "true/false", "only return", "id only", "concise status"]
            if any(kw in prompt_text for kw in structured_keywords):
                out_tokens = int(curr.get("output_tokens") or 0)
                expected = 50  # expected concise structured payload size
                if out_tokens > 4 * expected:
                    wasted_out = out_tokens - expected
                    m_name = curr.get("model") or model
                    delta = compute_cost_and_thermal_delta(
                        model=m_name,
                        total_in=0,
                        total_out=wasted_out,
                        useful_in=0,
                        useful_out=0,
                        region_key=region_key,
                    )
                    events.append(WasteEvent(
                        event_id=str(uuid.uuid4()),
                        run_id=run_id or str(curr.get("session_id") or ""),
                        event_type="output_padding",
                        rule_id="W004_EXCESSIVE_OUTPUT_PADDING",
                        tokens_wasted=wasted_out,
                        cost_wasted_usd=delta.cost_wasted_usd,
                        energy_wasted_kwh=delta.energy_wasted_kwh,
                        heat_wasted_dollars=delta.heat_dollars_wasted,
                        details=f"Structured query returned {out_tokens} tokens (> 4x expected payload {expected} tokens).",
                    ))
    return events


def scan_waste_rules(
    turns: list[dict],
    tool_calls: Optional[list[dict]] = None,
    run_id: str = "",
    model: str = "claude-3-5-sonnet",
    region_key: str = "quebec_commercial_hydro",
) -> list[WasteEvent]:
    """Run all waste attribution rules W001-W004 and return combined waste events."""
    events = []
    events.extend(eval_w001_retry_chain(turns, run_id=run_id, model=model, region_key=region_key))
    if tool_calls:
        events.extend(eval_w002_redundant_tool_call(tool_calls, run_id=run_id, model=model, region_key=region_key))
    events.extend(eval_w003_cache_miss_leak(turns, run_id=run_id, model=model, region_key=region_key))
    events.extend(eval_w004_excessive_output_padding(turns, run_id=run_id, model=model, region_key=region_key))
    return events

