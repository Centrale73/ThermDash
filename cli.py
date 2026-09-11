"""Token Dashboard CLI entrypoint."""
from __future__ import annotations

import argparse
import os
import webbrowser
from datetime import datetime, timedelta, timezone
from pathlib import Path

from token_dashboard.db import init_db, default_db_path, overview_totals
from token_dashboard.scanner import scan_dir
from token_dashboard.tips import all_tips


def _db_path(args) -> str:
    return args.db or os.environ.get("TOKEN_DASHBOARD_DB") or str(default_db_path())


def _projects(args) -> str:
    return (
        args.projects_dir
        or os.environ.get("CLAUDE_PROJECTS_DIR")
        or str(Path.home() / ".claude" / "projects")
    )


def _today_range():
    now = datetime.now(timezone.utc)
    start = datetime(now.year, now.month, now.day, tzinfo=timezone.utc).isoformat()
    end = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    return start, end


def cmd_scan(args):
    db = _db_path(args)
    init_db(db)
    recompute = getattr(args, "recompute", False)
    n = scan_dir(_projects(args), db, recompute=recompute)
    print(f"Token Dashboard: scanned {n['files']} files, {n['messages']} messages, {n['tools']} tool calls")


def cmd_today(args):
    db = _db_path(args)
    init_db(db)
    s, e = _today_range()
    t = overview_totals(db, since=s, until=e)
    print("Token Dashboard — today")
    print(f"  sessions: {t['sessions']}    turns: {t['turns']}")
    print(f"  input:    {t['input_tokens']:>12,}    output: {t['output_tokens']:>12,}")
    print(f"  cache rd: {t['cache_read_tokens']:>12,}    cache cr: {t['cache_create_5m_tokens']+t['cache_create_1h_tokens']:>12,}")


def cmd_stats(args):
    db = _db_path(args)
    init_db(db)
    t = overview_totals(db)
    print("Token Dashboard — all time")
    print(f"  sessions: {t['sessions']}    turns: {t['turns']}")
    print(f"  input:    {t['input_tokens']:>12,}    output: {t['output_tokens']:>12,}")


def cmd_tips(args):
    db = _db_path(args)
    init_db(db)
    tips = all_tips(db)
    if not tips:
        print("Token Dashboard: no suggestions")
        return
    for tip in tips:
        print(f"[{tip['category']}] {tip['title']}")
        print(f"  {tip['body']}\n")


def cmd_audit(args):
    db = _db_path(args)
    init_db(db)
    from token_dashboard.db import get_audit_summary
    s = get_audit_summary(db)
    print("Token Dashboard — Audit & Thermal Waste Summary")
    print(f"  total runs:       {s['total_runs']:>10,}")
    print(f"  billed cost:     ${s['total_cost_billed_usd']:>10.4f}")
    print(f"  useful cost:     ${s['total_cost_useful_usd']:>10.4f}")
    print(f"  delta waste cost:${s['total_cost_delta_usd']:>10.4f}")
    print(f"  efficiency ratio: {s['waste_efficiency_ratio']*100:>10.1f}%")
    print(f"  energy footprint: {s['total_energy_kwh']:>10.6f} kWh ({s['total_energy_joules']:,.1f} J)")
    print(f"  heat liabilities:${s['total_thermal_waste_heat_usd']:>10.6f}")
    print(f"  waste runs:       {s['waste_runs_count']:>10,}")


def cmd_thermo_report(args):
    import json
    db = _db_path(args)
    init_db(db)
    from token_dashboard.db import (
        backfill_efficiency,
        connect,
        efficiency_by_model,
        efficiency_overview,
    )
    from token_dashboard.quality_scores import get_loader

    if getattr(args, "recompute", False):
        with connect(db) as conn:
            backfilled, skipped = backfill_efficiency(conn, get_loader(), force_all=True)
            print(f"Recomputed efficiency for {backfilled} rows ({skipped} skipped without constants).")

    if args.model:
        models = efficiency_by_model(db, since=args.since, until=getattr(args, "until", None))
        matched = [m for m in models if m["model"] == args.model]
        data = matched[0] if matched else {"error": f"No data for model '{args.model}'"}
    else:
        data = efficiency_overview(db, since=args.since, until=getattr(args, "until", None))

    if getattr(args, "as_json", False):
        print(json.dumps(data, indent=2))
        return

    print("Token Dashboard — Thermodynamic Efficiency Report")
    adj = data.get("avg_eta_adjusted")
    unadj = data.get("avg_eta_unadjusted")
    print(f"  eta (quality-adjusted): {f'{adj:.4f}' if adj is not None else 'None'}")
    print(f"  eta (unadjusted):       {f'{unadj:.4f}' if unadj is not None else 'None'}")
    print(f"  quality-adjusted count: {data.get('quality_adjusted_count', 0):>8,}")
    print(f"  unadjusted count:       {data.get('unadjusted_count', 0):>8,}")
    mix = data.get("mix_ratio", 0.0)
    print(f"  mix ratio:              {mix*100:>7.1f}%")
    print(f"  total energy (E_in):    {data.get('sum_e_in_j', 0.0):>12.6f} J")
    print(f"  useful work (W_useful): {data.get('sum_w_useful_j', 0.0):>12.6f} J")
    print(f"  waste:                  {data.get('sum_waste_j', 0.0):>12.6f} J")
    if data.get("quality_adjusted_count", 0) == 0:
        print("  Tip: Q=1 assumed — no quality scores configured. Supply quality_scores.json or use 'thermo score'.")


def cmd_thermo_score(args):
    import time
    db = _db_path(args)
    init_db(db)
    from token_dashboard.db import connect
    from token_dashboard.quality_scores import QualityScore, composite_q

    qs = QualityScore(
        alpha=args.alpha,
        rho=args.rho,
        method=args.method,
        w_a=args.w_a,
        w_p=args.w_p,
    )
    q_val = composite_q(qs)
    with connect(db) as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO quality_scores
            (message_id, session_id, alpha, rho, method, w_a, w_p, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                args.message_id,
                args.session_id,
                args.alpha,
                args.rho,
                args.method,
                args.w_a,
                args.w_p,
                time.time(),
            ),
        )
        conn.commit()
    print(f"Recorded quality score for {args.message_id}: Q = {q_val:.4f} (alpha={args.alpha}, rho={args.rho}, method='{args.method}')")


def cmd_dashboard(args):
    db = _db_path(args)
    init_db(db)
    if not args.no_scan:
        scan_dir(_projects(args), db)
    from token_dashboard.server import run

    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "8080"))
    url = f"http://{host}:{port}/"
    if not args.no_open:
        webbrowser.open(url)
    print(f"Token Dashboard listening on {url}")
    run(host, port, db, _projects(args))


def main():
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--db", help="SQLite path (default ~/.claude/token-dashboard.db)")
    common.add_argument("--projects-dir", help="JSONL root (default ~/.claude/projects)")

    p = argparse.ArgumentParser(prog="token-dashboard", description="Local Claude Code usage dashboard", parents=[common])
    sub = p.add_subparsers(dest="cmd", required=True)

    scan_p = sub.add_parser("scan", parents=[common])
    scan_p.add_argument("--recompute", action="store_true", help="Force recomputation of efficiency for assistant messages")
    scan_p.set_defaults(func=cmd_scan)

    sub.add_parser("today", parents=[common]).set_defaults(func=cmd_today)
    sub.add_parser("stats", parents=[common]).set_defaults(func=cmd_stats)
    sub.add_parser("tips",  parents=[common]).set_defaults(func=cmd_tips)
    sub.add_parser("audit", parents=[common]).set_defaults(func=cmd_audit)

    thermo_p = sub.add_parser("thermo", parents=[common])
    thermo_sub = thermo_p.add_subparsers(dest="thermo_cmd", required=True)

    report_p = thermo_sub.add_parser("report", parents=[common])
    report_p.add_argument("--model", help="Filter by model name")
    report_p.add_argument("--since", help="Start timestamp ISO")
    report_p.add_argument("--until", help="End timestamp ISO")
    report_p.add_argument("--json", action="store_true", dest="as_json", help="Output machine-readable JSON")
    report_p.add_argument("--recompute", action="store_true", help="Force recompute efficiency for all assistant rows")
    report_p.set_defaults(func=cmd_thermo_report)

    score_p = thermo_sub.add_parser("score", parents=[common])
    score_p.add_argument("--message-id", required=True, help="Message ID to score")
    score_p.add_argument("--session-id", default="", help="Session ID (optional)")
    score_p.add_argument("--alpha", type=float, required=True, help="Accuracy score [0, 1]")
    score_p.add_argument("--rho", type=float, required=True, help="Precision/relevance score [0, 1]")
    score_p.add_argument("--method", default="human", help="Scoring method (human, judge, etc.)")
    score_p.add_argument("--w-a", type=float, default=0.5, help="Weight for alpha")
    score_p.add_argument("--w-p", type=float, default=0.5, help="Weight for rho")
    score_p.set_defaults(func=cmd_thermo_score)

    d = sub.add_parser("dashboard", parents=[common])
    d.add_argument("--no-scan", action="store_true")
    d.add_argument("--no-open", action="store_true")
    d.set_defaults(func=cmd_dashboard)
    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
