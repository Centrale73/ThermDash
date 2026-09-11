"""Unit tests for database migration and efficiency queries."""

from pathlib import Path
import sqlite3

import pytest

from token_dashboard.db import (
    backfill_efficiency,
    efficiency_by_day,
    efficiency_by_model,
    efficiency_overview,
    efficiency_sessions,
    init_db,
)
from token_dashboard.quality_scores import QualityScoreLoader


def create_pre_migration_db(path: Path) -> None:
    """Create a database in pre-migration state (no efficiency columns, no quality_scores table)."""
    conn = sqlite3.connect(path)
    conn.execute("""
        CREATE TABLE messages (
            uuid TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            project_slug TEXT NOT NULL,
            type TEXT NOT NULL,
            timestamp TEXT NOT NULL,
            model TEXT,
            message_id TEXT,
            input_tokens INTEGER NOT NULL DEFAULT 0,
            output_tokens INTEGER NOT NULL DEFAULT 0
        )
    """)
    conn.commit()
    conn.close()


def test_migration_adds_all_columns_and_table(tmp_path: Path):
    db_file = tmp_path / "old.db"
    create_pre_migration_db(db_file)

    # Run init_db which applies migrations
    init_db(db_file)

    conn = sqlite3.connect(db_file)
    cols = {row[1] for row in conn.execute("PRAGMA table_info(messages)")}
    expected_cols = {
        "e_in_j",
        "e_out_j",
        "w_useful_j",
        "waste_j",
        "q_alpha",
        "q_rho",
        "quality_adjusted",
        "eta",
    }
    assert expected_cols.issubset(cols)

    # Check quality_scores table exists
    tbl = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='quality_scores'"
    ).fetchone()
    assert tbl is not None
    conn.close()


def test_backfill_efficiency_unadjusted(tmp_path: Path):
    db_file = tmp_path / "test.db"
    init_db(db_file)

    conn = sqlite3.connect(db_file)
    conn.execute(
        """
        INSERT INTO messages (
            uuid, session_id, project_slug, type, timestamp, model, message_id,
            input_tokens, output_tokens
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "u1",
            "s1",
            "p1",
            "assistant",
            "2026-03-01T12:00:00Z",
            "gpt-4o",
            "m1",
            100,
            50,
        ),
    )
    conn.commit()

    backfilled, skipped = backfill_efficiency(conn)
    assert backfilled == 1
    assert skipped == 0

    row = conn.execute(
        "SELECT e_in_j, e_out_j, w_useful_j, waste_j, quality_adjusted, eta FROM messages WHERE uuid = 'u1'"
    ).fetchone()
    assert row[0] is not None and row[0] > 0
    assert row[1] is not None and row[1] > 0
    assert row[2] is not None and row[2] > 0
    assert row[3] is not None and row[3] >= 0
    assert row[4] == 0  # quality_adjusted is 0
    assert row[5] is not None and row[5] > 0
    # Conservation of energy: e_in_j == w_useful_j + waste_j
    assert pytest.approx(row[0]) == row[2] + row[3]
    conn.close()


def test_backfill_rescore_when_scores_available(tmp_path: Path):
    db_file = tmp_path / "test_rescore.db"
    init_db(db_file)

    conn = sqlite3.connect(db_file)
    # Insert assistant row that already has eta but quality_adjusted = 0
    conn.execute(
        """
        INSERT INTO messages (
            uuid, session_id, project_slug, type, timestamp, model, message_id,
            input_tokens, output_tokens, eta, quality_adjusted
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0.8, 0)
        """,
        (
            "u_score",
            "s1",
            "p1",
            "assistant",
            "2026-03-01T12:00:00Z",
            "gpt-4o",
            "msg_scored",
            100,
            50,
        ),
    )
    # Add quality score into quality_scores table
    conn.execute(
        """
        INSERT INTO quality_scores (message_id, session_id, alpha, rho, method, w_a, w_p, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        ("msg_scored", "s1", 0.9, 0.7, "human", 0.5, 0.5, 1000.0),
    )
    conn.commit()

    loader = QualityScoreLoader(db_path=db_file)
    assert loader.has_any() is True

    backfilled, skipped = backfill_efficiency(conn, quality_loader=loader)
    assert backfilled == 1

    row = conn.execute(
        "SELECT quality_adjusted, q_alpha, q_rho, eta, w_useful_j FROM messages WHERE uuid = 'u_score'"
    ).fetchone()
    assert row[0] == 1  # quality_adjusted is now 1
    assert row[1] == 0.9
    assert row[2] == 0.7
    assert row[3] is not None
    conn.close()


def test_efficiency_queries(tmp_path: Path):
    db_file = tmp_path / "test_queries.db"
    init_db(db_file)

    conn = sqlite3.connect(db_file)
    # Row 1: unadjusted
    conn.execute(
        """
        INSERT INTO messages (
            uuid, session_id, project_slug, type, timestamp, model, message_id,
            input_tokens, output_tokens
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        ("u1", "s1", "proj", "assistant", "2026-03-01T10:00:00Z", "gpt-4o", "m1", 100, 50),
    )
    # Row 2: scored
    conn.execute(
        """
        INSERT INTO messages (
            uuid, session_id, project_slug, type, timestamp, model, message_id,
            input_tokens, output_tokens
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        ("u2", "s2", "proj", "assistant", "2026-03-01T11:00:00Z", "claude-3-5-sonnet", "m2", 200, 100),
    )
    conn.execute(
        """
        INSERT INTO quality_scores (message_id, session_id, alpha, rho, method, w_a, w_p, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        ("m2", "s2", 0.8, 0.8, "human", 0.5, 0.5, 1000.0),
    )
    conn.commit()

    loader = QualityScoreLoader(db_path=db_file)
    backfill_efficiency(conn, quality_loader=loader, force_all=True)
    conn.close()

    # Overview
    ov = efficiency_overview(db_file)
    assert ov["total_rows_with_eta"] == 2
    assert ov["quality_adjusted_count"] == 1
    assert ov["unadjusted_count"] == 1
    assert ov["mix_ratio"] == 0.5
    assert ov["avg_eta_adjusted"] is not None
    assert ov["avg_eta_unadjusted"] is not None
    assert ov["sum_e_in_j"] > 0
    assert ov["sum_w_useful_j"] > 0
    assert ov["sum_waste_j"] >= 0

    # By model
    bm = efficiency_by_model(db_file)
    assert len(bm) == 2
    models = {r["model"] for r in bm}
    assert "gpt-4o" in models
    assert "claude-3-5-sonnet" in models

    # Sessions
    sess = efficiency_sessions(db_file, limit=10)
    assert len(sess) == 2
    sess_ids = {r["session_id"] for r in sess}
    assert "s1" in sess_ids
    assert "s2" in sess_ids

    # By day
    by_day = efficiency_by_day(db_file)
    assert len(by_day) == 1
    assert by_day[0]["day"] == "2026-03-01"
    assert by_day[0]["sum_e_in_j"] > 0
