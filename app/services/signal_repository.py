"""SQLite persistence for generated strategy signals."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

from app.database import DATABASE_PATH
from app.services.strategy_engine import StrategySignal


class SignalRepository:
    """Persist only real bullish and bearish signals idempotently."""

    def __init__(self, database_path: Path = DATABASE_PATH) -> None:
        self.database_path = database_path

    def insert(self, signal: StrategySignal) -> bool:
        """Insert a signal and its audit evidence in one transaction."""
        self.database_path.parent.mkdir(exist_ok=True)
        with sqlite3.connect(self.database_path) as connection:
            connection.execute("PRAGMA foreign_keys = ON")
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO signals
                    (market, strategy, direction, candle_timestamp, status)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    signal.market,
                    signal.strategy,
                    signal.direction,
                    signal.candle_timestamp.isoformat().replace("+00:00", "Z"),
                    signal.status,
                ),
            )
            if cursor.rowcount != 1:
                return False
            if signal.evidence is not None:
                evidence = signal.evidence
                connection.execute(
                    """
                    INSERT INTO signal_evidence (
                        signal_id, current_open, current_high, current_low, current_close,
                        current_candle_timestamp, previous_open, previous_high, previous_low,
                        previous_close, previous_candle_timestamp, previous_ma75,
                        previous_rsi7_raw, previous_rsi7_rounded, condition_1_passed,
                        condition_2_passed, condition_3_passed, evaluation_timestamp
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        cursor.lastrowid,
                        evidence.current_open,
                        evidence.current_high,
                        evidence.current_low,
                        evidence.current_close,
                        evidence.current_candle_timestamp.isoformat().replace("+00:00", "Z"),
                        evidence.previous_open,
                        evidence.previous_high,
                        evidence.previous_low,
                        evidence.previous_close,
                        evidence.previous_candle_timestamp.isoformat().replace("+00:00", "Z"),
                        evidence.previous_ma75,
                        evidence.previous_rsi7_raw,
                        evidence.previous_rsi7_rounded,
                        int(evidence.condition_1_passed),
                        int(evidence.condition_2_passed),
                        int(evidence.condition_3_passed),
                        evidence.evaluation_timestamp.isoformat().replace("+00:00", "Z"),
                    ),
                )
            return True

    def list_recent(self, limit: int = 50) -> list[dict[str, object]]:
        """Return persisted signals newest first without changing the database."""
        safe_limit = max(1, min(int(limit), 200))
        with sqlite3.connect(self.database_path) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                """
                SELECT id, market, strategy, direction, candle_timestamp, status
                FROM signals
                ORDER BY candle_timestamp DESC, id DESC
                LIMIT ?
                """,
                (safe_limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def list_today_session(
        self,
        now: datetime | None = None,
        limit: int = 200,
    ) -> list[dict[str, object]]:
        """Return today's 07:00 inclusive to 18:00 exclusive Kampala signals."""
        kampala = ZoneInfo("Africa/Kampala")
        reference = now or datetime.now(UTC)
        if reference.tzinfo is None:
            reference = reference.replace(tzinfo=UTC)
        session_date = reference.astimezone(kampala).date()
        session_start = datetime.combine(session_date, time(7), tzinfo=kampala).astimezone(UTC)
        session_end = datetime.combine(session_date, time(18), tzinfo=kampala).astimezone(UTC)
        safe_limit = max(1, min(int(limit), 500))
        start_value = session_start.isoformat().replace("+00:00", "Z")
        end_value = session_end.isoformat().replace("+00:00", "Z")
        with sqlite3.connect(self.database_path) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                """
                SELECT id, market, strategy, direction, candle_timestamp, status
                FROM signals
                WHERE candle_timestamp >= ? AND candle_timestamp < ?
                ORDER BY candle_timestamp DESC, id DESC
                LIMIT ?
                """,
                (start_value, end_value, safe_limit),
            ).fetchall()
        return [dict(row) for row in rows]
