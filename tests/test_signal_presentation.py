import asyncio
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.routes import market_routes
from app.services.exness_service import Candle, ExnessConfig, ExnessMarketDataService
from app.services.indicators import IndicatorPoint
from app.services.signal_repository import SignalRepository
from app.services.strategy_engine import StrategySignal


PRIVATE_KEY_HEX = "00" * 32
BASE_TIME = datetime(2026, 11, 2, 8, 0, tzinfo=UTC)


def isolated_repository(tmp_path: Path) -> SignalRepository:
    database_path = tmp_path / "signals.db"
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "CREATE TABLE signals (id INTEGER PRIMARY KEY AUTOINCREMENT, market TEXT NOT NULL, strategy TEXT NOT NULL, direction TEXT NOT NULL, candle_timestamp TEXT NOT NULL, status TEXT NOT NULL)"
        )
        connection.execute(
            "CREATE UNIQUE INDEX ux_signals ON signals (market, strategy, candle_timestamp)"
        )
    return SignalRepository(database_path)


def service_with(repository: SignalRepository) -> ExnessMarketDataService:
    return ExnessMarketDataService(
        ExnessConfig("key", PRIVATE_KEY_HEX, "123456", "https://gateway.test"),
        strategy_engine=None,
        signal_repository=repository,
    )


def test_stored_bullish_signal_is_returned_with_loaded_candle_price(tmp_path):
    repository = isolated_repository(tmp_path)
    timestamp = BASE_TIME
    signal = StrategySignal("USDJPY", "DayTrading Engulfing", "M5", "bullish", timestamp)
    repository.insert(signal)
    service = service_with(repository)
    service._candles = {int(timestamp.timestamp()): Candle(timestamp, 153.1, 153.5, 153.0, 153.427, None)}
    service._current_signal = {"direction": "bullish", "candle_timestamp": "2026-11-02T08:00:00Z", "price": 153.427}

    snapshot = service.get_signal_snapshot()

    assert snapshot["current_signal"]["direction"] == "bullish"
    assert snapshot["signal_history"][0]["direction"] == "bullish"
    assert snapshot["signal_history"][0]["price"] == 153.427


def test_stored_bearish_signal_is_returned(tmp_path):
    repository = isolated_repository(tmp_path)
    timestamp = BASE_TIME
    repository.insert(StrategySignal("USDJPY", "DayTrading Engulfing", "M5", "bearish", timestamp))

    snapshot = service_with(repository).get_signal_snapshot()

    assert snapshot["signal_history"][0]["direction"] == "bearish"
    assert snapshot["signal_history"][0]["market"] == "USDJPY"
    assert snapshot["signal_history"][0]["strategy"] == "DayTrading Engulfing"


def test_no_active_signal_preserves_historical_signal(tmp_path):
    repository = isolated_repository(tmp_path)
    repository.insert(StrategySignal("USDJPY", "DayTrading Engulfing", "M5", "bullish", BASE_TIME))
    service = service_with(repository)
    service._current_signal = None

    snapshot = service.get_signal_snapshot()

    assert snapshot["current_signal"] is None
    assert len(snapshot["signal_history"]) == 1


def test_signal_history_is_newest_first(tmp_path):
    repository = isolated_repository(tmp_path)
    older = BASE_TIME
    newer = BASE_TIME + timedelta(minutes=5)
    repository.insert(StrategySignal("USDJPY", "DayTrading Engulfing", "M5", "bullish", older))
    repository.insert(StrategySignal("USDJPY", "DayTrading Engulfing", "M5", "bearish", newer))

    history = service_with(repository).get_signal_snapshot()["signal_history"]

    assert [item["candle_timestamp"] for item in history] == [
        "2026-11-02T08:05:00Z",
        "2026-11-02T08:00:00Z",
    ]


def test_missing_optional_history_price_is_safe(tmp_path):
    repository = isolated_repository(tmp_path)
    repository.insert(StrategySignal("USDJPY", "DayTrading Engulfing", "M5", "bullish", BASE_TIME))

    history = service_with(repository).get_signal_snapshot()["signal_history"]

    assert history[0]["price"] is None


def test_signal_details_are_linked_to_previous_indicator_values(tmp_path):
    repository = isolated_repository(tmp_path)
    service = service_with(repository)
    previous = Candle(BASE_TIME, 100.0, 106.0, 99.0, 104.0, None)
    current = Candle(BASE_TIME + timedelta(minutes=5), 110.0, 111.0, 100.0, 101.0, None)
    indicators = [
        IndicatorPoint(previous.timestamp, 105.0, 39.0),
        IndicatorPoint(current.timestamp, 999.0, 99.0),
    ]
    signal = StrategySignal("USDJPY", "DayTrading Engulfing", "M5", "bullish", current.timestamp)

    view = service._build_signal_view(signal, [previous, current], indicators, 1)

    assert view["price"] == 101.0
    assert view["conditions"][0]["reference"] == 100.0
    assert view["conditions"][1]["reference"] == 105.0
    assert view["conditions"][2]["actual"] == 39.0


def test_signal_endpoint_reads_without_refreshing_market_data():
    class FakeService:
        def get_signal_snapshot(self):
            return {"current_signal": None, "signal_history": []}

    market_routes.configure_market_service(FakeService())
    try:
        response = asyncio.run(market_routes.signals())
    finally:
        market_routes.configure_market_service(None)

    assert response == {"current_signal": None, "signal_history": []}


def test_dashboard_refresh_does_not_evaluate_strategy_itself():
    class FakeService:
        def __init__(self):
            self.refreshes = 0
            self.evaluations = 0

        async def get_snapshot(self, refresh=False):
            self.refreshes += int(refresh)
            return {"status": "connected", "market": "USDJPY", "timeframe": "M5", "current_signal": None, "signal_history": []}

        def start_tick_stream(self):
            return None

    fake = FakeService()
    market_routes.configure_market_service(fake)
    try:
        asyncio.run(market_routes.market_data())
        asyncio.run(market_routes.market_data())
    finally:
        market_routes.configure_market_service(None)

    assert fake.refreshes == 2
    assert fake.evaluations == 0


def test_frontend_signal_presentation_uses_kampala_and_backend_signal_fields():
    javascript = Path("static/js/dashboard.js").read_text(encoding="utf-8")
    template = Path("app/templates/dashboard.html").read_text(encoding="utf-8")

    assert "function updateSignalPresentation(data)" in javascript
    assert "data.current_signal" in javascript
    assert "data.signal_history" in javascript
    assert "formatTimestamp(signal.candle_timestamp)" in javascript
    assert "formatTimestamp(record.candle_timestamp)" in javascript
    assert "NO ACTIVE SIGNAL" in template
    assert 'id="signal-history-body"' in template
