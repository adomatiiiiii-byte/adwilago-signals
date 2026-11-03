import asyncio
import base64
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient
from app.services import exness_service as exness_module

from app.main import app
from app.routes import market_routes
from app.services.exness_service import (
    Candle,
    ExnessAuthenticationError,
    ExnessConfig,
    ExnessMarketDataError,
    ExnessMarketDataService,
    InstrumentTradeSession,
    M5_SECONDS,
    REST_STALE_SECONDS,
    TICK_STALE_SECONDS,
    parse_candles,
)
from app.services.indicators import IndicatorPoint


PRIVATE_KEY_HEX = "00" * 32


def config() -> ExnessConfig:
    return ExnessConfig.from_mapping({
        "EXN_API_KEY": "test-api-key",
        "EXN_PRIVATE_KEY": PRIVATE_KEY_HEX,
        "EXN_ACCOUNT_ID": "123456",
        "EXN_DISCOVERY_BASE_URL": "https://gateway.test",
    })


def response_for(request: httpx.Request) -> httpx.Response:
    if request.url.path == "/v1/trading/access-point":
        payload = {"access_point": "https://access-point.test", "account_id": "123456"}
    elif request.url.path.endswith("/account"):
        payload = {"account_id": "123456", "currency": "USD"}
    elif request.url.path.endswith("/instruments"):
        payload = {"instruments": ["EURUSD", "USDJPY"]}
    elif request.url.path.endswith("/candles"):
        payload = {"candles": [{
            "time": "2026-11-02T08:00:00Z", "open": 153.100,
            "high": 153.200, "low": 153.000, "close": 153.150, "volume": 42,
        }]}
    else:
        return httpx.Response(404, json={"code": 2000, "error_message": "ACCOUNT_NOT_FOUND"})
    return httpx.Response(200, json=payload)


def service_with(handler=response_for) -> ExnessMarketDataService:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return ExnessMarketDataService(config(), http_client=client)


def test_missing_credentials_are_rejected_without_values():
    with pytest.raises(Exception) as caught:
        ExnessConfig.from_mapping({"EXN_API_KEY": "", "EXN_PRIVATE_KEY": ""})
    assert "EXN_API_KEY" in str(caught.value)
    assert "EXN_PRIVATE_KEY" in str(caught.value)


def test_authentication_failure_is_safe():
    def denied(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"code": 1001, "error_message": "AUTH_INVALID_SIGNATURE"})

    service = service_with(denied)
    with pytest.raises(ExnessAuthenticationError, match="authentication or permission"):
        asyncio.run(service.discover_access_point())
    asyncio.run(service.close())


def test_successful_signed_authentication_and_market_data():
    service = service_with()
    snapshot = asyncio.run(service.get_snapshot())
    assert snapshot["status"] == "connected"
    assert snapshot["market"] == "USDJPY"
    assert snapshot["timeframe"] == "M5"
    assert snapshot["price"] == 153.15
    assert set(snapshot["latest_indicators"]) == {"time", "timestamp", "ma", "rsi"}
    assert snapshot["health"]["rest_status"] == "connected"
    assert snapshot["health"]["latest_reconciliation_new_completed_candle"] is True
    asyncio.run(service.close())


def test_signed_headers_use_ed25519_and_exact_path():
    captured = {}

    def inspect(request: httpx.Request) -> httpx.Response:
        captured.update(dict(request.headers))
        payload = base64.urlsafe_b64decode(captured["exn-data"] + "==")
        decoded = json.loads(payload)
        assert decoded["method"] == "GET"
        assert decoded["path"] == request.url.raw_path.decode()
        public_key = Ed25519PrivateKey.from_private_bytes(bytes(32)).public_key()
        public_key.verify(base64.urlsafe_b64decode(captured["exn-sign"] + "=="), payload)
        return httpx.Response(200, json={"access_point": "https://access-point.test"})

    service = service_with(inspect)
    assert asyncio.run(service.discover_access_point()) == "https://access-point.test"
    assert captured["exn-api-key"] == "test-api-key"
    asyncio.run(service.close())


def test_usdjpy_must_exist_in_account_instrument_list():
    def no_usdjpy(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/instruments"):
            return httpx.Response(200, json={"instruments": ["EURUSD"]})
        return response_for(request)

    service = service_with(no_usdjpy)
    with pytest.raises(ExnessMarketDataError, match="USDJPY is not available"):
        asyncio.run(service.verify_account_and_instruments())
    asyncio.run(service.close())


def test_historical_m5_candle_is_parsed_as_utc_ohlc():
    candle = parse_candles({"candles": [{
        "time": "2026-11-02T08:00:00+03:00", "open": 153.1,
        "high": 153.2, "low": 153.0, "close": 153.15,
    }]})[0]
    assert candle.timestamp.isoformat() == "2026-11-02T05:00:00+00:00"
    assert candle.as_chart_point()["open"] == 153.1


def test_invalid_market_data_response_is_rejected():
    with pytest.raises(ExnessMarketDataError, match="invalid candle response"):
        parse_candles({"unexpected": []})


def test_dashboard_status_route_reports_connected_state():
    class FakeService:
        async def get_snapshot(self, refresh=False):
            return {"status": "connected", "market": "USDJPY", "timeframe": "M5"}

        def start_tick_stream(self):
            return None

    market_routes.configure_market_service(FakeService())
    payload = asyncio.run(market_routes.market_data())
    assert payload["status"] == "connected"
    assert payload["market"] == "USDJPY"
    market_routes.configure_market_service(None)


def test_dashboard_route_requests_a_rest_refresh_each_time():
    class FakeService:
        def __init__(self):
            self.refresh_values = []

        async def get_snapshot(self, refresh=False):
            self.refresh_values.append(refresh)
            return {"status": "connected", "market": "USDJPY", "timeframe": "M5"}

        def start_tick_stream(self):
            return None

    fake = FakeService()
    market_routes.configure_market_service(fake)
    try:
        asyncio.run(market_routes.market_data())
        asyncio.run(market_routes.market_data())
        assert fake.refresh_values == [True, True]
    finally:
        market_routes.configure_market_service(None)


def test_dashboard_websocket_stream_sends_snapshot_without_credentials():
    class FakeDashboardService:
        def __init__(self):
            self.queue = asyncio.Queue()

        async def get_snapshot(self):
            return {"status": "connected", "rest_status": "connected", "websocket_status": "connected", "market": "USDJPY", "timeframe": "M5", "chart_candles": []}

        def subscribe_dashboard(self):
            return self.queue

        def unsubscribe_dashboard(self, queue):
            return None

        def start_tick_stream(self):
            return None

    fake = FakeDashboardService()
    market_routes.configure_market_service(fake)
    try:
        with TestClient(app) as client:
            with client.websocket_connect("/ws/market-data") as websocket:
                payload = websocket.receive_json()
                assert payload["type"] == "snapshot"
                assert payload["market"] == "USDJPY"
    finally:
        market_routes.configure_market_service(None)


def test_dashboard_loads_with_stage_one_state():
    with TestClient(app) as client:
        response = client.get("/")

    assert response.status_code == 200
    assert "ADWILAGO TRADING SIGNALS" in response.text
    assert "USDJPY" in response.text
    assert "NO ACTIVE SIGNAL" in response.text
    assert "Disconnected" in response.text
    assert 'id="fixed-kampala-clock"' in response.text
    assert 'id="fixed-kampala-time"' in response.text
    assert 'dashboard.js?v=stage-4-6b-2' in response.text


def test_fixed_clock_uses_kampala_timezone_and_updates_independently():
    javascript = Path("static/js/dashboard.js").read_text(encoding="utf-8")
    stylesheet = Path("static/css/style.css").read_text(encoding="utf-8")
    assert "timeZone: DISPLAY_TIME_ZONE" in javascript
    assert "const fixedClock = document.getElementById(\"fixed-kampala-time\")" in javascript
    assert "if (fixedClock) fixedClock.textContent = currentTime" in javascript
    assert "function initializeClock()" in javascript
    assert "window.setInterval(updateClock, 1000)" in javascript
    assert ".fixed-kampala-clock { position: fixed !important;" in stylesheet
    assert "visibility: visible !important" in stylesheet
    assert "opacity: 1 !important" in stylesheet
    assert "right: 20px !important; bottom: 20px !important" in stylesheet


class FakeWebSocket:
    def __init__(self, messages):
        self.messages = messages
        self.sent = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    async def send(self, message):
        self.sent.append(json.loads(message))

    def __aiter__(self):
        return self._messages()

    async def _messages(self):
        for message in self.messages:
            yield json.dumps(message)


def test_websocket_tick_updates_price_and_resubscribes_after_reconnect():
    connections = [
        FakeWebSocket([{"instrument": "USDJPY", "bid": 153.1, "ask": 153.2, "timestamp": "2026-11-02T08:00:00Z"}]),
        FakeWebSocket([{"instrument": "USDJPY", "bid": 153.3, "ask": 153.4, "timestamp": "2026-11-02T08:00:01Z"}]),
    ]
    used_connections = []
    delays = []

    async def fake_sleep(delay):
        delays.append(delay)
        if len(delays) == 2:
            service._closing = True

    def connector(url, **kwargs):
        connection = connections.pop(0)
        connection.connector_kwargs = kwargs
        used_connections.append(connection)
        return connection

    service = service_with()
    service._access_point = "https://access-point.test"
    service._websocket_connector = connector
    service._sleep = fake_sleep
    asyncio.run(service._run_tick_stream())

    assert service._latest_tick["bid"] == 153.3
    assert service._websocket_activity == "disconnected"
    assert delays == [1.0, 1.0]
    assert connections == []
    assert len(used_connections) == 2
    assert all(connection.sent[0]["subscribe"]["instruments"] == ["USDJPY"] for connection in used_connections)
    assert all(connection.connector_kwargs["subprotocols"] == ["exness-ws-protocol"] for connection in used_connections)
    asyncio.run(service.close())


def test_websocket_reports_connected_before_first_valid_tick():
    published = []
    service = service_with()
    service._access_point = "https://access-point.test"
    service._websocket_connector = lambda url, **kwargs: FakeWebSocket([])

    async def capture(message):
        published.append(message)

    async def stop_after_close(delay):
        service._closing = True

    service._publish = capture
    service._sleep = stop_after_close
    asyncio.run(service._run_tick_stream())

    health_states = [message["health"]["websocket_activity"] for message in published if "health" in message]
    assert "awaiting_first_tick" in health_states
    assert service._websocket_status == "disconnected"
    asyncio.run(service.close())


def test_silent_websocket_is_detected_without_forcing_market_closure_reconnect(monkeypatch):
    published = []
    connections = []

    class QuietAfterTick:
        def __init__(self):
            self.index = 0

        async def send(self, message):
            return None

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return False

        def __aiter__(self):
            return self

        async def __anext__(self):
            if self.index == 0:
                self.index += 1
                return json.dumps({"instrument": "USDJPY", "bid": 153.1, "ask": 153.2})
            if self.index == 1:
                self.index += 1
                await asyncio.sleep(60)
            raise StopAsyncIteration

    def connector(url, **kwargs):
        connections.append(url)
        return QuietAfterTick()

    service = service_with()
    service._access_point = "https://access-point.test"
    service._websocket_connector = connector
    monkeypatch.setattr(exness_module, "TICK_STALE_SECONDS", 0.001)

    async def capture(message):
        published.append(message)

    service._publish = capture

    async def stop_after_backoff(delay):
        service._closing = True

    service._sleep = stop_after_backoff
    asyncio.run(service._run_tick_stream())

    assert len(connections) == 1
    assert any(message.get("health", {}).get("websocket_activity") == "silent" for message in published)
    asyncio.run(service.close())


def test_normal_websocket_closure_is_reported_before_reconnect():
    published = []
    service = service_with()
    service._access_point = "https://access-point.test"
    service._websocket_connector = lambda url, **kwargs: FakeWebSocket([])

    async def capture(message):
        published.append(message)

    service._publish = capture

    async def stop_after_close(delay):
        service._closing = True

    service._sleep = stop_after_close
    asyncio.run(service._run_tick_stream())

    assert any(message.get("websocket_error") == "Exness tick stream closed normally." for message in published)
    assert service._websocket_status == "disconnected"
    asyncio.run(service.close())


def test_websocket_backoff_is_capped_at_configured_maximum():
    delays = []

    def connector(url, **kwargs):
        raise RuntimeError("simulated connection failure")

    async def fake_sleep(delay):
        delays.append(delay)
        if len(delays) == 6:
            service._closing = True

    service = service_with()
    service._access_point = "https://access-point.test"
    service._websocket_connector = connector
    service._sleep = fake_sleep
    asyncio.run(service._run_tick_stream())

    assert delays == [1.0, 2.0, 4.0, 8.0, 16.0, 30.0]
    asyncio.run(service.close())


def test_websocket_backoff_increases_after_repeated_failures():
    delays = []

    def connector(url, **kwargs):
        raise RuntimeError("simulated connection failure")

    async def fake_sleep(delay):
        delays.append(delay)
        if len(delays) == 3:
            service._closing = True

    service = service_with()
    service._access_point = "https://access-point.test"
    service._websocket_connector = connector
    service._sleep = fake_sleep
    asyncio.run(service._run_tick_stream())

    assert delays == [1.0, 2.0, 4.0]
    asyncio.run(service.close())


def test_websocket_disconnect_reports_state_and_uses_backoff():
    attempts = []
    delays = []

    def connector(url, **kwargs):
        attempts.append(url)
        raise RuntimeError("simulated disconnect")

    async def fake_sleep(delay):
        delays.append(delay)
        service._closing = True

    service = service_with()
    service._access_point = "https://access-point.test"
    service._websocket_connector = connector
    service._sleep = fake_sleep
    asyncio.run(service._run_tick_stream())

    assert len(attempts) == 1
    assert service._websocket_status == "disconnected"
    assert service._websocket_error == "Exness tick stream disconnected."
    assert delays == [1.0]
    asyncio.run(service.close())


def test_candle_reconciliation_replaces_overlap_without_duplicates():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/candles"):
            calls.append(dict(request.url.params))
            if len(calls) == 1:
                candles = [{"time": "2026-11-02T08:00:00Z", "open": 153.1, "high": 153.2, "low": 153.0, "close": 153.15}]
            else:
                candles = [
                    {"time": "2026-11-02T08:00:00Z", "open": 153.1, "high": 153.3, "low": 153.0, "close": 153.25},
                    {"time": "2026-11-02T08:05:00Z", "open": 153.25, "high": 153.4, "low": 153.2, "close": 153.35},
                ]
            return httpx.Response(200, json={"candles": candles})
        return response_for(request)

    service = service_with(handler)
    asyncio.run(service.get_snapshot())
    asyncio.run(service.reconcile_candles())

    timestamps = [candle.timestamp for candle in service._completed_candles()]
    assert len(timestamps) == len(set(timestamps))
    assert len(service._candles) == 2
    assert service._candles[int(timestamps[0].timestamp())].close == 153.25
    assert calls[1]["count"] == "5"
    assert service.get_health_snapshot()["latest_reconciliation_new_completed_candle"] is True
    asyncio.run(service.close())


def test_successful_rest_reconciliation_without_new_candle_reports_false():
    service = service_with()
    asyncio.run(service.get_snapshot())
    asyncio.run(service.reconcile_candles())

    assert service.get_health_snapshot()["latest_reconciliation_new_completed_candle"] is False
    asyncio.run(service.close())


def _set_session_schedule(service, *sessions):
    service._trade_mode = "enabled"
    service._trade_sessions = tuple(sessions)
    service._session_information_available = True
    service._session_override = None


def test_instrument_conditions_are_parsed_and_cached():
    condition_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal condition_calls
        if request.url.path.endswith("/instruments/USDJPY/conditions"):
            condition_calls += 1
            return httpx.Response(200, json={
                "trade_mode": "enabled",
                "trade_sessions": [{
                    "trade_mode": "enabled",
                    "week_start_time": "Mon, 00:00:00",
                    "week_end_time": "Fri, 23:59:59",
                }],
            })
        return response_for(request)

    service = service_with(handler)
    service._access_point = "https://access-point.test"
    asyncio.run(service._refresh_instrument_conditions())
    asyncio.run(service._refresh_instrument_conditions())

    assert condition_calls == 1
    assert service._session_information_available is True
    assert service._trade_mode == "enabled"
    assert service._current_session_state(datetime(2026, 11, 3, 12, 0, tzinfo=UTC)) == "open"
    assert service._current_session_state(datetime(2026, 11, 7, 12, 0, tzinfo=UTC)) == "closed"
    asyncio.run(service.close())


def test_one_missing_interval_during_open_session_is_detected():
    service = service_with()
    _set_session_schedule(service, InstrumentTradeSession(0, 4 * 86400 + 86399))
    before = {datetime(2026, 11, 3, 10, 0, tzinfo=UTC)}
    after = before | {datetime(2026, 11, 3, 10, 10, tzinfo=UTC)}

    service._record_candle_reconciliation(before, after)
    health = service.get_health_snapshot()

    assert health["candle_sequence_state"] == "gap_detected"
    assert health["gap_detected"] is True
    assert health["missing_intervals"][0]["count"] == 1
    asyncio.run(service.close())


def test_multiple_missing_intervals_are_reported():
    service = service_with()
    _set_session_schedule(service, InstrumentTradeSession(0, 4 * 86400 + 86399))
    before = {datetime(2026, 11, 3, 10, 0, tzinfo=UTC)}
    after = before | {datetime(2026, 11, 3, 10, 20, tzinfo=UTC)}

    service._record_candle_reconciliation(before, after)

    assert service._recent_missing_intervals[0]["count"] == 3
    assert service._recent_gap_state == "gap_detected"
    asyncio.run(service.close())


def test_weekend_gap_is_classified_as_market_closed():
    service = service_with()
    _set_session_schedule(service, InstrumentTradeSession(0, 4 * 86400 + 86399))
    before = {datetime(2026, 11, 6, 23, 55, tzinfo=UTC)}
    after = before | {datetime(2026, 11, 7, 0, 10, tzinfo=UTC)}

    service._record_candle_reconciliation(before, after)

    assert service._recent_gap_state == "market_closed"
    assert service._recent_missing_intervals[0]["session_state"] == "market_closed"
    asyncio.run(service.close())


def test_scheduled_session_break_is_not_reported_as_missing_candle():
    service = service_with()
    sessions = []
    for day in range(5):
        day_start = day * 86400
        sessions.extend([
            InstrumentTradeSession(day_start, day_start + 10 * 3600 - 1),
            InstrumentTradeSession(day_start + 11 * 3600, day_start + 86399),
        ])
    _set_session_schedule(service, *sessions)
    before = {datetime(2026, 11, 3, 10, 0, tzinfo=UTC)}
    after = before | {datetime(2026, 11, 3, 11, 0, tzinfo=UTC)}

    service._record_candle_reconciliation(before, after)

    assert service._recent_gap_state == "market_closed"
    assert service._recent_missing_intervals[0]["session_state"] == "market_closed"
    asyncio.run(service.close())


def test_unknown_session_information_does_not_confirm_a_gap():
    service = service_with()
    before = {datetime(2026, 11, 3, 10, 0, tzinfo=UTC)}
    after = before | {datetime(2026, 11, 3, 10, 10, tzinfo=UTC)}

    service._record_candle_reconciliation(before, after)

    assert service._recent_gap_state == "unknown"
    health = service.get_health_snapshot()
    assert health["gap_detected"] is False
    asyncio.run(service.close())


def test_malformed_session_information_is_safe_and_unavailable():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/instruments/USDJPY/conditions"):
            return httpx.Response(200, json={"trade_sessions": "not-a-list"})
        return response_for(request)

    service = service_with(handler)
    service._access_point = "https://access-point.test"
    asyncio.run(service._refresh_instrument_conditions())

    assert service._session_information_available is False
    assert service._current_session_state(datetime.now(UTC)) == "unknown"
    asyncio.run(service.close())


def test_revised_ohlc_with_existing_timestamp_is_not_new_candle():
    service = service_with()
    timestamp = datetime(2026, 11, 3, 10, 0, tzinfo=UTC)
    service._record_candle_reconciliation({timestamp}, {timestamp})

    assert service._last_reconciliation_new_completed_candle is False
    assert service._last_new_completed_candle_at is None
    asyncio.run(service.close())


def test_initial_historical_gap_is_ignored_for_live_health():
    service = service_with()
    initial = {
        datetime(2026, 11, 3, 10, 0, tzinfo=UTC),
        datetime(2026, 11, 3, 10, 20, tzinfo=UTC),
    }

    service._record_candle_reconciliation(set(), initial)

    assert service._recent_missing_intervals == []
    assert service._recent_gap_state is None
    asyncio.run(service.close())


def test_rest_refresh_takes_over_when_websocket_is_disconnected():
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        if request.url.path.endswith("/candles"):
            calls += 1
            close = 153.15 if calls == 1 else 153.25
            return httpx.Response(200, json={"candles": [{
                "time": "2026-11-02T08:00:00Z", "open": 153.1,
                "high": 153.3, "low": 153.0, "close": close,
            }]})
        return response_for(request)

    service = service_with(handler)
    asyncio.run(service.get_snapshot())
    service._latest_tick = {"bid": 154.0, "ask": 154.1, "timestamp": "2026-11-02T08:01:00Z"}
    service._latest_tick_received_at = datetime.now(UTC)
    service._websocket_status = "disconnected"

    snapshot = asyncio.run(service.refresh_market_data())

    assert snapshot["price"] == 153.25
    assert snapshot["price_source"] == "latest_candle_close"
    assert snapshot["chart_candles"][-1]["close"] == 153.25
    asyncio.run(service.close())


def _health_service() -> ExnessMarketDataService:
    service = service_with()
    now = datetime.now(UTC)
    candle = Candle(now - timedelta(minutes=5, seconds=30), 153.1, 153.2, 153.0, 153.15, None)
    service._candles = {int(candle.timestamp.timestamp()): candle}
    service._candles_loaded = True
    service._rest_status = "connected"
    service._last_successful_update = now - timedelta(seconds=5)
    return service


def test_health_reports_healthy_rest_and_active_websocket():
    service = _health_service()
    now = datetime.now(UTC)
    service._websocket_status = "connected"
    service._latest_tick = {"timestamp": "2026-11-03T08:00:00Z", "bid": 153.1, "ask": 153.2}
    service._latest_tick_received_at = now - timedelta(seconds=2)

    health = service.get_health_snapshot(now=now)

    assert health["overall_state"] == "healthy"
    assert health["rest_status"] == "connected"
    assert health["websocket_activity"] == "active"
    assert health["latest_completed_candle_timestamp"] is not None
    assert health["latest_reconciliation_new_completed_candle"] is None
    asyncio.run(service.close())


def test_health_reports_rest_healthy_while_websocket_is_disconnected():
    service = _health_service()

    health = service.get_health_snapshot()

    assert health["overall_state"] == "degraded"
    assert health["rest_freshness"] == "fresh"
    assert health["websocket_activity"] == "disconnected"
    asyncio.run(service.close())


def test_health_reports_connected_websocket_without_a_valid_tick():
    service = _health_service()
    service._websocket_status = "connected"
    service._websocket_activity = "awaiting_first_tick"

    health = service.get_health_snapshot()

    assert health["websocket_activity"] == "awaiting_first_tick"
    assert health["overall_state"] == "degraded"
    asyncio.run(service.close())


def test_health_reports_stale_last_tick_without_changing_connection_state():
    service = _health_service()
    now = datetime.now(UTC)
    service._websocket_status = "connected"
    service._websocket_activity = "active"
    service._latest_tick = {"timestamp": "2026-11-03T08:00:00Z", "bid": 153.1, "ask": 153.2}
    service._latest_tick_received_at = now - timedelta(seconds=TICK_STALE_SECONDS + 1)

    health = service.get_health_snapshot(now=now)

    assert health["websocket_status"] == "connected"
    assert health["websocket_activity"] == "silent"
    assert health["tick_age_seconds"] > TICK_STALE_SECONDS
    asyncio.run(service.close())


def test_successful_rest_reconciliation_reports_whether_completed_sequence_advanced(monkeypatch):
    service = _health_service()
    service._last_reconciliation_new_completed_candle = False
    stable = service.get_health_snapshot()
    assert stable["latest_reconciliation_new_completed_candle"] is False

    service._last_reconciliation_new_completed_candle = True
    advanced = service.get_health_snapshot()
    assert advanced["latest_reconciliation_new_completed_candle"] is True
    asyncio.run(service.close())


def test_health_reports_stale_completed_candle_data():
    service = service_with()
    candle = Candle(datetime.now(UTC) - timedelta(seconds=2 * M5_SECONDS + REST_STALE_SECONDS + 1), 153.1, 153.2, 153.0, 153.15, None)
    service._candles = {int(candle.timestamp.timestamp()): candle}
    service._candles_loaded = True
    service._rest_status = "connected"
    service._last_successful_update = datetime.now(UTC)

    health = service.get_health_snapshot()

    assert health["candle_freshness"] == "stale"
    assert health["overall_state"] == "stale"
    asyncio.run(service.close())


def test_unconfigured_market_data_response_reports_unavailable_health():
    market_routes.configure_market_service(None)
    try:
        payload = asyncio.run(market_routes.market_data())
        assert payload["health"]["overall_state"] == "unavailable"
        assert payload["health"]["rest_status"] == "disconnected"
    finally:
        market_routes.configure_market_service(None)


def test_frontend_rest_fallback_interval_and_updates_are_present():
    javascript = Path("static/js/dashboard.js").read_text(encoding="utf-8")
    assert "setInterval(loadMarketData, 15000)" in javascript
    assert "applyMarketData(await response.json())" in javascript
    assert "updateMainChartData(data.chart_candles" in javascript
    assert "formatPrice(data.price)" in javascript


def test_frontend_preserves_three_decimal_usdjpy_price_precision():
    javascript = Path("static/js/dashboard.js").read_text(encoding="utf-8")
    assert 'const USDJPY_PRICE_FORMAT = { type: "price", precision: 3, minMove: 0.001 }' in javascript
    assert "priceFormat: USDJPY_PRICE_FORMAT" in javascript
    assert javascript.count("priceFormat: USDJPY_PRICE_FORMAT") == 2
    assert "formatPrice(candle.open)" in javascript
    assert "formatPrice(candle.high)" in javascript
    assert "formatPrice(candle.low)" in javascript
    assert "formatPrice(candle.close)" in javascript
    assert "document.getElementById(\"current-price\").textContent = formatPrice(data.bid)" in javascript
    assert "document.getElementById(\"current-price\").textContent = formatPrice(data.price)" in javascript


def test_completed_rsi_display_uses_two_decimal_strategy_value():
    javascript = Path("static/js/dashboard.js").read_text(encoding="utf-8")
    assert "function formatRsiValue(value)" in javascript
    assert "value.toFixed(2)" in javascript
    assert 'document.getElementById("latest-rsi").textContent = formatRsiValue(latest.rsi)' in javascript


def test_snapshot_keeps_historical_rsi_raw_but_rounds_strategy_facing_latest_value(monkeypatch):
    service = service_with()
    candle = Candle(datetime(2026, 11, 2, 8, 0, tzinfo=UTC), 153.1, 153.2, 153.0, 153.15, None)
    service._candles = {int(candle.timestamp.timestamp()): candle}
    service._candles_loaded = True
    service._rest_status = "connected"
    service._last_successful_update = datetime.now(UTC)
    monkeypatch.setattr(
        "app.services.exness_service.calculate_indicators",
        lambda candles: [IndicatorPoint(candle.timestamp, 153.1, 38.45672)],
    )

    snapshot = service._dashboard_snapshot()

    assert snapshot["indicators"][0]["rsi"] == pytest.approx(38.45672)
    assert snapshot["latest_indicators"]["rsi"] == pytest.approx(38.46)


def test_rsi_chart_has_only_the_rsi_series_without_visual_threshold_lines():
    javascript = Path("static/js/dashboard.js").read_text(encoding="utf-8")
    template = Path("app/templates/dashboard.html").read_text(encoding="utf-8")
    assert "rsiSeries.createPriceLine" not in javascript
    assert "price: 40" not in javascript
    assert "price: 60" not in javascript
    assert 'title: "RSI 7"' in javascript
    assert "Reference levels 40 / 60" not in template


def test_strategy_thresholds_remain_in_strategy_display():
    template = Path("app/templates/dashboard.html").read_text(encoding="utf-8")
    assert "Previous candle RSI &lt; 40" in template
    assert "Previous candle RSI &gt; 60" in template


def test_frontend_chart_updates_preserve_user_ranges_and_follow_latest_only_when_selected():
    javascript = Path("static/js/dashboard.js").read_text(encoding="utf-8")
    assert "subscribeVisibleLogicalRangeChange" in javascript
    assert "getVisibleLogicalRange()" in javascript
    assert "scrollToRealTime()" in javascript
    assert javascript.count("fitContent()") == 2
    assert "mainFollowsLatest" in javascript
    assert "rsiFollowsLatest" in javascript


def test_frontend_renders_only_unsmoothed_ma_and_rsi_series():
    javascript = Path("static/js/dashboard.js").read_text(encoding="utf-8")
    template = Path("app/templates/dashboard.html").read_text(encoding="utf-8")
    assert "maSmoothingSeries" not in javascript
    assert "rsiSmoothingSeries" not in javascript
    assert "ma_smoothing" not in javascript
    assert "rsi_smoothing" not in javascript
    assert "MA SMA 9" not in javascript
    assert "RSI SMA 9" not in javascript
    assert "latest-ma-smoothing" not in template
    assert "latest-rsi-smoothing" not in template


def test_frontend_updates_rsi_panel_when_candle_data_is_present():
    javascript = Path("static/js/dashboard.js").read_text(encoding="utf-8")
    market_data_block = javascript[javascript.index("function applyMarketData"):javascript.index("async function loadMarketData")]
    assert "updateMainChartData(data.chart_candles" in market_data_block
    assert "updateIndicatorCharts(data);" in market_data_block
    assert market_data_block.index("updateMainChartData") < market_data_block.index("updateIndicatorCharts")
    assert "updateRsiChartData(points)" in javascript
