"""Small SQLite setup used by the Stage 1 foundation."""

import sqlite3
from pathlib import Path


DATABASE_PATH = Path(__file__).resolve().parent.parent / "data" / "signals.db"


def initialize_database() -> None:
    """Create the empty signal-history table for later stages."""
    DATABASE_PATH.parent.mkdir(exist_ok=True)
    with sqlite3.connect(DATABASE_PATH) as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                market TEXT NOT NULL,
                strategy TEXT NOT NULL,
                direction TEXT NOT NULL,
                candle_timestamp TEXT NOT NULL,
                status TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS ux_signals_market_strategy_candle
            ON signals (market, strategy, candle_timestamp)
            """
        )
