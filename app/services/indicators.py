"""TradingView-compatible technical indicators for completed candles only."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Sequence


MA_LENGTH = 75
RSI_LENGTH = 7


def round_rsi_for_strategy(value: float | None) -> float | None:
    """Return the two-decimal RSI value reserved for strategy evaluation."""
    if value is None:
        return None
    return float(Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


class IndicatorDataError(ValueError):
    """The candle sequence cannot be used safely for indicator calculations."""


@dataclass(frozen=True)
class IndicatorPoint:
    timestamp: datetime
    moving_average: float | None
    rsi: float | None

    def as_dict(self) -> dict[str, Any]:
        timestamp = self.timestamp.astimezone(UTC).isoformat().replace("+00:00", "Z")
        return {
            "time": int(self.timestamp.timestamp()),
            "timestamp": timestamp,
            "ma": self.moving_average,
            "rsi": self.rsi,
        }


def _validate_candles(candles: Sequence[Any]) -> None:
    previous_timestamp: datetime | None = None
    for candle in candles:
        timestamp = getattr(candle, "timestamp", None)
        close = getattr(candle, "close", None)
        if not isinstance(timestamp, datetime) or timestamp.tzinfo is None:
            raise IndicatorDataError("Indicator candles require timezone-aware timestamps.")
        if previous_timestamp is not None and timestamp <= previous_timestamp:
            raise IndicatorDataError("Indicator candles must be strictly chronological.")
        if not isinstance(close, (int, float)) or not math.isfinite(float(close)):
            raise IndicatorDataError("Indicator candles require finite close values.")
        previous_timestamp = timestamp


def _sma(values: Sequence[float | None], length: int) -> list[float | None]:
    result: list[float | None] = [None] * len(values)
    for index in range(length - 1, len(values)):
        window = values[index - length + 1:index + 1]
        if any(value is None for value in window):
            continue
        result[index] = sum(value for value in window if value is not None) / length
    return result


def _rma(values: Sequence[float | None], length: int) -> list[float | None]:
    """Wilder's RMA: SMA seed followed by alpha=1/length recursion."""
    result: list[float | None] = [None] * len(values)
    seed_values: list[float] = []
    previous: float | None = None
    for index, value in enumerate(values):
        if value is None:
            continue
        numeric = float(value)
        if previous is None:
            seed_values.append(numeric)
            if len(seed_values) < length:
                continue
            previous = sum(seed_values) / length
        else:
            previous = ((previous * (length - 1)) + numeric) / length
        result[index] = previous
    return result


def calculate_indicators(candles: Sequence[Any]) -> list[IndicatorPoint]:
    """Calculate indicators without changing candle order or values.

    The caller must provide completed candles in ascending UTC time order.
    Values remain ``None`` until enough history exists for each calculation.
    """
    _validate_candles(candles)
    if not candles:
        return []

    closes = [float(candle.close) for candle in candles]
    moving_average = _sma(closes, MA_LENGTH)

    changes: list[float | None] = [None]
    for index in range(1, len(closes)):
        changes.append(closes[index] - closes[index - 1])
    gains = [None if change is None else max(change, 0.0) for change in changes]
    losses = [None if change is None else max(-change, 0.0) for change in changes]
    average_gain = _rma(gains, RSI_LENGTH)
    average_loss = _rma(losses, RSI_LENGTH)

    rsi: list[float | None] = [None] * len(closes)
    for index, (gain, loss) in enumerate(zip(average_gain, average_loss)):
        if gain is None or loss is None:
            continue
        if loss == 0 and gain == 0:
            rsi[index] = 50.0
        elif loss == 0:
            rsi[index] = 100.0
        else:
            rsi[index] = 100.0 - (100.0 / (1.0 + (gain / loss)))
    return [
        IndicatorPoint(
            timestamp=candle.timestamp,
            moving_average=moving_average[index],
            rsi=rsi[index],
        )
        for index, candle in enumerate(candles)
    ]
