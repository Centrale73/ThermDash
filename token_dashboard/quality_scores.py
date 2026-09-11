"""Quality score loader and evaluation module.

Supports user-supplied quality_scores.json and the SQLite quality_scores table.
Computes composite Q from alpha (accuracy) and rho (precision/relevance).
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import sqlite3
from typing import Optional, Union


@dataclass
class QualityScore:
    """Quality metrics for gating useful work."""

    alpha: float  # Accuracy / correctness in [0, 1]
    rho: float  # Precision / relevance in [0, 1]
    method: str = "unknown"  # e.g., 'human', 'judge', 'exact_match'
    w_a: float = 0.5  # Accuracy weight (defaults to 0.5)
    w_p: float = 0.5  # Precision weight (defaults to 0.5)


def composite_q(s: QualityScore) -> float:
    """Calculate composite quality factor Q in [0, 1].

    Q = w_a * alpha + w_p * rho
    Clamped to [0.0, 1.0].
    """
    total_w = s.w_a + s.w_p
    if total_w <= 0:
        return 1.0
    val = (s.w_a * s.alpha + s.w_p * s.rho) / total_w
    return max(0.0, min(1.0, float(val)))


class QualityScoreLoader:
    """Loads quality scores with DB-first lookup, falling back to JSON."""

    def __init__(
        self,
        json_path: Union[str, Path, None] = None,
        db_path: Union[str, Path, None] = None,
    ) -> None:
        self.json_path = Path(json_path) if json_path else None
        self.db_path = Path(db_path) if db_path else None
        self._cached_scores: dict[str, QualityScore] = {}
        self._last_mtime: Optional[float] = None
        self.reload_if_stale()

    def reload_if_stale(self) -> None:
        """Reload JSON scores file if mtime has changed."""
        if not self.json_path or not self.json_path.exists():
            self._cached_scores = {}
            self._last_mtime = None
            return

        try:
            mtime = self.json_path.stat().st_mtime
            if self._last_mtime != mtime:
                data = json.loads(self.json_path.read_text(encoding="utf-8"))
                scores_raw = data.get("scores", {})
                new_scores: dict[str, QualityScore] = {}
                for key, item in scores_raw.items():
                    new_scores[key] = QualityScore(
                        alpha=float(item.get("alpha", 1.0)),
                        rho=float(item.get("rho", 1.0)),
                        method=str(item.get("method", "unknown")),
                        w_a=float(item.get("w_a", 0.5)),
                        w_p=float(item.get("w_p", 0.5)),
                    )
                self._cached_scores = new_scores
                self._last_mtime = mtime
        except Exception:
            # Stale / malformed JSON should not crash scan
            pass

    def has_any(self) -> bool:
        """Return True if any quality scores exist in JSON or SQLite table."""
        self.reload_if_stale()
        if self._cached_scores:
            return True

        if self.db_path and self.db_path.exists():
            try:
                conn = sqlite3.connect(self.db_path)
                try:
                    table_exists = conn.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='quality_scores'"
                    ).fetchone()
                    if table_exists:
                        row = conn.execute("SELECT 1 FROM quality_scores LIMIT 1").fetchone()
                        if row:
                            return True
                finally:
                    conn.close()
            except Exception:
                pass

        return False

    def lookup(self, message_id: str, session_id: str = "") -> QualityScore | None:
        """Look up score for a message.

        Precedence:
        1. SQLite quality_scores table (by message_id)
        2. JSON scores by message_id
        3. JSON scores by session_id/message_id
        4. None
        """
        if not message_id:
            return None

        # 1. DB table lookup
        if self.db_path and self.db_path.exists():
            try:
                conn = sqlite3.connect(self.db_path)
                conn.row_factory = sqlite3.Row
                try:
                    table_exists = conn.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='quality_scores'"
                    ).fetchone()
                    if table_exists:
                        row = conn.execute(
                            "SELECT alpha, rho, method, w_a, w_p FROM quality_scores WHERE message_id = ?",
                            (message_id,),
                        ).fetchone()
                        if row:
                            return QualityScore(
                                alpha=float(row["alpha"]),
                                rho=float(row["rho"]),
                                method=str(row["method"]),
                                w_a=float(row["w_a"]),
                                w_p=float(row["w_p"]),
                            )
                finally:
                    conn.close()
            except Exception:
                pass

        # 2 & 3. JSON lookup
        self.reload_if_stale()
        if message_id in self._cached_scores:
            return self._cached_scores[message_id]

        if session_id:
            composite_key = f"{session_id}/{message_id}"
            if composite_key in self._cached_scores:
                return self._cached_scores[composite_key]

        return None


_loader: QualityScoreLoader | None = None


def init_loader(
    json_path: Union[str, Path, None] = None,
    db_path: Union[str, Path, None] = None,
) -> QualityScoreLoader:
    """Initialize the global QualityScoreLoader singleton."""
    global _loader
    _loader = QualityScoreLoader(json_path=json_path, db_path=db_path)
    return _loader


def get_loader() -> QualityScoreLoader | None:
    """Retrieve the global QualityScoreLoader singleton."""
    return _loader
