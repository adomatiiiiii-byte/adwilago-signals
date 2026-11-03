"""Read-only Exness Trader API market-data integration.

This module intentionally contains no trading-operation methods. Every Exness
request is signed with the documented Ed25519 request-signing protocol.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import json
import logging
import math
import os
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import urlencode, urlparse

import httpx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from dotenv import load_dotenv

from app.services.indicators import IndicatorDataError, calculate_indicators, round_rsi_for_strategy
from app.services.signal_repository import SignalRepository
from app.services.strategy_engine import DayTradingEngulfingStrategy, StrategySignal

try:
    import websockets
except ImportError:  # pragma: no cover - reported as a feature error at runtime.
    websockets = None


LOGGER = logging.getLogger(__name__)
MARKET = "USDJPY"
TIMEFRAME = "M5"
HISTORY_COUNT = 250
REQUEST_TIMEOUT_SECONDS = 15.0
CANDLE_REFRESH_SECONDS = 60.0
M5_SECONDS = 5 * 60
MAX_RECONNECT_DELAY_SECONDS = 30.0
REST_STALE_SECONDS = 180.0
TICK_STALE_SECONDS = 30.0
SESSION_REFRESH_SECONDS = 15 * 60
WEBSOCKET_PING_INTERVAL_SECONDS = 20.0
WEBSOCKET_PING_TIMEOUT_SECONDS = 10.0


class ExnessError(Exception):
    """A safe, user-facing Exness integration error."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class ExnessConfigurationError(ExnessError):
    """The local environment is incomplete or invalid."""


class ExnessAuthenticationError(ExnessError):
    """The API rejected the request credentials or signature."""


class ExnessMarketDataError(ExnessError):
    """The API returned an invalid or unusable market-data response."""


class ExnessMarketClosedError(ExnessError):
    """Exness explicitly reported that the instrument session is closed."""


@dataclass(frozen=True)
class InstrumentTradeSession:
    """A weekly UTC instrument session represented as seconds since Monday."""

    start_second: int
    end_second: int


@dataclass(frozen=True)
class ExnessConfig:
    """Credentials and host configuration loaded from the local environment."""

    api_key: str
    private_key: str
    account_id: str
    discovery_base_url: str

    @classmethod
    def from_env(cls, env_path: Path | None = None) -> "ExnessConfig":
        load_dotenv(dotenv_path=env_path)
        return cls.from_mapping(os.environ)

    @classmethod
    def from_mapping(cls, values: Any) -> "ExnessConfig":
        names = {
            "api_key": "EXN_API_KEY",
            "private_key": "EXN_PRIVATE_KEY",
            "account_id": "EXN_ACCOUNT_ID",
            "discovery_base_url": "EXN_DISCOVERY_BASE_URL",
        }
        missing = [name for field, name in names.items() if not values.get(name, "").strip()]
        if missing:
            raise ExnessConfigurationError(
                "Missing Exness configuration: " + ", ".join(missing) + "."
            )

        account_id = values[names["account_id"]].strip()
        if not account_id.isdigit():
            raise ExnessConfigurationError("EXN_ACCOUNT_ID must contain digits only.")

        discovery_base_url = values[names["discovery_base_url"]].strip().rstrip("/")
        parsed_url = urlparse(discovery_base_url)
        if parsed_url.scheme != "https" or not parsed_url.netloc:
            raise ExnessConfigurationError(
                "EXN_DISCOVERY_BASE_URL must be a valid HTTPS URL."
            )

        return cls(
            api_key=values[names["api_key"]].strip(),
            private_key=values[names["private_key"]].strip(),
            account_id=account_id,
            discovery_base_url=discovery_base_url,
        )


@dataclass(frozen=True)
class Candle:
    """A validated Exness candle in UTC."""

    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float | None

    def as_chart_point(self) -> dict[str, Any]:
        return {
            "time": int(self.timestamp.timestamp()),
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
        }

    def as_dict(self) -> dict[str, Any]:
        result = self.as_chart_point()
        result["timestamp"] = self.timestamp.isoformat().replace("+00:00", "Z")
        result["volume"] = self.volume
        return result


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _private_key_from_value(value: str) -> Ed25519PrivateKey:
    """Load either Exness's 128-character hex key or an unencrypted PEM key."""
    try:
        if value.startswith("-----BEGIN"):
            key = serialization.load_pem_private_key(value.encode(), password=None)
            if not isinstance(key, Ed25519PrivateKey):
                raise ValueError
            return key

        raw = bytes.fromhex(value)
        if len(raw) == 64:
            raw = raw[:32]
        if len(raw) != 32:
            raise ValueError
        return Ed25519PrivateKey.from_private_bytes(raw)
    except (ValueError, TypeError, binascii.Error) as exc:
        raise ExnessConfigurationError(
            "EXN_PRIVATE_KEY is not a supported Ed25519 private-key format."
        ) from exc


def parse_candles(payload: Any) -> list[Candle]:
    """Validate and parse the documented candle response without fabricating data."""
    if not isinstance(payload, dict) or not isinstance(payload.get("candles"), list):
        raise ExnessMarketDataError("Exness returned an invalid candle response.")
    if not payload["candles"]:
        raise ExnessMarketDataError("Exness returned no USDJPY M5 candles.")

    candles: list[Candle] = []
    for raw in payload["candles"]:
        if not isinstance(raw, dict):
            raise ExnessMarketDataError("Exness returned an invalid candle item.")
        try:
            timestamp_value = raw["time"]
            timestamp = datetime.fromisoformat(str(timestamp_value).replace("Z", "+00:00"))
            timestamp = timestamp.astimezone(UTC)
            values = [float(raw[field]) for field in ("open", "high", "low", "close")]
            volume = float(raw["volume"]) if raw.get("volume") is not None else None
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            raise ExnessMarketDataError("Exness returned an invalid candle item.") from exc

        all_values = (*values, volume) if volume is not None else tuple(values)
        if any(not math.isfinite(value) for value in all_values):
            raise ExnessMarketDataError("Exness returned a non-finite candle value.")
        candles.append(Candle(timestamp, *values, volume))

    return candles


class ExnessMarketDataService:
    """Read-only service for the configured account's USDJPY market data."""

    def __init__(
        self,
        config: ExnessConfig,
        http_client: httpx.AsyncClient | None = None,
        websocket_connector: Callable[..., Any] | None = None,
        reconnect_base_seconds: float = 1.0,
        reconnect_max_seconds: float = MAX_RECONNECT_DELAY_SECONDS,
        sleep: Callable[[float], Awaitable[None]] | None = None,
        strategy_engine: DayTradingEngulfingStrategy | None = None,
        signal_repository: SignalRepository | None = None,
    ) -> None:
        self.config = config
        self._private_key = _private_key_from_value(config.private_key)
        self._http = http_client or httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS)
        self._owns_http = http_client is None
        self._access_point: str | None = None
        self._instruments: tuple[str, ...] | None = None
        self._account_verified = False
        self._trade_sessions: tuple[InstrumentTradeSession, ...] = ()
        self._trade_mode: str | None = None
        self._session_information_loaded_at: datetime | None = None
        self._session_information_available = False
        self._session_error: str | None = None
        self._session_override: str | None = None
        self._latest_tick: dict[str, Any] | None = None
        self._latest_tick_received_at: datetime | None = None
        self._stream_task: asyncio.Task[None] | None = None
        self._candle_task: asyncio.Task[None] | None = None
        self._candles: dict[int, Candle] = {}
        self._candles_loaded = False
        self._rest_status = "disconnected"
        self._rest_error: str | None = None
        self._websocket_status = "disconnected"
        self._websocket_activity = "disconnected"
        self._current_connection_received_tick = False
        self._websocket_error: str | None = None
        self._last_successful_update: datetime | None = None
        self._last_reconciliation_new_completed_candle: bool | None = None
        self._last_new_completed_candle_at: datetime | None = None
        self._recent_missing_intervals: list[dict[str, Any]] = []
        self._recent_gap_state: str | None = None
        self._dashboard_subscribers: set[asyncio.Queue[dict[str, Any]]] = set()
        self._refresh_lock = asyncio.Lock()
        self._strategy_lock = asyncio.Lock()
        self._strategy_engine = strategy_engine
        self._signal_repository = signal_repository
        self._last_strategy_candle_timestamp: datetime | None = None
        self._current_signal: dict[str, Any] | None = None
        self._closing = False
        self._reconnect_base_seconds = reconnect_base_seconds
        self._reconnect_max_seconds = reconnect_max_seconds
        self._sleep = sleep or asyncio.sleep
        self._websocket_connector = websocket_connector or (
            websockets.connect if websockets is not None else None
        )

    def _headers(self, method: str, path: str, body: bytes = b"") -> dict[str, str]:
        timestamp = int(time.time() * 1000)
        idempotency_key = ""
        payload = {
            "api_key": self.config.api_key,
            "idempotency_key": idempotency_key,
            "timestamp": timestamp,
            "sign_version": 1,
            "method": method.upper(),
            "path": path,
            "body_hash": _b64url(hashlib.sha256(body).digest()),
        }
        encoded_payload = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        signature = self._private_key.sign(encoded_payload)
        return {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "EXN-API-KEY": self.config.api_key,
            "EXN-IDEMPOTENCY-KEY": "",
            "EXN-TIMESTAMP": str(timestamp),
            "EXN-SIGN-VERSION": "1",
            "EXN-DATA": _b64url(encoded_payload),
            "EXN-SIGN": _b64url(signature),
        }

    async def _get(self, base_url: str, path: str, params: dict[str, Any] | None = None) -> Any:
        query = urlencode(params or {})
        request_path = f"{path}?{query}" if query else path
        try:
            response = await self._http.get(
                f"{base_url.rstrip('/')}{request_path}",
                headers=self._headers("GET", request_path),
            )
        except httpx.TimeoutException as exc:
            raise ExnessError("Exness API request timed out.") from exc
        except httpx.HTTPError as exc:
            raise ExnessError("Exness API is unavailable.") from exc

        try:
            payload = response.json()
        except ValueError as exc:
            raise ExnessError("Exness returned a non-JSON response.") from exc

        if response.status_code >= 400:
            error_name = payload.get("error_message") if isinstance(payload, dict) else None
            code = payload.get("code") if isinstance(payload, dict) else None
            if code in {1000, 1001, 1002}:
                raise ExnessAuthenticationError(
                    "Exness authentication or permission failed.",
                    status_code=response.status_code,
                )
            if code == 4000:
                raise ExnessMarketDataError(
                    "USDJPY was not found for this account.",
                    status_code=response.status_code,
                )
            if code in {4001, 4002, 4003}:
                raise ExnessMarketClosedError(
                    "Exness reported that the USDJPY market session is closed.",
                    status_code=response.status_code,
                )
            raise ExnessError(
                "Exness API request failed.",
                status_code=response.status_code,
            )
        return payload

    async def discover_access_point(self) -> str:
        if self._access_point:
            return self._access_point
        payload = await self._get(
            self.config.discovery_base_url,
            "/v1/trading/access-point",
            {"account_id": self.config.account_id},
        )
        access_point = payload.get("access_point") if isinstance(payload, dict) else None
        if not isinstance(access_point, str) or not access_point.startswith("https://"):
            raise ExnessError("Exness returned an invalid account Access Point.")
        self._access_point = access_point.rstrip("/")
        return self._access_point

    async def verify_account_and_instruments(self) -> tuple[str, ...]:
        access_point = await self.discover_access_point()
        if not self._account_verified:
            await self._get(
                access_point,
                f"/v1/configuration/accounts/{self.config.account_id}/account",
            )
            self._account_verified = True
        if self._instruments is None:
            payload = await self._get(
                access_point,
                f"/v1/configuration/accounts/{self.config.account_id}/instruments",
            )
            instruments = payload.get("instruments") if isinstance(payload, dict) else None
            if not isinstance(instruments, list) or not all(isinstance(item, str) for item in instruments):
                raise ExnessMarketDataError("Exness returned an invalid instrument list.")
            self._instruments = tuple(instruments)
        if MARKET not in self._instruments:
            raise ExnessMarketDataError("USDJPY is not available for this account.")
        return self._instruments

    @staticmethod
    def _parse_week_time(value: Any) -> int:
        if not isinstance(value, str):
            raise ValueError("missing weekly session time")
        day_name, clock = value.split(",", 1)
        day = {"Mon": 0, "Tue": 1, "Wed": 2, "Thu": 3, "Fri": 4, "Sat": 5, "Sun": 6}[day_name.strip()]
        hour, minute, second = (int(part) for part in clock.strip().split(":"))
        if not 0 <= hour <= 23 or not 0 <= minute <= 59 or not 0 <= second <= 59:
            raise ValueError("invalid weekly session time")
        return day * 86400 + hour * 3600 + minute * 60 + second

    @classmethod
    def _parse_trade_sessions(cls, payload: Any) -> tuple[str, tuple[InstrumentTradeSession, ...]]:
        if not isinstance(payload, dict):
            raise ValueError("invalid instrument conditions response")
        trade_mode = payload.get("trade_mode")
        if trade_mode not in {"enabled", "trading_disabled"}:
            raise ValueError("invalid instrument trade mode")
        raw_sessions = payload.get("trade_sessions")
        if not isinstance(raw_sessions, list) or not raw_sessions:
            raise ValueError("instrument conditions contain no trade sessions")
        sessions: list[InstrumentTradeSession] = []
        for raw_session in raw_sessions:
            if not isinstance(raw_session, dict):
                raise ValueError("invalid instrument trade session")
            if raw_session.get("trade_mode", trade_mode) != "enabled":
                continue
            start = cls._parse_week_time(raw_session.get("week_start_time"))
            end = cls._parse_week_time(raw_session.get("week_end_time"))
            sessions.append(InstrumentTradeSession(start, end))
        if not sessions:
            raise ValueError("instrument conditions contain no enabled sessions")
        return trade_mode, tuple(sessions)

    async def _refresh_instrument_conditions(self) -> None:
        now = datetime.now(UTC)
        if (
            self._session_information_loaded_at is not None
            and (now - self._session_information_loaded_at).total_seconds() < SESSION_REFRESH_SECONDS
        ):
            return

        self._session_information_loaded_at = now
        self._session_override = None
        try:
            access_point = await self.discover_access_point()
            payload = await self._get(
                access_point,
                f"/v1/configuration/accounts/{self.config.account_id}/instruments/{MARKET}/conditions",
            )
            trade_mode, sessions = self._parse_trade_sessions(payload)
        except ExnessMarketClosedError:
            self._trade_mode = "trading_disabled"
            self._trade_sessions = ()
            self._session_information_available = True
            self._session_error = None
            self._session_override = "closed"
            return
        except (ExnessError, ValueError):
            self._trade_mode = None
            self._trade_sessions = ()
            self._session_information_available = False
            self._session_error = "Exness instrument session information is unavailable."
            return

        self._trade_mode = trade_mode
        self._trade_sessions = sessions
        self._session_information_available = True
        self._session_error = None

    def _session_state_at(self, timestamp: datetime) -> str:
        if self._session_override == "closed":
            return "closed"
        if not self._session_information_available or self._trade_mode is None:
            return "unknown"
        if self._trade_mode != "enabled":
            return "closed"
        current = timestamp.astimezone(UTC)
        second_of_week = (
            current.weekday() * 86400
            + current.hour * 3600
            + current.minute * 60
            + current.second
        )
        for session in self._trade_sessions:
            if session.start_second <= session.end_second:
                if session.start_second <= second_of_week <= session.end_second:
                    return "open"
            elif second_of_week >= session.start_second or second_of_week <= session.end_second:
                return "open"
        return "closed"

    def _current_session_state(self, now: datetime | None = None) -> str:
        return self._session_state_at(now or datetime.now(UTC))

    async def _request_candles(self, count: int, start: datetime) -> list[Candle]:
        access_point = await self.discover_access_point()
        await self.verify_account_and_instruments()
        await self._refresh_instrument_conditions()
        try:
            payload = await self._get(
                access_point,
                f"/v1/market-data/accounts/{self.config.account_id}/candles",
                {
                    "instrument": MARKET,
                    "price_type": "bid",
                    "timeframe": TIMEFRAME,
                    "from": start.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
                    "count": count,
                },
            )
        except ExnessMarketClosedError:
            self._session_override = "closed"
            raise
        return parse_candles(payload)

    def _merge_candles(self, candles: list[Candle]) -> None:
        for candle in candles:
            self._candles[int(candle.timestamp.timestamp())] = candle

    def _completed_candles(self) -> list[Candle]:
        cutoff = datetime.now(UTC).timestamp() - M5_SECONDS
        return [candle for timestamp, candle in sorted(self._candles.items()) if timestamp <= cutoff]

    @staticmethod
    def _timestamp_text(timestamp: datetime) -> str:
        return timestamp.astimezone(UTC).isoformat().replace("+00:00", "Z")

    def _record_candle_reconciliation(
        self,
        completed_before: set[datetime],
        completed_after: set[datetime],
    ) -> None:
        """Record sequence advancement without treating the initial history as live data."""
        newly_completed = sorted(completed_after - completed_before)
        self._last_reconciliation_new_completed_candle = bool(newly_completed)
        if newly_completed:
            self._last_new_completed_candle_at = datetime.now(UTC)

        previous_latest = max(completed_before) if completed_before else None
        if previous_latest is None:
            self._recent_missing_intervals = []
            self._recent_gap_state = None
            return
        if not newly_completed:
            return

        missing: list[dict[str, Any]] = []
        sequence_previous = previous_latest
        for timestamp in newly_completed:
            if timestamp <= sequence_previous:
                continue
            delta_seconds = (timestamp - sequence_previous).total_seconds()
            if delta_seconds > M5_SECONDS:
                missing_open_times = [
                    sequence_previous + timedelta(seconds=M5_SECONDS * index)
                    for index in range(1, int(delta_seconds // M5_SECONDS))
                ]
                interval_states = [self._session_state_at(value) for value in missing_open_times]
                if "open" in interval_states:
                    gap_state = "gap_detected"
                elif interval_states and all(state == "closed" for state in interval_states):
                    gap_state = "market_closed"
                else:
                    gap_state = "unknown"
                missing.append({
                    "start_open_time": self._timestamp_text(missing_open_times[0]),
                    "end_open_time": self._timestamp_text(missing_open_times[-1]),
                    "count": len(missing_open_times),
                    "session_state": gap_state,
                })
            sequence_previous = timestamp

        self._recent_missing_intervals = missing
        if missing:
            states = {item["session_state"] for item in missing}
            self._recent_gap_state = (
                "gap_detected" if "gap_detected" in states
                else "unknown" if "unknown" in states
                else "market_closed"
            )
        else:
            self._recent_gap_state = None

    async def load_initial_candles(self) -> list[Candle]:
        self._rest_status = "connecting"
        completed_before = {candle.timestamp for candle in self._completed_candles()}
        candles = await self._request_candles(
            HISTORY_COUNT,
            datetime.now(UTC) - timedelta(seconds=M5_SECONDS * HISTORY_COUNT + M5_SECONDS),
        )
        self._merge_candles(candles)
        self._candles_loaded = True
        self._rest_status = "connected"
        self._rest_error = None
        self._last_successful_update = datetime.now(UTC)
        completed_after = {candle.timestamp for candle in self._completed_candles()}
        self._record_candle_reconciliation(completed_before, completed_after)
        await self._evaluate_completed_candles()
        return self._completed_candles()

    async def _evaluate_completed_candles(self) -> list[StrategySignal]:
        """Evaluate every newly completed candle in chronological order."""
        if self._strategy_engine is None or self._signal_repository is None:
            return []

        async with self._strategy_lock:
            candles = self._completed_candles()
            if len(candles) < 2:
                return []
            indicators = calculate_indicators(candles)
            generated: list[StrategySignal] = []
            for index in range(1, len(candles)):
                candle_timestamp = candles[index].timestamp
                if (
                    self._last_strategy_candle_timestamp is not None
                    and candle_timestamp <= self._last_strategy_candle_timestamp
                ):
                    continue
                signal = self._strategy_engine.evaluate(candles, indicators, index)
                if signal is not None:
                    self._current_signal = self._build_signal_view(signal, candles, indicators, index)
                    self._signal_repository.insert(signal)
                    generated.append(signal)
                else:
                    self._current_signal = None
                self._last_strategy_candle_timestamp = candle_timestamp
            return generated

    def _build_signal_view(
        self,
        signal: StrategySignal,
        candles: list[Candle],
        indicators: list[Any],
        current_index: int,
    ) -> dict[str, Any]:
        """Build presentation data from the exact values used by the engine."""
        current = candles[current_index]
        previous = candles[current_index - 1]
        previous_indicator = indicators[current_index - 1]
        rounded_rsi = round_rsi_for_strategy(previous_indicator.rsi)
        return {
            "market": signal.market,
            "strategy": signal.strategy,
            "timeframe": signal.timeframe,
            "direction": signal.direction,
            "candle_timestamp": signal.candle_timestamp.isoformat().replace("+00:00", "Z"),
            "price": current.close,
            "status": signal.status,
            "conditions": [
                {"label": "Current close vs previous open", "actual": current.close, "reference": previous.open, "operator": ">" if signal.direction == "bullish" else "<"},
                {"label": "Current open vs previous MA 75", "actual": current.open, "reference": previous_indicator.moving_average, "operator": ">" if signal.direction == "bullish" else "<"},
                {"label": "Previous RSI 7", "actual": rounded_rsi, "reference": 40 if signal.direction == "bullish" else 60, "operator": "<" if signal.direction == "bullish" else ">"},
            ],
        }

    def _signal_history(self) -> list[dict[str, Any]]:
        if self._signal_repository is None:
            return []
        list_recent = getattr(self._signal_repository, "list_recent", None)
        if not callable(list_recent):
            return []
        history: list[dict[str, Any]] = []
        for record in list_recent():
            item = dict(record)
            timestamp_value = str(item["candle_timestamp"])
            try:
                timestamp = datetime.fromisoformat(timestamp_value.replace("Z", "+00:00")).astimezone(UTC)
                candle = self._candles.get(int(timestamp.timestamp()))
            except (TypeError, ValueError, OverflowError):
                candle = None
            item["price"] = candle.close if candle is not None else None
            history.append(item)
        return history

    def _decorate_signal_records(self, records: list[dict[str, object]]) -> list[dict[str, Any]]:
        """Add a matching cached candle close without changing persisted records."""
        history: list[dict[str, Any]] = []
        for record in records:
            item = dict(record)
            timestamp_value = str(item["candle_timestamp"])
            try:
                timestamp = datetime.fromisoformat(timestamp_value.replace("Z", "+00:00")).astimezone(UTC)
                candle = self._candles.get(int(timestamp.timestamp()))
            except (TypeError, ValueError, OverflowError):
                candle = None
            item["price"] = candle.close if candle is not None else None
            history.append(item)
        return history

    def get_signal_snapshot(self) -> dict[str, Any]:
        """Return current evaluated status and persisted history without refreshing data."""
        if self._signal_repository is None:
            return {"current_signal": self._current_signal, "signal_history": [], "today_signals": []}
        list_recent = getattr(self._signal_repository, "list_recent", None)
        list_today = getattr(self._signal_repository, "list_today_session", None)
        return {
            "current_signal": self._current_signal,
            "signal_history": self._signal_history() if callable(list_recent) else [],
            "today_signals": self._decorate_signal_records(list_today()) if callable(list_today) else [],
        }

    async def reconcile_candles(self) -> list[Candle]:
        if not self._candles_loaded:
            return await self.load_initial_candles()
        completed_before = {candle.timestamp for candle in self._completed_candles()}
        latest = max(self._candles.values(), key=lambda candle: candle.timestamp)
        candles = await self._request_candles(5, latest.timestamp)
        self._merge_candles(candles)
        self._rest_status = "connected"
        self._rest_error = None
        self._last_successful_update = datetime.now(UTC)
        completed_after = {candle.timestamp for candle in self._completed_candles()}
        self._record_candle_reconciliation(completed_before, completed_after)
        await self._evaluate_completed_candles()
        await self._publish(self._dashboard_snapshot())
        return self._completed_candles()

    async def get_candles(self, count: int = HISTORY_COUNT) -> list[Candle]:
        if not self._candles_loaded or count != HISTORY_COUNT:
            if count != HISTORY_COUNT:
                candles = await self._request_candles(
                    count,
                    datetime.now(UTC) - timedelta(seconds=M5_SECONDS * count + M5_SECONDS),
                )
                return candles
            return await self.load_initial_candles()
        return self._completed_candles()

    def _dashboard_snapshot(self) -> dict[str, Any]:
        candles = self._completed_candles()
        latest = candles[-1] if candles else None
        indicator_error: str | None = None
        try:
            indicator_points = calculate_indicators(candles)
        except IndicatorDataError as exc:
            indicator_points = []
            indicator_error = str(exc)
        now = datetime.now(UTC)
        tick = self._latest_tick
        update_age = (now - self._last_successful_update).total_seconds() if self._last_successful_update else None
        tick_age = (now - self._latest_tick_received_at).total_seconds() if self._latest_tick_received_at else None
        tick_is_fresh = tick is not None and tick_age is not None and tick_age <= TICK_STALE_SECONDS and self._websocket_status == "connected"
        latest_indicators = indicator_points[-1].as_dict() if indicator_points else None
        if latest_indicators is not None:
            # This is the canonical RSI value for the future strategy evaluator.
            latest_indicators["rsi"] = round_rsi_for_strategy(latest_indicators["rsi"])
        return {
            "status": "connected" if self._candles_loaded else self._rest_status,
            "market": MARKET,
            "timeframe": TIMEFRAME,
            "provider": "Exness",
            "rest_status": self._rest_status,
            "websocket_status": self._websocket_status,
            "candles": [candle.as_dict() for candle in candles],
            "chart_candles": [candle.as_chart_point() for candle in candles],
            "indicators": [point.as_dict() for point in indicator_points],
            "latest_indicators": latest_indicators,
            "indicator_error": indicator_error,
            "latest_candle_timestamp": latest.timestamp.isoformat().replace("+00:00", "Z") if latest else None,
            "price": tick["bid"] if tick_is_fresh else (latest.close if latest else None),
            "ask": tick["ask"] if tick_is_fresh else None,
            "price_source": "live_tick" if tick_is_fresh else "latest_candle_close",
            "last_successful_update": self._last_successful_update.isoformat().replace("+00:00", "Z") if self._last_successful_update else None,
            "last_tick_timestamp": tick.get("timestamp") if tick else None,
            "freshness_seconds": update_age,
            "rest_freshness": "stale" if update_age is not None and update_age > REST_STALE_SECONDS else "fresh",
            "tick_freshness_seconds": tick_age,
            "tick_freshness": "fresh" if tick_age is not None and tick_age <= TICK_STALE_SECONDS else "stale",
            "rest_error": self._rest_error,
            "websocket_error": self._websocket_error,
            "error": self._rest_error,
            "health": self.get_health_snapshot(now=now),
            **self.get_signal_snapshot(),
        }

    def get_health_snapshot(self, now: datetime | None = None) -> dict[str, Any]:
        """Return an in-memory health projection without refreshing or evaluating data."""
        current_time = (now or datetime.now(UTC)).astimezone(UTC)
        latest_completed = self._completed_candles()[-1] if self._completed_candles() else None
        rest_age = (
            (current_time - self._last_successful_update).total_seconds()
            if self._last_successful_update
            else None
        )
        tick_age = (
            (current_time - self._latest_tick_received_at).total_seconds()
            if self._latest_tick_received_at
            else None
        )
        candle_age = None
        if latest_completed is not None:
            candle_close = latest_completed.timestamp + timedelta(seconds=M5_SECONDS)
            candle_age = max(0.0, (current_time - candle_close).total_seconds())

        session_state = self._current_session_state(current_time)

        rest_is_fresh = (
            self._rest_status == "connected"
            and rest_age is not None
            and rest_age <= REST_STALE_SECONDS
        )
        candle_is_fresh = (
            latest_completed is not None
            and candle_age is not None
            and candle_age <= M5_SECONDS + REST_STALE_SECONDS
        )
        websocket_active = (
            self._websocket_status == "connected"
            and tick_age is not None
            and tick_age <= TICK_STALE_SECONDS
        )
        if self._websocket_status == "connected":
            if self._websocket_activity == "awaiting_first_tick":
                websocket_activity = "awaiting_first_tick"
            elif self._websocket_activity == "silent" or not websocket_active:
                websocket_activity = "silent"
            else:
                websocket_activity = "active"
        elif self._websocket_status == "connecting":
            websocket_activity = "connecting"
        elif self._websocket_status == "reconnecting":
            websocket_activity = "reconnecting"
        else:
            websocket_activity = "disconnected"

        if not self._candles_loaded or self._rest_status in {"disconnected", "error"}:
            overall = "unavailable" if latest_completed is None else "stale"
        elif not rest_is_fresh or not candle_is_fresh:
            overall = "stale"
        elif not websocket_active:
            overall = "degraded"
        else:
            overall = "healthy"

        if self._recent_gap_state is not None:
            candle_sequence_state = self._recent_gap_state
        elif session_state == "closed":
            candle_sequence_state = "market_closed"
        elif latest_completed is None:
            candle_sequence_state = "unknown"
        elif session_state == "unknown" and not candle_is_fresh:
            candle_sequence_state = "unknown"
        elif not candle_is_fresh:
            candle_sequence_state = "delayed"
        else:
            candle_sequence_state = "healthy"

        return {
            "overall_state": overall,
            "rest_status": self._rest_status,
            "last_successful_rest_request": (
                self._last_successful_update.isoformat().replace("+00:00", "Z")
                if self._last_successful_update
                else None
            ),
            "rest_age_seconds": rest_age,
            "websocket_status": self._websocket_status,
            "websocket_activity": websocket_activity,
            "last_valid_tick_timestamp": (
                self._latest_tick.get("timestamp") if self._latest_tick else None
            ),
            "last_valid_tick_received_at": (
                self._latest_tick_received_at.isoformat().replace("+00:00", "Z")
                if self._latest_tick_received_at
                else None
            ),
            "tick_age_seconds": tick_age,
            "latest_completed_candle_timestamp": (
                latest_completed.timestamp.isoformat().replace("+00:00", "Z")
                if latest_completed
                else None
            ),
            "latest_completed_candle_open_time": (
                self._timestamp_text(latest_completed.timestamp) if latest_completed else None
            ),
            "latest_completed_candle_close_time": (
                self._timestamp_text(latest_completed.timestamp + timedelta(seconds=M5_SECONDS))
                if latest_completed else None
            ),
            "latest_completed_candle_age_seconds": candle_age,
            "latest_reconciliation_new_completed_candle": self._last_reconciliation_new_completed_candle,
            "last_new_completed_candle_at": (
                self._timestamp_text(self._last_new_completed_candle_at)
                if self._last_new_completed_candle_at else None
            ),
            "candle_sequence_state": candle_sequence_state,
            "session_state": session_state,
            "trade_mode": self._trade_mode,
            "session_information_available": self._session_information_available,
            "gap_detected": self._recent_gap_state == "gap_detected",
            "missing_intervals": list(self._recent_missing_intervals),
            "rest_freshness": "fresh" if rest_is_fresh else "stale",
            "candle_freshness": "fresh" if candle_is_fresh else "stale",
            "websocket_error": self._websocket_error,
            "rest_error": self._rest_error,
            "session_error": self._session_error,
        }

    async def refresh_market_data(self) -> dict[str, Any]:
        async with self._refresh_lock:
            try:
                if not self._candles_loaded:
                    await self.load_initial_candles()
                else:
                    await self.reconcile_candles()
            except ExnessError as exc:
                self._rest_status = "error"
                self._rest_error = str(exc)
            return self._dashboard_snapshot()

    async def get_snapshot(self, refresh: bool = False) -> dict[str, Any]:
        if refresh:
            return await self.refresh_market_data()
        if not self._candles_loaded:
            try:
                await self.load_initial_candles()
            except ExnessError as exc:
                self._rest_status = "error"
                self._rest_error = str(exc)
        return self._dashboard_snapshot()

    async def start(self) -> None:
        self._closing = False
        await self.get_snapshot()
        self.start_tick_stream()
        if self._candle_task is None or self._candle_task.done():
            self._candle_task = asyncio.create_task(self._run_candle_reconciliation())

    def subscribe_dashboard(self) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=10)
        self._dashboard_subscribers.add(queue)
        return queue

    def unsubscribe_dashboard(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        self._dashboard_subscribers.discard(queue)

    async def _publish(self, message: dict[str, Any]) -> None:
        for queue in tuple(self._dashboard_subscribers):
            if queue.full():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            await queue.put(message)

    async def _run_candle_reconciliation(self) -> None:
        while not self._closing:
            await self._sleep(CANDLE_REFRESH_SECONDS)
            if self._closing:
                return
            try:
                await self.reconcile_candles()
            except ExnessError as exc:
                self._rest_status = "error"
                self._rest_error = str(exc)
                await self._publish(self._dashboard_snapshot())

    def start_tick_stream(self) -> None:
        if self._stream_task is None or self._stream_task.done():
            self._stream_task = asyncio.create_task(self._run_tick_stream())

    async def _run_tick_stream(self) -> None:
        if self._websocket_connector is None:
            self._websocket_status = "error"
            self._websocket_error = "WebSocket support is not installed."
            return
        delay = self._reconnect_base_seconds
        while not self._closing:
            try:
                self._websocket_status = "connecting"
                self._websocket_activity = "connecting"
                self._websocket_error = None
                await self._publish(self._dashboard_snapshot())
                access_point = await self.discover_access_point()
                path = f"/v1/server-events/accounts/{self.config.account_id}/ws/ticks"
                headers = self._headers("GET", path)
                ws_url = access_point.replace("https://", "wss://", 1) + path
                async with self._websocket_connector(
                    ws_url,
                    additional_headers=headers,
                    subprotocols=["exness-ws-protocol"],
                    ping_interval=WEBSOCKET_PING_INTERVAL_SECONDS,
                    ping_timeout=WEBSOCKET_PING_TIMEOUT_SECONDS,
                ) as websocket:
                    await websocket.send(json.dumps({
                        "id": "adwilago-usdjpy-ticks",
                        "subscribe": {"event": "ticks", "instruments": [MARKET]},
                    }))
                    self._websocket_status = "connected"
                    self._websocket_activity = "awaiting_first_tick"
                    self._current_connection_received_tick = False
                    delay = self._reconnect_base_seconds
                    await self._publish(self._dashboard_snapshot())
                    message_iterator = websocket.__aiter__()
                    while True:
                        try:
                            raw_message = await asyncio.wait_for(
                                message_iterator.__anext__(),
                                timeout=TICK_STALE_SECONDS,
                            )
                        except StopAsyncIteration:
                            self._websocket_status = "disconnected"
                            self._websocket_activity = "disconnected"
                            self._websocket_error = "Exness tick stream closed normally."
                            await self._publish(self._dashboard_snapshot())
                            break
                        except asyncio.TimeoutError:
                            # Do not reconnect solely because the market is quiet. The
                            # WebSocket transport has its own ping/pong watchdog, while
                            # this timeout updates data-activity health only.
                            if self._websocket_activity != "silent":
                                self._websocket_activity = "silent" if self._current_connection_received_tick else "awaiting_first_tick"
                                self._websocket_error = "No recent valid USDJPY tick received."
                                await self._publish(self._dashboard_snapshot())
                            continue
                        message = json.loads(raw_message)
                        if message.get("instrument") != MARKET:
                            continue
                        if not isinstance(message.get("bid"), (int, float)) or not isinstance(message.get("ask"), (int, float)):
                            continue
                        self._latest_tick = {
                            "bid": float(message["bid"]),
                            "ask": float(message["ask"]),
                            "timestamp": message.get("timestamp") or datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                        }
                        self._latest_tick_received_at = datetime.now(UTC)
                        self._websocket_activity = "active"
                        self._current_connection_received_tick = True
                        self._websocket_error = None
                        delay = self._reconnect_base_seconds
                        await self._publish({"type": "tick", **self._latest_tick, "websocket_status": "connected", "websocket_activity": "active"})
            except asyncio.CancelledError:
                raise
            except Exception:
                self._websocket_status = "disconnected"
                self._websocket_activity = "disconnected"
                self._websocket_error = "Exness tick stream disconnected."
                await self._publish(self._dashboard_snapshot())
            if not self._closing:
                self._websocket_status = "reconnecting"
                self._websocket_activity = "reconnecting"
                await self._publish(self._dashboard_snapshot())
                await self._sleep(delay)
                if self._closing:
                    self._websocket_status = "disconnected"
                    self._websocket_activity = "disconnected"
                    break
                delay = min(max(delay * 2, self._reconnect_base_seconds), self._reconnect_max_seconds)
            else:
                self._websocket_status = "disconnected"
                self._websocket_activity = "disconnected"

    async def close(self) -> None:
        self._closing = True
        for task in (self._stream_task, self._candle_task):
            if task and not task.done():
                task.cancel()
        await asyncio.gather(*(task for task in (self._stream_task, self._candle_task) if task), return_exceptions=True)
        if self._owns_http:
            await self._http.aclose()
