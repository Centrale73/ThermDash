"""SQLite schema, connection, and shared query helpers."""
from __future__ import annotations

import os
import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Optional, Union

SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
  path        TEXT PRIMARY KEY,
  mtime       REAL    NOT NULL,
  bytes_read  INTEGER NOT NULL,
  scanned_at  REAL    NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
  uuid                    TEXT PRIMARY KEY,
  parent_uuid             TEXT,
  session_id              TEXT NOT NULL,
  project_slug            TEXT NOT NULL,
  cwd                     TEXT,
  git_branch              TEXT,
  cc_version              TEXT,
  entrypoint              TEXT,
  type                    TEXT NOT NULL,
  is_sidechain            INTEGER NOT NULL DEFAULT 0,
  agent_id                TEXT,
  timestamp               TEXT NOT NULL,
  model                   TEXT,
  stop_reason             TEXT,
  prompt_id               TEXT,
  message_id              TEXT,
  input_tokens            INTEGER NOT NULL DEFAULT 0,
  output_tokens           INTEGER NOT NULL DEFAULT 0,
  cache_read_tokens       INTEGER NOT NULL DEFAULT 0,
  cache_create_5m_tokens  INTEGER NOT NULL DEFAULT 0,
  cache_create_1h_tokens  INTEGER NOT NULL DEFAULT 0,
  prompt_text             TEXT,
  prompt_chars            INTEGER,
  tool_calls_json         TEXT,
  energy_kwh              REAL NOT NULL DEFAULT 0,
  gwp_kgco2eq             REAL NOT NULL DEFAULT 0,
  wcf_l                   REAL NOT NULL DEFAULT 0,
  adpe_kgsbeq             REAL NOT NULL DEFAULT 0,
  pe_mj                   REAL NOT NULL DEFAULT 0,
  impact_source           TEXT NOT NULL DEFAULT '',
  e_in_j                  REAL,
  e_out_j                 REAL,
  w_useful_j              REAL,
  waste_j                 REAL,
  q_alpha                 REAL,
  q_rho                   REAL,
  quality_adjusted        INTEGER NOT NULL DEFAULT 0,
  eta                     REAL
);
CREATE INDEX IF NOT EXISTS idx_messages_session   ON messages(session_id);
CREATE INDEX IF NOT EXISTS idx_messages_project   ON messages(project_slug);
CREATE INDEX IF NOT EXISTS idx_messages_timestamp ON messages(timestamp);
CREATE INDEX IF NOT EXISTS idx_messages_model     ON messages(model);
CREATE INDEX IF NOT EXISTS idx_messages_msgid     ON messages(session_id, message_id);

CREATE TABLE IF NOT EXISTS quality_scores (
  message_id  TEXT PRIMARY KEY,
  session_id  TEXT,
  alpha       REAL NOT NULL,
  rho         REAL NOT NULL,
  method      TEXT NOT NULL DEFAULT 'unknown',
  w_a         REAL NOT NULL DEFAULT 0.5,
  w_p         REAL NOT NULL DEFAULT 0.5,
  created_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_qs_session ON quality_scores(session_id);

CREATE TABLE IF NOT EXISTS tool_calls (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  message_uuid  TEXT    NOT NULL,
  session_id    TEXT    NOT NULL,
  project_slug  TEXT    NOT NULL,
  tool_name     TEXT    NOT NULL,
  target        TEXT,
  result_tokens INTEGER,
  is_error      INTEGER NOT NULL DEFAULT 0,
  timestamp     TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tools_session ON tool_calls(session_id);
CREATE INDEX IF NOT EXISTS idx_tools_name    ON tool_calls(tool_name);
CREATE INDEX IF NOT EXISTS idx_tools_target  ON tool_calls(target);

CREATE TABLE IF NOT EXISTS plan (
  k TEXT PRIMARY KEY,
  v TEXT
);

CREATE TABLE IF NOT EXISTS dismissed_tips (
  tip_key       TEXT PRIMARY KEY,
  dismissed_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_runs (
  run_id                 TEXT PRIMARY KEY,
  session_id             TEXT,
  client_id              TEXT,
  timestamp              TEXT NOT NULL,
  model                  TEXT,
  input_tokens           INTEGER NOT NULL DEFAULT 0,
  output_tokens          INTEGER NOT NULL DEFAULT 0,
  cost_usd               REAL NOT NULL DEFAULT 0.0,
  energy_kwh             REAL NOT NULL DEFAULT 0.0,
  energy_joules          REAL NOT NULL DEFAULT 0.0,
  energy_method          TEXT NOT NULL DEFAULT 'conservative_fallback',
  benchmark_source       TEXT DEFAULT '',
  hardware_assumed       TEXT DEFAULT '',
  heat_dollars           REAL NOT NULL DEFAULT 0.0,
  is_waste               INTEGER NOT NULL DEFAULT 0,
  waste_cost_usd         REAL NOT NULL DEFAULT 0.0,
  waste_energy_kwh       REAL NOT NULL DEFAULT 0.0,
  cost_billed_usd        REAL NOT NULL DEFAULT 0.0,
  cost_useful_usd        REAL NOT NULL DEFAULT 0.0,
  cost_delta_usd         REAL NOT NULL DEFAULT 0.0,
  thermal_waste_joules   REAL NOT NULL DEFAULT 0.0,
  thermal_waste_kwh      REAL NOT NULL DEFAULT 0.0,
  thermal_waste_heat_usd REAL NOT NULL DEFAULT 0.0
);
CREATE INDEX IF NOT EXISTS idx_audit_runs_session   ON audit_runs(session_id);
CREATE INDEX IF NOT EXISTS idx_audit_runs_timestamp ON audit_runs(timestamp);
CREATE INDEX IF NOT EXISTS idx_audit_runs_waste     ON audit_runs(is_waste);

CREATE TABLE IF NOT EXISTS audit_events (
  event_id               TEXT PRIMARY KEY,
  run_id                 TEXT NOT NULL,
  event_type             TEXT NOT NULL,
  tokens_wasted          INTEGER NOT NULL DEFAULT 0,
  cost_wasted_usd        REAL NOT NULL DEFAULT 0.0,
  energy_wasted_kwh      REAL NOT NULL DEFAULT 0.0,
  heat_wasted_dollars    REAL NOT NULL DEFAULT 0.0,
  rule_id                TEXT NOT NULL,
  details                TEXT DEFAULT '',
  FOREIGN KEY (run_id) REFERENCES audit_runs(run_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_audit_events_run  ON audit_events(run_id);
CREATE INDEX IF NOT EXISTS idx_audit_events_rule ON audit_events(rule_id);
CREATE INDEX IF NOT EXISTS idx_audit_events_type ON audit_events(event_type);
"""


def default_db_path() -> Path:
    return Path.home() / ".claude" / "token-dashboard.db"


def init_db(path: Union[str, Path]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as c:
        _migrate_add_message_id(c)
        _migrate_impact_columns(c)
        _migrate_efficiency_columns(c)
        _migrate_audit_tables(c)
        c.executescript(SCHEMA)
        try:
            from .quality_scores import QualityScoreLoader
            loader = QualityScoreLoader(db_path=path)
            backfill_efficiency(c, loader)
        except Exception:
            pass


def _migrate_efficiency_columns(conn) -> None:
    """Add thermodynamic efficiency columns if they do not exist."""
    has_table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='messages'"
    ).fetchone()
    if not has_table:
        return
    cols = {row[1] for row in conn.execute("PRAGMA table_info(messages)")}
    new_columns = [
        ("e_in_j", "REAL"),
        ("e_out_j", "REAL"),
        ("w_useful_j", "REAL"),
        ("waste_j", "REAL"),
        ("q_alpha", "REAL"),
        ("q_rho", "REAL"),
        ("quality_adjusted", "INTEGER NOT NULL DEFAULT 0"),
        ("eta", "REAL"),
    ]
    for col_name, col_type in new_columns:
        if col_name not in cols:
            conn.execute(f"ALTER TABLE messages ADD COLUMN {col_name} {col_type}")
    conn.commit()


def _migrate_audit_tables(conn) -> None:
    """Ensure all required columns exist in audit_runs and audit_events."""
    has_runs = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='audit_runs'"
    ).fetchone()
    if has_runs:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(audit_runs)")}
        run_cols = [
            ("model", "TEXT DEFAULT ''"),
            ("cost_billed_usd", "REAL DEFAULT 0.0"),
            ("cost_useful_usd", "REAL DEFAULT 0.0"),
            ("cost_delta_usd", "REAL DEFAULT 0.0"),
            ("thermal_waste_joules", "REAL DEFAULT 0.0"),
            ("thermal_waste_kwh", "REAL DEFAULT 0.0"),
            ("thermal_waste_heat_usd", "REAL DEFAULT 0.0"),
        ]
        for col_name, col_type in run_cols:
            if col_name not in cols:
                conn.execute(f"ALTER TABLE audit_runs ADD COLUMN {col_name} {col_type}")
    conn.commit()


def _migrate_impact_columns(conn) -> None:
    """Add environmental impact columns if they do not exist."""
    has_table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='messages'"
    ).fetchone()
    if not has_table:
        return
    cols = {row[1] for row in conn.execute("PRAGMA table_info(messages)")}
    new_columns = [
        ("energy_kwh", "REAL DEFAULT 0"),
        ("gwp_kgco2eq", "REAL DEFAULT 0"),
        ("wcf_l", "REAL DEFAULT 0"),
        ("adpe_kgsbeq", "REAL DEFAULT 0"),
        ("pe_mj", "REAL DEFAULT 0"),
        ("impact_source", "TEXT DEFAULT ''"),
    ]
    for col_name, col_type in new_columns:
        if col_name not in cols:
            conn.execute(f"ALTER TABLE messages ADD COLUMN {col_name} {col_type}")
    conn.commit()


def _migrate_add_message_id(conn) -> None:
    """Add messages.message_id for streaming-snapshot dedup.

    Why: pre-migration rows were summed from all streaming snapshots (over-count).
    How to apply: if the old table exists without the column, add it and clear
    messages/tool_calls/files so the next scan replays JSONLs cleanly. Source
    of truth is on disk; rescanning is cheap.
    """
    has_table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='messages'"
    ).fetchone()
    if not has_table:
        return
    cols = {row[1] for row in conn.execute("PRAGMA table_info(messages)")}
    if "message_id" in cols:
        return
    conn.execute("ALTER TABLE messages ADD COLUMN message_id TEXT")
    conn.execute("DELETE FROM messages")
    conn.execute("DELETE FROM tool_calls")
    conn.execute("DELETE FROM files")
    conn.commit()


@contextmanager
def connect(path: Union[str, Path]):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
    finally:
        conn.close()


def _range_clause(since, until, col: str = "timestamp"):
    where, args = [], []
    if since:
        where.append(f"{col} >= ?"); args.append(since)
    if until:
        where.append(f"{col} < ?"); args.append(until)
    return ((" AND " + " AND ".join(where)) if where else "", args)


def _encode_slug(path: str) -> str:
    """Claude Code's project-slug encoding: each of `:`, `\\`, `/`, space → one `-`."""
    return re.sub(r"[:\\/ ]", "-", path)


def _walk_to_root(cwd: str, slug: str) -> Optional[str]:
    """If any ancestor of cwd encodes to slug, return that ancestor's basename."""
    if not cwd or not slug:
        return None
    trimmed = cwd.rstrip("/\\")
    sep = "\\" if "\\" in trimmed else "/"
    parts = trimmed.split(sep)
    for i in range(len(parts), 0, -1):
        if _encode_slug(sep.join(parts[:i])) == slug:
            name = parts[i - 1]
            if name:
                return name
    return None


def project_name_for(cwd: Optional[str], fallback_slug: str) -> str:
    """Pretty project name from a single cwd + slug (best-effort).

    For the multi-cwd case, prefer `best_project_name`.
    """
    name = _walk_to_root(cwd or "", fallback_slug or "")
    if name:
        return name
    if cwd:
        trimmed = cwd.rstrip("/\\")
        sep = "\\" if "\\" in trimmed else "/"
        tail = trimmed.split(sep)[-1]
        if tail:
            return tail
    if fallback_slug:
        parts = [p for p in re.split(r"-+", fallback_slug) if p]
        if parts:
            return parts[-1]
    return fallback_slug or ""


def best_project_name(cwds, slug: str) -> str:
    """Pick a pretty name from a list of cwds.

    Prefer a cwd whose walk-up matches `slug` (a true descendant of the project
    root). If none match, fall back to `project_name_for` on the first cwd,
    then to the slug's last segment.
    """
    cwds = [c for c in (cwds or []) if c]
    for cwd in cwds:
        name = _walk_to_root(cwd, slug)
        if name:
            return name
    return project_name_for(cwds[0] if cwds else None, slug)


def overview_totals(db_path, since=None, until=None) -> dict:
    rng, args = _range_clause(since, until)
    sql = f"""
      SELECT COUNT(DISTINCT session_id) AS sessions,
             SUM(CASE WHEN type='user' THEN 1 ELSE 0 END) AS turns,
             COALESCE(SUM(input_tokens),0)            AS input_tokens,
             COALESCE(SUM(output_tokens),0)           AS output_tokens,
             COALESCE(SUM(cache_read_tokens),0)       AS cache_read_tokens,
             COALESCE(SUM(cache_create_5m_tokens),0)  AS cache_create_5m_tokens,
             COALESCE(SUM(cache_create_1h_tokens),0)  AS cache_create_1h_tokens
        FROM messages WHERE 1=1 {rng}
    """
    with connect(db_path) as c:
        return dict(c.execute(sql, args).fetchone())


def expensive_prompts(db_path, limit: int = 50, sort: str = "tokens") -> list:
    """User prompt joined with the immediately-following assistant turn's tokens.

    sort="tokens" (default) → largest billable first.
    sort="recent"           → newest first.
    """
    order = "u.timestamp DESC" if sort == "recent" else "billable_tokens DESC"
    sql = f"""
      SELECT u.uuid AS user_uuid, u.session_id, u.project_slug, u.timestamp,
             u.prompt_text, u.prompt_chars,
             a.uuid AS assistant_uuid, a.model,
             COALESCE(a.input_tokens,0)+COALESCE(a.output_tokens,0)
               +COALESCE(a.cache_create_5m_tokens,0)+COALESCE(a.cache_create_1h_tokens,0) AS billable_tokens,
             COALESCE(a.cache_read_tokens,0) AS cache_read_tokens
        FROM messages u
        JOIN messages a ON a.parent_uuid = u.uuid AND a.type='assistant'
       WHERE u.type='user' AND u.prompt_text IS NOT NULL
       ORDER BY {order}
       LIMIT ?
    """
    with connect(db_path) as c:
        return [dict(r) for r in c.execute(sql, (limit,))]


def project_summary(db_path, since=None, until=None) -> list:
    rng, args = _range_clause(since, until)
    sql = f"""
      SELECT project_slug,
             COUNT(DISTINCT session_id) AS sessions,
             SUM(CASE WHEN type='user' THEN 1 ELSE 0 END) AS turns,
             COALESCE(SUM(input_tokens), 0)  AS input_tokens,
             COALESCE(SUM(output_tokens), 0) AS output_tokens,
             SUM(input_tokens)+SUM(output_tokens)
               +SUM(cache_create_5m_tokens)+SUM(cache_create_1h_tokens) AS billable_tokens,
             SUM(cache_read_tokens) AS cache_read_tokens
        FROM messages m
       WHERE 1=1 {rng}
       GROUP BY project_slug
       ORDER BY billable_tokens DESC
    """
    with connect(db_path) as c:
        rows = [dict(r) for r in c.execute(sql, args)]
        for r in rows:
            cwds = [row["cwd"] for row in c.execute(
                "SELECT DISTINCT cwd FROM messages WHERE project_slug=? AND cwd IS NOT NULL",
                (r["project_slug"],),
            )]
            r["project_name"] = best_project_name(cwds, r["project_slug"])
    return rows


def tool_token_breakdown(db_path, since=None, until=None) -> list:
    rng, args = _range_clause(since, until)
    sql = f"""
      SELECT tool_name,
             COUNT(*) AS calls,
             COALESCE(SUM(result_tokens),0) AS result_tokens
        FROM tool_calls
       WHERE tool_name != '_tool_result' {rng}
       GROUP BY tool_name
       ORDER BY calls DESC
    """
    with connect(db_path) as c:
        return [dict(r) for r in c.execute(sql, args)]


def recent_sessions(db_path, limit: int = 20, since=None, until=None) -> list:
    rng, args = _range_clause(since, until)
    sql = f"""
      SELECT session_id, project_slug,
             MIN(timestamp) AS started, MAX(timestamp) AS ended,
             SUM(CASE WHEN type='user' THEN 1 ELSE 0 END) AS turns,
             SUM(input_tokens)+SUM(output_tokens) AS tokens
        FROM messages m
       WHERE 1=1 {rng}
       GROUP BY session_id
       ORDER BY ended DESC
       LIMIT ?
    """
    with connect(db_path) as c:
        rows = [dict(r) for r in c.execute(sql, (*args, limit))]
        # Cache per-slug name lookups so we don't query once per session.
        slug_cache = {}
        for r in rows:
            slug = r["project_slug"]
            if slug not in slug_cache:
                cwds = [row["cwd"] for row in c.execute(
                    "SELECT DISTINCT cwd FROM messages WHERE project_slug=? AND cwd IS NOT NULL",
                    (slug,),
                )]
                slug_cache[slug] = best_project_name(cwds, slug)
            r["project_name"] = slug_cache[slug]
    return rows


def session_turns(db_path, session_id: str) -> list:
    sql = """
      SELECT uuid, parent_uuid, type, timestamp, model, is_sidechain, agent_id,
             input_tokens, output_tokens, cache_read_tokens,
             cache_create_5m_tokens, cache_create_1h_tokens,
             prompt_text, prompt_chars, tool_calls_json, project_slug, cwd
        FROM messages
       WHERE session_id = ?
       ORDER BY timestamp ASC
    """
    with connect(db_path) as c:
        return [dict(r) for r in c.execute(sql, (session_id,))]


def daily_token_breakdown(db_path, since=None, until=None) -> list:
    """One row per day: stacked bar data for input/output/cache_read/cache_create."""
    rng, args = _range_clause(since, until)
    sql = f"""
      SELECT substr(timestamp, 1, 10) AS day,
             COALESCE(SUM(input_tokens),0)      AS input_tokens,
             COALESCE(SUM(output_tokens),0)     AS output_tokens,
             COALESCE(SUM(cache_read_tokens),0) AS cache_read_tokens,
             COALESCE(SUM(cache_create_5m_tokens),0)
               + COALESCE(SUM(cache_create_1h_tokens),0) AS cache_create_tokens
        FROM messages
       WHERE timestamp IS NOT NULL {rng}
       GROUP BY day
       ORDER BY day ASC
    """
    with connect(db_path) as c:
        return [dict(r) for r in c.execute(sql, args)]


def skill_breakdown(db_path, since=None, until=None) -> list:
    """Per-skill invocation counts, distinct sessions, last-used timestamp.

    Token attribution per skill is not included: in Claude Code, a Skill's
    content is loaded via a system-reminder on the next turn, not as the
    tool_result body — so `result_tokens` on _tool_result rows reflects the
    activation ack (tiny), not the skill definition (which is what actually
    fills context). A future schema change (storing tool_use_id on the
    invocation row) could enable precise attribution; for now we only expose
    the reliable counts.
    """
    rng, args = _range_clause(since, until)
    sql = f"""
      SELECT target AS skill,
             COUNT(*) AS invocations,
             COUNT(DISTINCT session_id) AS sessions,
             MAX(timestamp) AS last_used
        FROM tool_calls
       WHERE tool_name = 'Skill' AND target IS NOT NULL AND target != '' {rng}
       GROUP BY target
       ORDER BY invocations DESC
    """
    with connect(db_path) as c:
        return [dict(r) for r in c.execute(sql, args)]


def model_breakdown(db_path, since=None, until=None) -> list:
    """Per-model token totals + turn count. Caller computes cost via pricing."""
    rng, args = _range_clause(since, until)
    sql = f"""
      SELECT COALESCE(model, 'unknown') AS model,
             COUNT(*) AS turns,
             COALESCE(SUM(input_tokens),0)            AS input_tokens,
             COALESCE(SUM(output_tokens),0)           AS output_tokens,
             COALESCE(SUM(cache_read_tokens),0)       AS cache_read_tokens,
             COALESCE(SUM(cache_create_5m_tokens),0)  AS cache_create_5m_tokens,
             COALESCE(SUM(cache_create_1h_tokens),0)  AS cache_create_1h_tokens
        FROM messages
       WHERE type = 'assistant' {rng}
       GROUP BY model
       ORDER BY (input_tokens + output_tokens + cache_create_5m_tokens + cache_create_1h_tokens) DESC
    """
    with connect(db_path) as c:
        return [dict(r) for r in c.execute(sql, args)]


def impact_totals(db_path: Union[str, Path], since=None, until=None) -> dict:
    """SUM of all impact columns from assistant messages, plus source counts."""
    rng, args = _range_clause(since, until)
    sql = f"""
      SELECT COALESCE(SUM(energy_kwh), 0)  AS energy_kwh,
             COALESCE(SUM(gwp_kgco2eq), 0) AS gwp_kgco2eq,
             COALESCE(SUM(wcf_l), 0)       AS wcf_l,
             COALESCE(SUM(adpe_kgsbeq), 0) AS adpe_kgsbeq,
             COALESCE(SUM(pe_mj), 0)       AS pe_mj,
             SUM(CASE WHEN impact_source='ecologits' THEN 1 ELSE 0 END) AS ecologits_count,
             SUM(CASE WHEN impact_source='fallback' THEN 1 ELSE 0 END)  AS fallback_count,
             COUNT(*) AS total_messages
        FROM messages
       WHERE type='assistant' {rng}
    """
    with connect(db_path) as c:
        row = dict(c.execute(sql, args).fetchone() or {})
        return {
            "energy_kwh":      round(float(row.get("energy_kwh") or 0.0), 6),
            "gwp_kgco2eq":     round(float(row.get("gwp_kgco2eq") or 0.0), 6),
            "wcf_l":           round(float(row.get("wcf_l") or 0.0), 6),
            "adpe_kgsbeq":     round(float(row.get("adpe_kgsbeq") or 0.0), 10),
            "pe_mj":           round(float(row.get("pe_mj") or 0.0), 6),
            "ecologits_count": int(row.get("ecologits_count") or 0),
            "fallback_count":  int(row.get("fallback_count") or 0),
            "total_messages":  int(row.get("total_messages") or 0),
        }


def daily_impacts(db_path: Union[str, Path], days: Optional[int] = None, since=None, until=None) -> list[dict]:
    """Daily energy, carbon, and water footprint trend."""
    if since is None and days is not None:
        from datetime import datetime, timedelta, timezone
        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    rng, args = _range_clause(since, until)
    sql = f"""
      SELECT substr(timestamp, 1, 10) AS day,
             COALESCE(SUM(energy_kwh), 0)  AS energy_kwh,
             COALESCE(SUM(gwp_kgco2eq), 0) AS gwp_kgco2eq,
             COALESCE(SUM(wcf_l), 0)       AS wcf_l,
             COALESCE(SUM(adpe_kgsbeq), 0) AS adpe_kgsbeq,
             COALESCE(SUM(pe_mj), 0)       AS pe_mj,
             COUNT(*)                      AS turns
        FROM messages
       WHERE type='assistant' AND timestamp IS NOT NULL {rng}
       GROUP BY day
       ORDER BY day ASC
    """
    with connect(db_path) as c:
        rows = []
        for r in c.execute(sql, args):
            d = dict(r)
            rows.append({
                "day":         d["day"],
                "energy_kwh":  round(float(d["energy_kwh"]), 6),
                "gwp_kgco2eq": round(float(d["gwp_kgco2eq"]), 6),
                "wcf_l":       round(float(d["wcf_l"]), 6),
                "adpe_kgsbeq": round(float(d["adpe_kgsbeq"]), 10),
                "pe_mj":       round(float(d["pe_mj"]), 6),
                "turns":       int(d["turns"]),
            })
        return rows


def model_impact_breakdown(db_path: Union[str, Path], since=None, until=None) -> list[dict]:
    """Per-model environmental impact breakdown ordered by carbon emissions descending."""
    rng, args = _range_clause(since, until)
    sql = f"""
      SELECT COALESCE(model, 'unknown') AS model,
             COUNT(*) AS turns,
             COALESCE(SUM(energy_kwh), 0)  AS energy_kwh,
             COALESCE(SUM(gwp_kgco2eq), 0) AS gwp_kgco2eq,
             COALESCE(SUM(wcf_l), 0)       AS wcf_l,
             COALESCE(SUM(adpe_kgsbeq), 0) AS adpe_kgsbeq,
             COALESCE(SUM(pe_mj), 0)       AS pe_mj,
             COALESCE(SUM(input_tokens), 0) AS input_tokens,
             COALESCE(SUM(output_tokens), 0) AS output_tokens,
             COALESCE(SUM(cache_read_tokens), 0) AS cache_read_tokens,
             COALESCE(SUM(cache_create_5m_tokens), 0) + COALESCE(SUM(cache_create_1h_tokens), 0) AS cache_create_tokens
        FROM messages
       WHERE type='assistant' {rng}
       GROUP BY model
       ORDER BY SUM(gwp_kgco2eq) DESC
    """
    with connect(db_path) as c:
        rows = []
        for r in c.execute(sql, args):
            d = dict(r)
            rows.append({
                "model":               d["model"],
                "turns":               int(d["turns"]),
                "energy_kwh":          round(float(d["energy_kwh"]), 6),
                "gwp_kgco2eq":         round(float(d["gwp_kgco2eq"]), 6),
                "wcf_l":               round(float(d["wcf_l"]), 6),
                "adpe_kgsbeq":         round(float(d["adpe_kgsbeq"]), 10),
                "pe_mj":               round(float(d["pe_mj"]), 6),
                "input_tokens":        int(d["input_tokens"]),
                "output_tokens":       int(d["output_tokens"]),
                "cache_read_tokens":   int(d["cache_read_tokens"]),
                "cache_create_tokens": int(d["cache_create_tokens"]),
            })
        return rows


def insert_audit_run(db_path: Union[str, Path], run: dict) -> str:
    """Insert or update a record in audit_runs and return run_id."""
    import uuid
    from datetime import datetime, timezone

    run_id = str(run.get("run_id") or uuid.uuid4())
    ts = run.get("timestamp") or datetime.now(timezone.utc).isoformat()
    sql = """
      INSERT OR REPLACE INTO audit_runs (
        run_id, session_id, client_id, timestamp, model,
        input_tokens, output_tokens, cost_usd,
        energy_kwh, energy_joules, energy_method,
        benchmark_source, hardware_assumed, heat_dollars,
        is_waste, waste_cost_usd, waste_energy_kwh,
        cost_billed_usd, cost_useful_usd, cost_delta_usd,
        thermal_waste_joules, thermal_waste_kwh, thermal_waste_heat_usd
      ) VALUES (
        :run_id, :session_id, :client_id, :timestamp, :model,
        :input_tokens, :output_tokens, :cost_usd,
        :energy_kwh, :energy_joules, :energy_method,
        :benchmark_source, :hardware_assumed, :heat_dollars,
        :is_waste, :waste_cost_usd, :waste_energy_kwh,
        :cost_billed_usd, :cost_useful_usd, :cost_delta_usd,
        :thermal_waste_joules, :thermal_waste_kwh, :thermal_waste_heat_usd
      )
    """
    params = {
        "run_id":                 run_id,
        "session_id":             run.get("session_id") or "",
        "client_id":              run.get("client_id") or "",
        "timestamp":              ts,
        "model":                  run.get("model") or "",
        "input_tokens":           int(run.get("input_tokens") or 0),
        "output_tokens":          int(run.get("output_tokens") or 0),
        "cost_usd":               float(run.get("cost_usd") or 0.0),
        "energy_kwh":             float(run.get("energy_kwh") or 0.0),
        "energy_joules":          float(run.get("energy_joules") or 0.0),
        "energy_method":          str(run.get("energy_method") or "conservative_fallback"),
        "benchmark_source":       str(run.get("benchmark_source") or ""),
        "hardware_assumed":       str(run.get("hardware_assumed") or ""),
        "heat_dollars":           float(run.get("heat_dollars") or 0.0),
        "is_waste":               1 if run.get("is_waste") else 0,
        "waste_cost_usd":         float(run.get("waste_cost_usd") or 0.0),
        "waste_energy_kwh":       float(run.get("waste_energy_kwh") or 0.0),
        "cost_billed_usd":        float(run.get("cost_billed_usd") or 0.0),
        "cost_useful_usd":        float(run.get("cost_useful_usd") or 0.0),
        "cost_delta_usd":         float(run.get("cost_delta_usd") or 0.0),
        "thermal_waste_joules":   float(run.get("thermal_waste_joules") or 0.0),
        "thermal_waste_kwh":      float(run.get("thermal_waste_kwh") or 0.0),
        "thermal_waste_heat_usd": float(run.get("thermal_waste_heat_usd") or 0.0),
    }
    with connect(db_path) as c:
        c.execute(sql, params)
        c.commit()
    return run_id


def insert_audit_event(db_path: Union[str, Path], event: dict) -> str:
    """Insert a record into audit_events and return event_id."""
    import uuid

    event_id = str(event.get("event_id") or uuid.uuid4())
    sql = """
      INSERT OR REPLACE INTO audit_events (
        event_id, run_id, event_type, tokens_wasted,
        cost_wasted_usd, energy_wasted_kwh, heat_wasted_dollars,
        rule_id, details
      ) VALUES (
        :event_id, :run_id, :event_type, :tokens_wasted,
        :cost_wasted_usd, :energy_wasted_kwh, :heat_wasted_dollars,
        :rule_id, :details
      )
    """
    params = {
        "event_id":            event_id,
        "run_id":              str(event.get("run_id") or ""),
        "event_type":          str(event.get("event_type") or "other"),
        "tokens_wasted":       int(event.get("tokens_wasted") or 0),
        "cost_wasted_usd":     float(event.get("cost_wasted_usd") or 0.0),
        "energy_wasted_kwh":   float(event.get("energy_wasted_kwh") or 0.0),
        "heat_wasted_dollars": float(event.get("heat_wasted_dollars") or 0.0),
        "rule_id":             str(event.get("rule_id") or ""),
        "details":             str(event.get("details") or ""),
    }
    with connect(db_path) as c:
        c.execute(sql, params)
        c.commit()
    return event_id


def get_audit_summary(db_path: Union[str, Path], since: Optional[str] = None, until: Optional[str] = None) -> dict:
    """Aggregated metrics across audit runs."""
    rng, args = _range_clause(since, until)
    sql = f"""
      SELECT COUNT(*)                                AS total_runs,
             COALESCE(SUM(input_tokens), 0)          AS total_input_tokens,
             COALESCE(SUM(output_tokens), 0)         AS total_output_tokens,
             COALESCE(SUM(cost_usd), 0.0)            AS total_cost_usd,
             COALESCE(SUM(energy_kwh), 0.0)          AS total_energy_kwh,
             COALESCE(SUM(energy_joules), 0.0)       AS total_energy_joules,
             COALESCE(SUM(heat_dollars), 0.0)        AS total_heat_dollars,
             COALESCE(SUM(CASE WHEN is_waste=1 THEN 1 ELSE 0 END), 0) AS waste_runs_count,
             COALESCE(SUM(waste_cost_usd), 0.0)      AS total_waste_cost_usd,
             COALESCE(SUM(waste_energy_kwh), 0.0)    AS total_waste_energy_kwh,
             COALESCE(SUM(cost_billed_usd), 0.0)     AS total_cost_billed_usd,
             COALESCE(SUM(cost_useful_usd), 0.0)     AS total_cost_useful_usd,
             COALESCE(SUM(cost_delta_usd), 0.0)      AS total_cost_delta_usd,
             COALESCE(SUM(thermal_waste_joules), 0.0)AS total_thermal_waste_joules,
             COALESCE(SUM(thermal_waste_kwh), 0.0)   AS total_thermal_waste_kwh,
             COALESCE(SUM(thermal_waste_heat_usd), 0.0) AS total_thermal_waste_heat_usd
        FROM audit_runs
       WHERE 1=1 {rng}
    """
    with connect(db_path) as c:
        row = dict(c.execute(sql, args).fetchone() or {})
        billed = float(row.get("total_cost_billed_usd") or 0.0)
        useful = float(row.get("total_cost_useful_usd") or 0.0)
        ratio = round(useful / billed, 4) if billed > 0 else 1.0

        # Energy method breakdown
        methods_sql = f"""
          SELECT energy_method, COUNT(*) as runs, COALESCE(SUM(energy_kwh), 0.0) as energy_kwh
            FROM audit_runs WHERE 1=1 {rng}
           GROUP BY energy_method
        """
        by_method = [dict(r) for r in c.execute(methods_sql, args)]

        # Hardware assumed breakdown
        hw_sql = f"""
          SELECT hardware_assumed, COUNT(*) as runs, COALESCE(SUM(energy_kwh), 0.0) as energy_kwh
            FROM audit_runs WHERE 1=1 {rng}
           GROUP BY hardware_assumed
        """
        by_hardware = [dict(r) for r in c.execute(hw_sql, args)]

    return {
        "total_runs":                 int(row.get("total_runs") or 0),
        "total_input_tokens":         int(row.get("total_input_tokens") or 0),
        "total_output_tokens":        int(row.get("total_output_tokens") or 0),
        "total_cost_usd":             round(float(row.get("total_cost_usd") or 0.0), 6),
        "total_energy_kwh":           round(float(row.get("total_energy_kwh") or 0.0), 6),
        "total_energy_joules":        round(float(row.get("total_energy_joules") or 0.0), 4),
        "total_heat_dollars":         round(float(row.get("total_heat_dollars") or 0.0), 6),
        "waste_runs_count":           int(row.get("waste_runs_count") or 0),
        "total_waste_cost_usd":       round(float(row.get("total_waste_cost_usd") or 0.0), 6),
        "total_waste_energy_kwh":     round(float(row.get("total_waste_energy_kwh") or 0.0), 6),
        "total_cost_billed_usd":      round(billed, 6),
        "total_cost_useful_usd":      round(useful, 6),
        "total_cost_delta_usd":       round(float(row.get("total_cost_delta_usd") or 0.0), 6),
        "total_thermal_waste_joules": round(float(row.get("total_thermal_waste_joules") or 0.0), 4),
        "total_thermal_waste_kwh":    round(float(row.get("total_thermal_waste_kwh") or 0.0), 6),
        "total_thermal_waste_heat_usd": round(float(row.get("total_thermal_waste_heat_usd") or 0.0), 6),
        "waste_efficiency_ratio":     ratio,
        "by_energy_method":           by_method,
        "by_hardware":                by_hardware,
    }


def get_audit_waste_events(
    db_path: Union[str, Path],
    run_id: Optional[str] = None,
    rule_id: Optional[str] = None,
    event_type: Optional[str] = None,
    limit: int = 50,
) -> list[dict]:
    """Retrieve filtered audit waste events."""
    where, args = [], []
    if run_id:
        where.append("run_id = ?"); args.append(run_id)
    if rule_id:
        where.append("rule_id = ?"); args.append(rule_id)
    if event_type:
        where.append("event_type = ?"); args.append(event_type)
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    sql = f"""
      SELECT event_id, run_id, event_type, tokens_wasted,
             cost_wasted_usd, energy_wasted_kwh, heat_wasted_dollars,
             rule_id, details
        FROM audit_events
       {clause}
       ORDER BY ROWID DESC
       LIMIT ?
    """
    args.append(limit)
    with connect(db_path) as c:
        return [dict(r) for r in c.execute(sql, args)]


def get_audit_runs(
    db_path: Union[str, Path],
    session_id: Optional[str] = None,
    limit: int = 50,
    since: Optional[str] = None,
    until: Optional[str] = None,
) -> list[dict]:
    """Retrieve audit runs."""
    where, args = [], []
    if session_id:
        where.append("session_id = ?"); args.append(session_id)
    if since:
        where.append("timestamp >= ?"); args.append(since)
    if until:
        where.append("timestamp < ?"); args.append(until)
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    sql = f"""
      SELECT *
        FROM audit_runs
       {clause}
       ORDER BY timestamp DESC
       LIMIT ?
    """
    args.append(limit)
    with connect(db_path) as c:
        return [dict(r) for r in c.execute(sql, args)]


def backfill_efficiency(
    conn: sqlite3.Connection,
    quality_loader=None,
    force_all: bool = False,
) -> tuple[int, int]:
    """Backfill efficiency calculations for assistant messages.

    Returns (backfilled_count, skipped_no_constants_count).
    """
    from .energy_table import lookup
    from .quality_scores import composite_q
    from .thermodynamics import MissingEnergyConstantsError, efficiency

    scores_available = quality_loader.has_any() if quality_loader else False

    if force_all:
        query = """
            SELECT uuid, model, input_tokens, output_tokens, message_id, session_id
            FROM messages
            WHERE type = 'assistant' AND model IS NOT NULL
        """
        params = ()
    else:
        query = """
            SELECT uuid, model, input_tokens, output_tokens, message_id, session_id
            FROM messages
            WHERE type = 'assistant' AND model IS NOT NULL
              AND (
                eta IS NULL
                OR (quality_adjusted = 0 AND ?)
              )
        """
        params = (1 if scores_available else 0,)

    cursor = conn.cursor()
    rows = cursor.execute(query, params).fetchall()

    backfilled = 0
    skipped = 0

    for row in rows:
        uuid_val = row[0]
        model = row[1]
        n_in = row[2] or 0
        n_out = row[3] or 0
        msg_id = row[4]
        sess_id = row[5]

        try:
            constants = lookup(model)
        except (MissingEnergyConstantsError, Exception):
            skipped += 1
            continue

        score = quality_loader.lookup(msg_id, sess_id) if quality_loader else None
        q = composite_q(score) if score else None
        report = efficiency(n_in, n_out, constants, q)

        cursor.execute(
            """
            UPDATE messages SET
              e_in_j = ?,
              e_out_j = ?,
              w_useful_j = ?,
              waste_j = ?,
              q_alpha = ?,
              q_rho = ?,
              quality_adjusted = ?,
              eta = ?
            WHERE uuid = ?
            """,
            (
                report.e_in,
                report.e_out,
                report.w_useful,
                report.waste_joules,
                score.alpha if score else None,
                score.rho if score else None,
                1 if report.quality_adjusted else 0,
                report.eta,
                uuid_val,
            ),
        )
        backfilled += 1

    conn.commit()
    return backfilled, skipped


def efficiency_overview(
    db_path: Union[str, Path],
    since: Optional[str] = None,
    until: Optional[str] = None,
) -> dict:
    """Return aggregate efficiency metrics, strictly separating adjusted and unadjusted eta."""
    rng, args = _range_clause(since, until)
    sql = f"""
      SELECT
        AVG(CASE WHEN quality_adjusted = 1 THEN eta END) AS avg_eta_adjusted,
        AVG(CASE WHEN quality_adjusted = 0 AND eta IS NOT NULL THEN eta END) AS avg_eta_unadjusted,
        SUM(CASE WHEN quality_adjusted = 1 THEN 1 ELSE 0 END) AS quality_adjusted_count,
        SUM(CASE WHEN quality_adjusted = 0 AND eta IS NOT NULL THEN 1 ELSE 0 END) AS unadjusted_count,
        COALESCE(SUM(e_in_j), 0.0) AS sum_e_in_j,
        COALESCE(SUM(w_useful_j), 0.0) AS sum_w_useful_j,
        COALESCE(SUM(waste_j), 0.0) AS sum_waste_j,
        COUNT(eta) AS total_rows_with_eta
      FROM messages
      WHERE type = 'assistant' AND eta IS NOT NULL {rng}
    """
    with connect(db_path) as c:
        row = c.execute(sql, args).fetchone()
        res = dict(row) if row else {}
        total = int(res.get("total_rows_with_eta") or 0)
        adj_cnt = int(res.get("quality_adjusted_count") or 0)
        unadj_cnt = int(res.get("unadjusted_count") or 0)
        mix_ratio = round(adj_cnt / total, 4) if total > 0 else 0.0

        avg_adj = res.get("avg_eta_adjusted")
        avg_unadj = res.get("avg_eta_unadjusted")

        return {
            "avg_eta_adjusted": round(float(avg_adj), 6) if avg_adj is not None else None,
            "avg_eta_unadjusted": round(float(avg_unadj), 6) if avg_unadj is not None else None,
            "quality_adjusted_count": adj_cnt,
            "unadjusted_count": unadj_cnt,
            "mix_ratio": mix_ratio,
            "sum_e_in_j": round(float(res.get("sum_e_in_j") or 0.0), 6),
            "sum_w_useful_j": round(float(res.get("sum_w_useful_j") or 0.0), 6),
            "sum_waste_j": round(float(res.get("sum_waste_j") or 0.0), 6),
            "total_rows_with_eta": total,
        }


def efficiency_by_model(
    db_path: Union[str, Path],
    since: Optional[str] = None,
    until: Optional[str] = None,
) -> list[dict]:
    """Return efficiency metrics broken down by model."""
    rng, args = _range_clause(since, until)
    sql = f"""
      SELECT
        model,
        COUNT(eta) AS turns_with_eta,
        AVG(CASE WHEN quality_adjusted = 1 THEN eta END) AS avg_eta_adjusted,
        AVG(CASE WHEN quality_adjusted = 0 AND eta IS NOT NULL THEN eta END) AS avg_eta_unadjusted,
        SUM(CASE WHEN quality_adjusted = 1 THEN 1 ELSE 0 END) AS quality_adjusted_count,
        SUM(CASE WHEN quality_adjusted = 0 AND eta IS NOT NULL THEN 1 ELSE 0 END) AS unadjusted_count,
        COALESCE(SUM(e_in_j), 0.0) AS sum_e_in_j,
        COALESCE(SUM(w_useful_j), 0.0) AS sum_w_useful_j,
        COALESCE(SUM(waste_j), 0.0) AS sum_waste_j
      FROM messages
      WHERE type = 'assistant' AND eta IS NOT NULL {rng}
      GROUP BY model
      ORDER BY sum_waste_j DESC
    """
    with connect(db_path) as c:
        rows = c.execute(sql, args).fetchall()
        result = []
        for r in rows:
            row = dict(r)
            avg_adj = row.get("avg_eta_adjusted")
            avg_unadj = row.get("avg_eta_unadjusted")
            result.append({
                "model": row["model"],
                "turns_with_eta": int(row.get("turns_with_eta") or 0),
                "avg_eta_adjusted": round(float(avg_adj), 6) if avg_adj is not None else None,
                "avg_eta_unadjusted": round(float(avg_unadj), 6) if avg_unadj is not None else None,
                "quality_adjusted_count": int(row.get("quality_adjusted_count") or 0),
                "unadjusted_count": int(row.get("unadjusted_count") or 0),
                "sum_e_in_j": round(float(row.get("sum_e_in_j") or 0.0), 6),
                "sum_w_useful_j": round(float(row.get("sum_w_useful_j") or 0.0), 6),
                "sum_waste_j": round(float(row.get("sum_waste_j") or 0.0), 6),
            })
        return result


def efficiency_sessions(
    db_path: Union[str, Path],
    limit: int = 20,
    since: Optional[str] = None,
    until: Optional[str] = None,
) -> list[dict]:
    """Return top wasteful sessions ranked by sum_waste_j descending."""
    rng, args = _range_clause(since, until)
    sql = f"""
      SELECT
        session_id,
        project_slug,
        COUNT(eta) AS turns_with_eta,
        AVG(CASE WHEN quality_adjusted = 1 THEN eta END) AS avg_eta_adjusted,
        AVG(CASE WHEN quality_adjusted = 0 AND eta IS NOT NULL THEN eta END) AS avg_eta_unadjusted,
        COALESCE(SUM(e_in_j), 0.0) AS sum_e_in_j,
        COALESCE(SUM(w_useful_j), 0.0) AS sum_w_useful_j,
        COALESCE(SUM(waste_j), 0.0) AS sum_waste_j
      FROM messages
      WHERE type = 'assistant' AND eta IS NOT NULL {rng}
      GROUP BY session_id, project_slug
      ORDER BY sum_waste_j DESC
      LIMIT ?
    """
    args.append(limit)
    with connect(db_path) as c:
        rows = c.execute(sql, args).fetchall()
        result = []
        for r in rows:
            row = dict(r)
            avg_adj = row.get("avg_eta_adjusted")
            avg_unadj = row.get("avg_eta_unadjusted")
            result.append({
                "session_id": row["session_id"],
                "project_slug": row["project_slug"],
                "turns_with_eta": int(row.get("turns_with_eta") or 0),
                "avg_eta_adjusted": round(float(avg_adj), 6) if avg_adj is not None else None,
                "avg_eta_unadjusted": round(float(avg_unadj), 6) if avg_unadj is not None else None,
                "sum_e_in_j": round(float(row.get("sum_e_in_j") or 0.0), 6),
                "sum_w_useful_j": round(float(row.get("sum_w_useful_j") or 0.0), 6),
                "sum_waste_j": round(float(row.get("sum_waste_j") or 0.0), 6),
            })
        return result


def efficiency_by_day(
    db_path: Union[str, Path],
    since: Optional[str] = None,
    until: Optional[str] = None,
) -> list[dict]:
    """Return daily time series of efficiency and energy metrics."""
    rng, args = _range_clause(since, until)
    sql = f"""
      SELECT
        SUBSTR(timestamp, 1, 10) AS day,
        COALESCE(SUM(e_in_j), 0.0) AS sum_e_in_j,
        COALESCE(SUM(w_useful_j), 0.0) AS sum_w_useful_j,
        COALESCE(SUM(waste_j), 0.0) AS sum_waste_j,
        AVG(CASE WHEN quality_adjusted = 1 THEN eta END) AS avg_eta_adjusted,
        AVG(CASE WHEN quality_adjusted = 0 AND eta IS NOT NULL THEN eta END) AS avg_eta_unadjusted
      FROM messages
      WHERE type = 'assistant' AND eta IS NOT NULL {rng}
      GROUP BY day
      ORDER BY day ASC
    """
    with connect(db_path) as c:
        rows = c.execute(sql, args).fetchall()
        result = []
        for r in rows:
            row = dict(r)
            avg_adj = row.get("avg_eta_adjusted")
            avg_unadj = row.get("avg_eta_unadjusted")
            result.append({
                "day": row["day"],
                "sum_e_in_j": round(float(row.get("sum_e_in_j") or 0.0), 6),
                "sum_w_useful_j": round(float(row.get("sum_w_useful_j") or 0.0), 6),
                "sum_waste_j": round(float(row.get("sum_waste_j") or 0.0), 6),
                "avg_eta_adjusted": round(float(avg_adj), 6) if avg_adj is not None else None,
                "avg_eta_unadjusted": round(float(avg_unadj), 6) if avg_unadj is not None else None,
            })
        return result



