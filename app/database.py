"""Small SQLite setup used by the Stage 1 foundation."""

import sqlite3
from pathlib import Path


DATABASE_PATH = Path(__file__).resolve().parent.parent / "data" / "signals.db"


def initialize_database() -> None:
    """Create the signal-history and audit-evidence tables if needed."""
    DATABASE_PATH.parent.mkdir(exist_ok=True)
    with sqlite3.connect(DATABASE_PATH) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
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
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS signal_evidence (
                signal_id INTEGER PRIMARY KEY,
                current_open REAL NOT NULL,
                current_high REAL NOT NULL,
                current_low REAL NOT NULL,
                current_close REAL NOT NULL,
                current_candle_timestamp TEXT NOT NULL,
                previous_open REAL NOT NULL,
                previous_high REAL NOT NULL,
                previous_low REAL NOT NULL,
                previous_close REAL NOT NULL,
                previous_candle_timestamp TEXT NOT NULL,
                previous_ma75 REAL NOT NULL,
                previous_rsi7_raw REAL NOT NULL,
                previous_rsi7_rounded REAL NOT NULL,
                condition_1_passed INTEGER NOT NULL CHECK (condition_1_passed IN (0, 1)),
                condition_2_passed INTEGER NOT NULL CHECK (condition_2_passed IN (0, 1)),
                condition_3_passed INTEGER NOT NULL CHECK (condition_3_passed IN (0, 1)),
                evaluation_timestamp TEXT NOT NULL,
                FOREIGN KEY (signal_id) REFERENCES signals (id)
            )
            """
        )
