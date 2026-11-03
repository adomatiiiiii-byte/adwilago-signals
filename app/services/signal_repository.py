"""SQLite persistence for generated strategy signals."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from app.database import DATABASE_PATH
from app.services.strategy_engine import StrategySignal


class SignalRepository:
    """Persist only real bullish and bearish signals idempotently."""

    def __init__(self, database_path: Path = DATABASE_PATH) -> None:
        self.database_path = database_path

    def insert(self, signal: StrategySignal) -> bool:
        """Insert a signal, returning False when the unique key already exists."""
        self.database_path.parent.mkdir(exist_ok=True)
        with sqlite3.connect(self.database_path) as connection:
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
            return cursor.rowcount == 1

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
