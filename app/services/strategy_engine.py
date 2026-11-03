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
class SignalEvidence:
    """Immutable audit snapshot of the values used for one signal."""

    current_open: float
    current_high: float
    current_low: float
    current_close: float
    current_candle_timestamp: datetime
    previous_open: float
    previous_high: float
    previous_low: float
    previous_close: float
    previous_candle_timestamp: datetime
    previous_ma75: float
    previous_rsi7_raw: float
    previous_rsi7_rounded: float
    condition_1_passed: bool
    condition_2_passed: bool
    condition_3_passed: bool
    evaluation_timestamp: datetime


@dataclass(frozen=True)
class StrategySignal:
    """A real bullish or bearish strategy result ready for persistence."""

    market: str
    strategy: str
    timeframe: str
    direction: str
    candle_timestamp: datetime
    status: str = "new"
    evidence: SignalEvidence | None = None


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

        condition_1_passed = current.close > previous.open
        condition_2_passed = current.open > previous_indicator.moving_average
        condition_3_passed = rounded_rsi < 40
        bearish_condition_1_passed = current.close < previous.open
        bearish_condition_2_passed = current.open < previous_indicator.moving_average
        bearish_condition_3_passed = rounded_rsi > 60
        bullish = condition_1_passed and condition_2_passed and condition_3_passed
        bearish = (
            bearish_condition_1_passed
            and bearish_condition_2_passed
            and bearish_condition_3_passed
        )
        if not bullish and not bearish:
            return None

        evidence = SignalEvidence(
            current_open=current.open,
            current_high=current.high,
            current_low=current.low,
            current_close=current.close,
            current_candle_timestamp=current.timestamp.astimezone(UTC),
            previous_open=previous.open,
            previous_high=previous.high,
            previous_low=previous.low,
            previous_close=previous.close,
            previous_candle_timestamp=previous.timestamp.astimezone(UTC),
            previous_ma75=previous_indicator.moving_average,
            previous_rsi7_raw=previous_indicator.rsi,
            previous_rsi7_rounded=rounded_rsi,
            condition_1_passed=condition_1_passed if bullish else bearish_condition_1_passed,
            condition_2_passed=condition_2_passed if bullish else bearish_condition_2_passed,
            condition_3_passed=condition_3_passed if bullish else bearish_condition_3_passed,
            evaluation_timestamp=reference_now,
        )
        return StrategySignal(
            market=MARKET,
            strategy=STRATEGY_NAME,
            timeframe=TIMEFRAME,
            direction="bullish" if bullish else "bearish",
            candle_timestamp=current_time.astimezone(UTC),
            evidence=evidence,
        )
