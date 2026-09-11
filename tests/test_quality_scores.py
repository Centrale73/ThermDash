"""Unit tests for quality scores loader and composite Q calculation."""

import json
from pathlib import Path
import sqlite3
import time

import pytest

from token_dashboard.quality_scores import (
    QualityScore,
    QualityScoreLoader,
    composite_q,
)


def test_composite_q_default_weights():
    # w_a = 0.5, w_p = 0.5
    s = QualityScore(alpha=0.8, rho=0.6, method="human")
    assert pytest.approx(composite_q(s)) == 0.7


def test_composite_q_custom_weights():
    s = QualityScore(alpha=1.0, rho=0.0, method="judge", w_a=0.8, w_p=0.2)
    assert pytest.approx(composite_q(s)) == 0.8


def test_composite_q_clamping():
    s_high = QualityScore(alpha=1.5, rho=1.2, method="test")
    assert composite_q(s_high) == 1.0

    s_low = QualityScore(alpha=-0.5, rho=-0.2, method="test")
    assert composite_q(s_low) == 0.0


def test_json_loading_and_lookup(tmp_path: Path):
    json_file = tmp_path / "quality_scores.json"
    data = {
        "scores": {
            "msg_123": {"alpha": 0.9, "rho": 0.8, "method": "human"},
            "sess_abc/msg_456": {"alpha": 0.5, "rho": 0.6, "method": "judge"},
        }
    }
    json_file.write_text(json.dumps(data), encoding="utf-8")

    loader = QualityScoreLoader(json_path=json_file)
    assert loader.has_any() is True

    # Lookup by direct message_id
    score1 = loader.lookup("msg_123")
    assert score1 is not None
    assert score1.alpha == 0.9
    assert score1.rho == 0.8
    assert score1.method == "human"

    # Lookup by session_id/message_id composite
    score2 = loader.lookup("msg_456", session_id="sess_abc")
    assert score2 is not None
    assert score2.alpha == 0.5
    assert score2.rho == 0.6

    # Non-existent
    assert loader.lookup("non_existent") is None


def test_absent_json_file(tmp_path: Path):
    missing_file = tmp_path / "missing.json"
    loader = QualityScoreLoader(json_path=missing_file)
    assert loader.has_any() is False
    assert loader.lookup("msg_1") is None


def test_db_priority_over_json(tmp_path: Path):
    db_file = tmp_path / "test.db"
    conn = sqlite3.connect(db_file)
    conn.execute("""
        CREATE TABLE quality_scores (
            message_id TEXT PRIMARY KEY,
            session_id TEXT,
            alpha REAL NOT NULL,
            rho REAL NOT NULL,
            method TEXT NOT NULL,
            w_a REAL NOT NULL DEFAULT 0.5,
            w_p REAL NOT NULL DEFAULT 0.5,
            created_at REAL NOT NULL
        )
    """)
    conn.execute(
        "INSERT INTO quality_scores VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("msg_shared", "sess_1", 0.95, 0.95, "db_override", 0.5, 0.5, 1000.0),
    )
    conn.commit()
    conn.close()

    json_file = tmp_path / "quality_scores.json"
    json_file.write_text(
        json.dumps({
            "scores": {
                "msg_shared": {"alpha": 0.1, "rho": 0.1, "method": "json_stale"}
            }
        }),
        encoding="utf-8",
    )

    loader = QualityScoreLoader(json_path=json_file, db_path=db_file)
    assert loader.has_any() is True

    score = loader.lookup("msg_shared")
    assert score is not None
    assert score.alpha == 0.95
    assert score.method == "db_override"


def test_reload_if_stale(tmp_path: Path):
    json_file = tmp_path / "quality_scores.json"
    json_file.write_text(
        json.dumps({"scores": {"m1": {"alpha": 0.2, "rho": 0.2, "method": "v1"}}}),
        encoding="utf-8",
    )

    loader = QualityScoreLoader(json_path=json_file)
    assert loader.lookup("m1").alpha == 0.2

    # Wait or force mtime update
    time.sleep(0.05)
    json_file.write_text(
        json.dumps({"scores": {"m1": {"alpha": 0.85, "rho": 0.85, "method": "v2"}}}),
        encoding="utf-8",
    )

    loader.reload_if_stale()
    updated = loader.lookup("m1")
    assert updated is not None
    assert updated.alpha == 0.85
    assert updated.method == "v2"
