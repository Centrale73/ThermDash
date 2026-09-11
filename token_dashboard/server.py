"""HTTP server: static frontend + JSON endpoints + SSE diff stream."""
from __future__ import annotations

import http.server
import json
import mimetypes
import queue
import threading
import time
from dataclasses import asdict
from pathlib import Path
from urllib.parse import urlparse, parse_qs

from .db import (
    overview_totals, expensive_prompts, project_summary,
    tool_token_breakdown, recent_sessions, session_turns,
    daily_token_breakdown, model_breakdown, skill_breakdown,
    impact_totals, daily_impacts, model_impact_breakdown,
    get_audit_summary, get_audit_waste_events, get_audit_runs,
    insert_audit_run, insert_audit_event,
    efficiency_overview, efficiency_by_model, efficiency_sessions, efficiency_by_day,
    connect,
)
from .pricing import load_pricing, cost_for, get_plan, set_plan
from .tips import all_tips, dismiss_tip
from .scanner import scan_dir
from .skills import cached_catalog
from .ecologits_bridge import is_ecologits_available, get_grid_intensity, calculate_energy
from .energy_table import lookup
from .quality_scores import QualityScore, composite_q
from .heat import calculate_heat_dollars
from .delta_heat import compute_cost_and_thermal_delta
from .chat import handle_chat_request, get_configured_keys, test_api_key


WEB_ROOT = Path(__file__).resolve().parent.parent / "web"
PRICING_JSON = Path(__file__).resolve().parent.parent / "pricing.json"

EVENTS: "queue.Queue[dict]" = queue.Queue()

MAX_POST_BYTES = 1_000_000  # 1 MB — we only accept tiny JSON bodies (plan, tip key)
MAX_LIMIT = 1000


def _send_json(handler, obj, status: int = 200) -> None:
    body = json.dumps(obj, default=str).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(body)


def _send_error(handler, status: int, msg: str) -> None:
    _send_json(handler, {"error": msg}, status=status)


def _clamp_limit(raw, default: int) -> int:
    try:
        v = int(raw)
    except (TypeError, ValueError):
        return default
    return max(1, min(v, MAX_LIMIT))


def _serve_static(handler, rel: str) -> None:
    rel = rel.lstrip("/")
    p = (WEB_ROOT / rel).resolve()
    if not str(p).startswith(str(WEB_ROOT.resolve())) or not p.is_file():
        handler.send_response(404)
        handler.end_headers()
        return
    body = p.read_bytes()
    ctype, _ = mimetypes.guess_type(str(p))
    handler.send_response(200)
    handler.send_header("Content-Type", ctype or "application/octet-stream")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def build_handler(db_path: str, projects_dir: str):
    pricing = load_pricing(PRICING_JSON)

    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass

        def do_HEAD(self):
            return self.do_GET()

        def do_GET(self):
            url = urlparse(self.path)
            qs = parse_qs(url.query or "")
            path = url.path
            since = qs.get("since", [None])[0]
            until = qs.get("until", [None])[0]
            if path in ("/", "/index.html"):
                return _serve_static(self, "index.html")
            if path.startswith("/web/"):
                return _serve_static(self, path[5:])
            if path == "/api/overview":
                totals = overview_totals(db_path, since, until)
                cost_usd = 0.0
                for m in model_breakdown(db_path, since, until):
                    c = cost_for(m["model"], m, pricing)
                    if c["usd"] is not None:
                        cost_usd += c["usd"]
                totals["cost_usd"] = round(cost_usd, 4)
                totals["impacts"] = impact_totals(db_path, since, until)
                return _send_json(self, totals)
            if path == "/api/prompts":
                limit = _clamp_limit(qs.get("limit", ["50"])[0], 50)
                sort = qs.get("sort", ["tokens"])[0]
                rows = expensive_prompts(db_path, limit=limit, sort=sort)
                for r in rows:
                    c = cost_for(r["model"], {
                        "input_tokens": 0, "output_tokens": 0,
                        "cache_read_tokens": r["cache_read_tokens"],
                        "cache_create_5m_tokens": 0, "cache_create_1h_tokens": 0,
                    }, pricing)
                    r["estimated_cost_usd"] = c["usd"]
                return _send_json(self, rows)
            if path == "/api/projects":
                return _send_json(self, project_summary(db_path, since, until))
            if path == "/api/tools":
                return _send_json(self, tool_token_breakdown(db_path, since, until))
            if path == "/api/sessions":
                return _send_json(self, recent_sessions(
                    db_path, limit=_clamp_limit(qs.get("limit", ["20"])[0], 20),
                    since=since, until=until,
                ))
            if path == "/api/daily":
                return _send_json(self, daily_token_breakdown(db_path, since, until))
            if path == "/api/skills":
                rows = skill_breakdown(db_path, since, until)
                catalog = cached_catalog()
                for r in rows:
                    info = catalog.get(r["skill"])
                    r["tokens_per_call"] = info["tokens"] if info else None
                return _send_json(self, rows)
            if path == "/api/by-model":
                rows = model_breakdown(db_path, since, until)
                for r in rows:
                    c = cost_for(r["model"], r, pricing)
                    r["cost_usd"] = c["usd"]
                    r["cost_estimated"] = c["estimated"]
                return _send_json(self, rows)
            if path.startswith("/api/sessions/"):
                sid = path.rsplit("/", 1)[1]
                return _send_json(self, session_turns(db_path, sid))
            if path == "/api/tips":
                return _send_json(self, all_tips(db_path))
            if path == "/api/impacts":
                totals = impact_totals(db_path, since, until)
                return _send_json(self, {
                    **totals,
                    "ecologits_available": is_ecologits_available(),
                    "grid_intensity": get_grid_intensity("USA"),
                    "grid_zone": "USA",
                })
            if path == "/api/impacts/daily":
                days = int(qs.get("days")[0]) if "days" in qs else None
                return _send_json(self, daily_impacts(db_path, days=days, since=since, until=until))
            if path == "/api/impacts/models":
                return _send_json(self, model_impact_breakdown(db_path, since=since, until=until))
            if path == "/api/plan":
                return _send_json(self, {"plan": get_plan(db_path), "pricing": pricing})
            if path == "/api/keys/status":
                return _send_json(self, get_configured_keys())
            if path == "/api/audit/summary":
                return _send_json(self, get_audit_summary(db_path, since=since, until=until))
            if path == "/api/audit/waste-events":
                run_id = qs.get("run_id", [None])[0]
                rule_id = qs.get("rule_id", [None])[0]
                event_type = qs.get("event_type", [None])[0]
                limit = _clamp_limit(qs.get("limit", ["50"])[0], 50)
                return _send_json(self, get_audit_waste_events(
                    db_path, run_id=run_id, rule_id=rule_id, event_type=event_type, limit=limit
                ))
            if path == "/api/audit/runs":
                sid = qs.get("session_id", [None])[0]
                limit = _clamp_limit(qs.get("limit", ["50"])[0], 50)
                return _send_json(self, get_audit_runs(db_path, session_id=sid, limit=limit, since=since, until=until))
            if path == "/api/efficiency/overview":
                return _send_json(self, efficiency_overview(db_path, since=since, until=until))
            if path == "/api/efficiency/by_model":
                rows = efficiency_by_model(db_path, since=since, until=until)
                for r in rows:
                    try:
                        c = lookup(r["model"])
                        r["energy_source"] = c.source
                        r["energy_measured"] = c.measured
                        r["gpu"] = c.gpu
                    except Exception:
                        r["energy_source"] = ""
                        r["energy_measured"] = False
                        r["gpu"] = ""
                return _send_json(self, rows)
            if path == "/api/efficiency/sessions":
                limit = _clamp_limit(qs.get("limit", ["20"])[0], 20)
                return _send_json(self, efficiency_sessions(db_path, limit=limit, since=since, until=until))
            if path == "/api/efficiency/by_day":
                return _send_json(self, efficiency_by_day(db_path, since=since, until=until))
            if path == "/api/scan":
                n = scan_dir(projects_dir, db_path)
                return _send_json(self, n)
            if path == "/api/stream":
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Connection", "keep-alive")
                self.end_headers()
                while True:
                    try:
                        evt = EVENTS.get(timeout=15)
                        chunk = f"data: {json.dumps(evt, default=str)}\n\n".encode()
                    except queue.Empty:
                        chunk = b": ping\n\n"
                    try:
                        self.wfile.write(chunk)
                        self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError):
                        return
            self.send_response(404)
            self.end_headers()

        def do_POST(self):
            url = urlparse(self.path)
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                return _send_error(self, 400, "invalid Content-Length")
            if length < 0 or length > MAX_POST_BYTES:
                return _send_error(self, 413, f"body too large (max {MAX_POST_BYTES} bytes)")
            try:
                body = json.loads(self.rfile.read(length) or b"{}") if length else {}
            except json.JSONDecodeError:
                return _send_error(self, 400, "invalid JSON")
            if not isinstance(body, dict):
                return _send_error(self, 400, "body must be a JSON object")
            if url.path == "/api/quality_scores":
                msg_id = body.get("message_id")
                if not msg_id or not isinstance(msg_id, str):
                    return _send_error(self, 400, "message_id is required")
                sess_id = body.get("session_id", "")
                try:
                    alpha = float(body.get("alpha"))
                    rho = float(body.get("rho"))
                except (TypeError, ValueError):
                    return _send_error(self, 400, "alpha and rho must be numbers in [0, 1]")
                if not (0.0 <= alpha <= 1.0) or not (0.0 <= rho <= 1.0):
                    return _send_error(self, 400, "alpha and rho must be between 0.0 and 1.0")
                method = str(body.get("method", "unknown"))
                try:
                    w_a = float(body.get("w_a", 0.5))
                    w_p = float(body.get("w_p", 0.5))
                except (TypeError, ValueError):
                    w_a, w_p = 0.5, 0.5

                qs_obj = QualityScore(alpha=alpha, rho=rho, method=method, w_a=w_a, w_p=w_p)
                q_val = composite_q(qs_obj)

                with connect(db_path) as conn:
                    conn.execute(
                        """
                        INSERT OR REPLACE INTO quality_scores
                        (message_id, session_id, alpha, rho, method, w_a, w_p, created_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (msg_id, sess_id, alpha, rho, method, w_a, w_p, time.time()),
                    )
                    conn.commit()

                return _send_json(self, {"ok": True, "q": q_val}, status=201)
            if url.path == "/api/plan":
                set_plan(db_path, body.get("plan", "api"))
                return _send_json(self, {"ok": True})
            if url.path == "/api/tips/dismiss":
                dismiss_tip(db_path, body.get("key", ""))
                return _send_json(self, {"ok": True})
            if url.path == "/api/chat":
                try:
                    res = handle_chat_request(db_path, body, pricing)
                    return _send_json(self, res)
                except Exception as e:
                    return _send_error(self, 500, str(e))
            if url.path == "/api/keys/test":
                try:
                    provider = body.get("provider", "")
                    api_key = body.get("api_key", "")
                    res = test_api_key(provider, api_key)
                    return _send_json(self, res)
                except Exception as e:
                    return _send_error(self, 500, str(e))
            if url.path == "/api/audit/ingest":
                try:
                    run_data = body.copy()
                    model_name = run_data.get("model") or "claude-3-5-sonnet"
                    inp_tok = int(run_data.get("input_tokens") or 0)
                    out_tok = int(run_data.get("output_tokens") or 0)
                    useful_in = int(run_data.get("useful_in", inp_tok))
                    useful_out = int(run_data.get("useful_out", out_tok))
                    p_in = run_data.get("price_per_1k_in")
                    p_out = run_data.get("price_per_1k_out")
                    region = run_data.get("region_key", "quebec_commercial_hydro")
                    custom_hw = run_data.get("custom_hardware")
                    override_pue = run_data.get("override_pue")
                    override_rate = run_data.get("override_rate")

                    energy_res = calculate_energy(
                        model=model_name,
                        input_tokens=inp_tok,
                        output_tokens=out_tok,
                        custom_hardware=custom_hw,
                    )
                    heat_res = calculate_heat_dollars(
                        energy_kwh=energy_res.energy_kwh,
                        region_key=region,
                        override_pue=override_pue,
                        override_rate=override_rate,
                    )
                    delta_res = compute_cost_and_thermal_delta(
                        model=model_name,
                        total_in=inp_tok,
                        total_out=out_tok,
                        useful_in=useful_in,
                        useful_out=useful_out,
                        price_per_1k_in=p_in,
                        price_per_1k_out=p_out,
                        region_key=region,
                        custom_hardware=custom_hw,
                        override_pue=override_pue,
                        override_rate=override_rate,
                    )

                    child_events = run_data.get("waste_events") or []
                    is_waste = 1 if (delta_res.cost_wasted_usd > 0 or child_events or run_data.get("is_waste")) else 0

                    record = {
                        "run_id":                 run_data.get("run_id"),
                        "session_id":             run_data.get("session_id") or "",
                        "client_id":              run_data.get("client_id") or "",
                        "timestamp":              run_data.get("timestamp"),
                        "model":                  model_name,
                        "input_tokens":           inp_tok,
                        "output_tokens":          out_tok,
                        "cost_usd":               delta_res.cost_billed_usd,
                        "energy_kwh":             energy_res.energy_kwh,
                        "energy_joules":          energy_res.energy_joules,
                        "energy_method":          energy_res.energy_method,
                        "benchmark_source":       energy_res.benchmark_source,
                        "hardware_assumed":       energy_res.hardware_assumed,
                        "heat_dollars":           heat_res.heat_dollars,
                        "is_waste":               is_waste,
                        "waste_cost_usd":         delta_res.cost_wasted_usd,
                        "waste_energy_kwh":       delta_res.energy_wasted_kwh,
                        "cost_billed_usd":        delta_res.cost_billed_usd,
                        "cost_useful_usd":        delta_res.cost_useful_usd,
                        "cost_delta_usd":         delta_res.cost_wasted_usd,
                        "thermal_waste_joules":   delta_res.energy_wasted_joules,
                        "thermal_waste_kwh":      delta_res.energy_wasted_kwh,
                        "thermal_waste_heat_usd": delta_res.heat_dollars_wasted,
                    }
                    persisted_run_id = insert_audit_run(db_path, record)

                    persisted_event_ids = []
                    for ev in child_events:
                        ev_dict = dict(ev)
                        ev_dict["run_id"] = persisted_run_id
                        persisted_event_ids.append(insert_audit_event(db_path, ev_dict))

                    return _send_json(self, {
                        "ok": True,
                        "run_id": persisted_run_id,
                        "event_ids": persisted_event_ids,
                        "delta": asdict(delta_res),
                        "energy": asdict(energy_res),
                        "heat": asdict(heat_res),
                    }, status=201)
                except Exception as e:
                    return _send_error(self, 500, str(e))
            self.send_response(404)
            self.end_headers()

    return H


def _scan_loop(db_path: str, projects_dir: str, interval: float = 30.0):
    while True:
        try:
            n = scan_dir(projects_dir, db_path)
            if n["messages"] > 0:
                EVENTS.put({"type": "scan", "n": n, "ts": time.time()})
        except Exception as e:
            EVENTS.put({"type": "error", "message": str(e)})
        time.sleep(interval)


def run(host: str, port: int, db_path: str, projects_dir: str):
    threading.Thread(target=_scan_loop, args=(db_path, projects_dir), daemon=True).start()
    H = build_handler(db_path, projects_dir)
    httpd = http.server.ThreadingHTTPServer((host, port), H)
    httpd.serve_forever()
