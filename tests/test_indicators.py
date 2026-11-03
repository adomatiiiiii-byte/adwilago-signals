from datetime import UTC, datetime, timedelta

import pytest

from app.services.exness_service import Candle
from app.services.indicators import IndicatorDataError, calculate_indicators, round_rsi_for_strategy


def candles_from_closes(closes):
    start = datetime(2026, 1, 1, tzinfo=UTC)
    return [
        Candle(start + timedelta(minutes=5 * index), close, close, close, close, None)
        for index, close in enumerate(closes)
    ]


def test_sma_75_matches_known_sequence():
    points = calculate_indicators(candles_from_closes(range(1, 84)))

    assert points[73].moving_average is None
    assert points[74].moving_average == pytest.approx(38.0)
    assert points[-1].moving_average == pytest.approx(46.0)
    assert not hasattr(points[-1], "moving_average_smoothing")


def test_rsi_uses_wilder_rma():
    closes = [10, 11, 10, 12, 11, 13, 12, 14]
    points = calculate_indicators(candles_from_closes(closes))

    # Seven changes: gains sum to 7, losses sum to 3. RMA seed gives
    # average gain=1 and average loss=3/7, so RSI=70.
    assert points[6].rsi is None
    assert points[7].rsi == pytest.approx(70.0)
    assert not hasattr(points[-1], "rsi_smoothing")


def test_strategy_rsi_rounding_uses_two_decimal_places_without_changing_calculation():
    assert round_rsi_for_strategy(38.45672) == pytest.approx(38.46)
    assert round_rsi_for_strategy(42.0) == pytest.approx(42.0)
    assert round_rsi_for_strategy(None) is None


def test_insufficient_and_empty_history_are_handled_without_fabrication():
    points = calculate_indicators(candles_from_closes(range(1, 75)))

    assert points
    assert all(point.moving_average is None for point in points)
    assert calculate_indicators([]) == []


def test_missing_or_non_chronological_data_is_rejected():
    candles = candles_from_closes([1, 2, 3])
    with pytest.raises(IndicatorDataError, match="strictly chronological"):
        calculate_indicators([candles[1], candles[0], candles[2]])

    with pytest.raises(IndicatorDataError, match="finite close"):
        calculate_indicators([Candle(candles[0].timestamp, float("nan"), 1, 1, float("nan"), None)])


def test_indicator_values_update_when_a_completed_candle_arrives():
    before = calculate_indicators(candles_from_closes(range(1, 84)))[-1]
    after = calculate_indicators(candles_from_closes(range(1, 85)))[-1]

    assert before.moving_average == pytest.approx(46.0)
    assert after.moving_average == pytest.approx(47.0)
