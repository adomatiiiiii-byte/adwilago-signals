from datetime import UTC, datetime, timedelta
import sqlite3

import pytest

from app.services.exness_service import Candle, ExnessConfig, ExnessMarketDataService
from app.services.indicators import IndicatorPoint
from app.services.signal_repository import SignalRepository
from app.services.strategy_engine import (
    DayTradingEngulfingStrategy,
    StrategySignal,
)
from app.database import initialize_database
import app.database as database_module


PRIVATE_KEY_HEX = "00" * 32
NOW = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)


def candle(timestamp, open_price, close_price):
    high = max(open_price, close_price)
    low = min(open_price, close_price)
    return Candle(timestamp, open_price, high, low, close_price, None)


def pair(previous_ma=105.0, previous_rsi=39.994, current_open=110.0, current_close=101.0):
    timestamps = [NOW - timedelta(minutes=10), NOW - timedelta(minutes=5)]
    candles = [
        candle(timestamps[0], 100.0, 100.0),
        candle(timestamps[1], current_open, current_close),
    ]
    indicators = [
        IndicatorPoint(timestamps[0], previous_ma, previous_rsi),
        IndicatorPoint(timestamps[1], 999.0, 999.0),
    ]
    return candles, indicators


def test_bullish_signal_requires_all_three_conditions():
    candles, indicators = pair()

    signal = DayTradingEngulfingStrategy().evaluate(candles, indicators, 1, now=NOW)

    assert signal is not None
    assert signal.direction == "bullish"
    assert signal.candle_timestamp == candles[1].timestamp


def evidence_repository(tmp_path, monkeypatch):
    database_path = tmp_path / "signals.db"
    monkeypatch.setattr(database_module, "DATABASE_PATH", database_path)
    initialize_database()
    return database_path, SignalRepository(database_path)


def test_generated_signal_contains_exact_audit_evidence():
    candles, indicators = pair()

    signal = DayTradingEngulfingStrategy().evaluate(candles, indicators, 1, now=NOW)

    assert signal is not None
    assert signal.evidence is not None
    assert signal.evidence.current_open == candles[1].open
    assert signal.evidence.current_high == candles[1].high
    assert signal.evidence.current_low == candles[1].low
    assert signal.evidence.current_close == candles[1].close
    assert signal.evidence.current_candle_timestamp == candles[1].timestamp
    assert signal.evidence.previous_open == candles[0].open
    assert signal.evidence.previous_high == candles[0].high
    assert signal.evidence.previous_low == candles[0].low
    assert signal.evidence.previous_close == candles[0].close
    assert signal.evidence.previous_candle_timestamp == candles[0].timestamp
    assert signal.evidence.previous_ma75 == 105.0
    assert signal.evidence.previous_rsi7_raw == 39.994
    assert signal.evidence.previous_rsi7_rounded == 39.99
    assert (signal.evidence.condition_1_passed, signal.evidence.condition_2_passed, signal.evidence.condition_3_passed) == (True, True, True)
    assert signal.evidence.evaluation_timestamp == NOW


def test_signal_and_evidence_are_persisted_and_survive_repository_restart(tmp_path, monkeypatch):
    database_path, repository = evidence_repository(tmp_path, monkeypatch)
    candles, indicators = pair()
    signal = DayTradingEngulfingStrategy().evaluate(candles, indicators, 1, now=NOW)

    assert signal is not None
    assert repository.insert(signal) is True
    assert SignalRepository(database_path).insert(signal) is False
    with sqlite3.connect(database_path) as connection:
        row = connection.execute(
            "SELECT s.id, e.current_close, e.previous_ma75, e.previous_rsi7_raw, e.previous_rsi7_rounded, e.condition_1_passed, e.condition_2_passed, e.condition_3_passed, e.evaluation_timestamp FROM signals s JOIN signal_evidence e ON e.signal_id = s.id"
        ).fetchone()
        assert row == (1, 101.0, 105.0, 39.994, 39.99, 1, 1, 1, "2026-11-02T12:00:00Z")


def test_bearish_signal_persists_directional_evidence(tmp_path, monkeypatch):
    _, repository = evidence_repository(tmp_path, monkeypatch)
    candles, indicators = pair(previous_ma=95.0, previous_rsi=60.006, current_open=90.0, current_close=99.0)
    signal = DayTradingEngulfingStrategy().evaluate(candles, indicators, 1, now=NOW)

    assert signal is not None
    assert signal.direction == "bearish"
    assert repository.insert(signal) is True
    with sqlite3.connect(repository.database_path) as connection:
        row = connection.execute(
            "SELECT condition_1_passed, condition_2_passed, condition_3_passed, previous_rsi7_raw, previous_rsi7_rounded FROM signal_evidence"
        ).fetchone()
        assert row == (1, 1, 1, 60.006, 60.01)


def test_evidence_failure_rolls_back_signal_insertion(tmp_path):
    database_path = tmp_path / "signals.db"
    with sqlite3.connect(database_path) as connection:
        connection.execute("CREATE TABLE signals (id INTEGER PRIMARY KEY AUTOINCREMENT, market TEXT NOT NULL, strategy TEXT NOT NULL, direction TEXT NOT NULL, candle_timestamp TEXT NOT NULL, status TEXT NOT NULL)")
        connection.execute("CREATE UNIQUE INDEX ux_signals ON signals (market, strategy, candle_timestamp)")
        connection.execute("CREATE TABLE signal_evidence (signal_id INTEGER PRIMARY KEY, current_open REAL NOT NULL)")
    candles, indicators = pair()
    signal = DayTradingEngulfingStrategy().evaluate(candles, indicators, 1, now=NOW)

    assert signal is not None
    with pytest.raises(sqlite3.OperationalError):
        SignalRepository(database_path).insert(signal)
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM signals").fetchone()[0] == 0


@pytest.mark.parametrize(
    ("current_open", "current_close", "previous_ma", "previous_rsi"),
    [
        (110.0, 99.0, 105.0, 39.994),  # close is not above previous open
        (104.0, 101.0, 105.0, 39.994),  # open is not above previous MA
        (110.0, 101.0, 105.0, 40.0),  # rounded RSI is not below 40
    ],
)
def test_each_bullish_condition_failure_returns_no_signal(
    current_open, current_close, previous_ma, previous_rsi
):
    candles, indicators = pair(previous_ma, previous_rsi, current_open, current_close)

    assert DayTradingEngulfingStrategy().evaluate(candles, indicators, 1, now=NOW) is None


def test_bearish_signal_requires_all_three_conditions():
    candles, indicators = pair(previous_ma=95.0, previous_rsi=60.006, current_open=90.0, current_close=99.0)

    signal = DayTradingEngulfingStrategy().evaluate(candles, indicators, 1, now=NOW)

    assert signal is not None
    assert signal.direction == "bearish"


@pytest.mark.parametrize(
    ("current_open", "current_close", "previous_ma", "previous_rsi"),
    [
        (90.0, 101.0, 95.0, 60.006),  # close is not below previous open
        (96.0, 99.0, 95.0, 60.006),  # open is not below previous MA
        (90.0, 99.0, 95.0, 60.0),  # rounded RSI is not above 60
    ],
)
def test_each_bearish_condition_failure_returns_no_signal(
    current_open, current_close, previous_ma, previous_rsi
):
    candles, indicators = pair(previous_ma, previous_rsi, current_open, current_close)

    assert DayTradingEngulfingStrategy().evaluate(candles, indicators, 1, now=NOW) is None


def test_rsi_rounding_boundaries_are_used_for_comparisons():
    bullish_candles, bullish_indicators = pair(previous_rsi=39.995)
    bearish_candles, bearish_indicators = pair(previous_ma=95.0, previous_rsi=60.005, current_open=90.0, current_close=99.0)
    strategy = DayTradingEngulfingStrategy()

    assert strategy.evaluate(bullish_candles, bullish_indicators, 1, now=NOW) is None
    assert strategy.evaluate(bearish_candles, bearish_indicators, 1, now=NOW) is not None


def test_missing_values_and_insufficient_history_return_no_signal():
    candles, indicators = pair()
    strategy = DayTradingEngulfingStrategy()

    assert strategy.evaluate(candles[:1], indicators[:1], 0, now=NOW) is None
    assert strategy.evaluate(candles, [IndicatorPoint(indicators[0].timestamp, None, 39.0), indicators[1]], 1, now=NOW) is None
    assert strategy.evaluate(candles, [IndicatorPoint(indicators[0].timestamp, 105.0, None), indicators[1]], 1, now=NOW) is None


def test_incomplete_current_candle_is_ignored():
    incomplete = NOW - timedelta(seconds=299)
    candles = [candle(NOW - timedelta(minutes=10), 100.0, 100.0), candle(incomplete, 110.0, 101.0)]
    indicators = [IndicatorPoint(candles[0].timestamp, 105.0, 39.0), IndicatorPoint(incomplete, 999.0, 999.0)]

    assert DayTradingEngulfingStrategy().evaluate(candles, indicators, 1, now=NOW) is None


def test_previous_candle_indicators_are_used_not_current_values():
    candles, indicators = pair(previous_ma=105.0, previous_rsi=39.0, current_open=110.0, current_close=101.0)
    indicators[1] = IndicatorPoint(candles[1].timestamp, 1.0, 99.0)

    signal = DayTradingEngulfingStrategy().evaluate(candles, indicators, 1, now=NOW)

    assert signal is not None
    assert signal.direction == "bullish"


class RecordingEngine:
    def __init__(self):
        self.indices = []

    def evaluate(self, candles, indicators, current_index):
        self.indices.append(current_index)
        return StrategySignal("USDJPY", "DayTrading Engulfing", "M5", "bullish", candles[current_index].timestamp)


class RecordingRepository:
    def __init__(self):
        self.signals = []

    def insert(self, signal):
        self.signals.append(signal)
        return True


def test_multiple_new_completed_candles_are_processed_chronologically():
    engine = RecordingEngine()
    repository = RecordingRepository()
    service = ExnessMarketDataService(
        ExnessConfig("key", PRIVATE_KEY_HEX, "123456", "https://gateway.test"),
        strategy_engine=engine,
        signal_repository=repository,
    )
    timestamps = [NOW - timedelta(minutes=20 - (index * 5)) for index in range(4)]
    service._candles = {
        int(timestamp.timestamp()): candle(timestamp, 100.0, 100.0)
        for timestamp in timestamps
    }
    service._candles_loaded = True

    import asyncio
    asyncio.run(service._evaluate_completed_candles())
    asyncio.run(service._evaluate_completed_candles())

    assert engine.indices == [1, 2, 3]
    assert [signal.candle_timestamp for signal in repository.signals] == timestamps[1:]
    asyncio.run(service.close())


def test_tick_updates_do_not_trigger_strategy_evaluation():
    class TickWebSocket:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return False

        async def send(self, message):
            return None

        def __aiter__(self):
            return self._messages()

        async def _messages(self):
            yield '{"instrument":"USDJPY","bid":153.427,"ask":153.437}'

    engine = RecordingEngine()
    service = ExnessMarketDataService(
        ExnessConfig("key", PRIVATE_KEY_HEX, "123456", "https://gateway.test"),
        strategy_engine=engine,
        signal_repository=RecordingRepository(),
    )
    service._access_point = "https://access-point.test"
    service.discover_access_point = lambda: _completed("https://access-point.test")
    service._websocket_connector = lambda url, **kwargs: TickWebSocket()

    async def stop_after_disconnect(delay):
        service._closing = True

    service._sleep = stop_after_disconnect

    import asyncio
    asyncio.run(service._run_tick_stream())

    assert service._latest_tick["bid"] == 153.427
    assert engine.indices == []
    asyncio.run(service.close())


def test_duplicate_signal_prevention_survives_repository_restart(tmp_path):
    database_path = tmp_path / "signals.db"
    with __import__("sqlite3").connect(database_path) as connection:
        connection.execute("CREATE TABLE signals (id INTEGER PRIMARY KEY AUTOINCREMENT, market TEXT NOT NULL, strategy TEXT NOT NULL, direction TEXT NOT NULL, candle_timestamp TEXT NOT NULL, status TEXT NOT NULL)")
        connection.execute("CREATE UNIQUE INDEX ux_signals ON signals (market, strategy, candle_timestamp)")
    signal = StrategySignal("USDJPY", "DayTrading Engulfing", "M5", "bullish", NOW - timedelta(minutes=5))

    assert SignalRepository(database_path).insert(signal) is True
    assert SignalRepository(database_path).insert(signal) is False
    with __import__("sqlite3").connect(database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM signals").fetchone()[0] == 1


async def _completed(value):
    return value
