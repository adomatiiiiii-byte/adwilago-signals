"""Pure DayTrading Engulfing strategy evaluation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Sequence

from app.services.indicators import IndicatorPoint, round_rsi_for_strategy


MARKET = "USDJPY"
TIMEFRAME = "M5"
STRATEGY_NAME = "DayTrading Engulfing"
M5_SECONDS = 5 * 60


@dataclass(frozen=True)
class StrategySignal:
    """A real bullish or bearish strategy result ready for persistence."""

    market: str
    strategy: str
    timeframe: str
    direction: str
    candle_timestamp: datetime
    status: str = "new"


class DayTradingEngulfingStrategy:
    """Evaluate the exact three-condition strategy without external side effects."""

    def evaluate(
        self,
        candles: Sequence[Any],
        indicators: Sequence[IndicatorPoint],
        current_index: int,
        now: datetime | None = None,
    ) -> StrategySignal | None:
        """Return a signal for one completed candle, or ``None``.

        The caller supplies candles already filtered to completed candles. The
        completion check remains here as a defensive boundary for direct users
        of this pure engine.
        """
        if current_index <= 0 or current_index >= len(candles):
            return None
        if len(indicators) <= current_index - 1:
            return None

        current = candles[current_index]
        previous = candles[current_index - 1]
        current_time = current.timestamp
        reference_now = (now or datetime.now(UTC)).astimezone(UTC)
        if current_time > reference_now - timedelta(seconds=M5_SECONDS):
            return None

        previous_indicator = indicators[current_index - 1]
        if previous_indicator.timestamp != previous.timestamp:
            return None
        if previous_indicator.moving_average is None or previous_indicator.rsi is None:
            return None

        rounded_rsi = round_rsi_for_strategy(previous_indicator.rsi)
        if rounded_rsi is None:
            return None

        bullish = (
            current.close > previous.open
            and current.open > previous_indicator.moving_average
            and rounded_rsi < 40
        )
        bearish = (
            current.close < previous.open
            and current.open < previous_indicator.moving_average
            and rounded_rsi > 60
        )
        if not bullish and not bearish:
            return None

        return StrategySignal(
            market=MARKET,
            strategy=STRATEGY_NAME,
            timeframe=TIMEFRAME,
            direction="bullish" if bullish else "bearish",
            candle_timestamp=current_time.astimezone(UTC),
        )
