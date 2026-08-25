import numpy as np
import pandas as pd

from regime_scoring import (_haltu_component, _project_lower_channel,
                            build_regime_frame,
                            composite_bull_regime, current_regime_decision,
                            scoring_config, validate_scoring_config)


def frame(periods=420, scale=1.0):
    index = pd.date_range("2020-01-01", periods=periods, freq="D", tz="UTC")
    close = np.exp(np.linspace(np.log(100.0), np.log(500.0), periods)) * scale
    return pd.DataFrame({
        "open": close * 0.995,
        "high": close * 1.02,
        "low": close * 0.98,
        "close": close,
    }, index=index)


def test_defaults_use_independent_detectors_without_weights_or_thresholds():
    cfg = scoring_config({})
    assert cfg["bull_detector"] == "dual_ma"
    assert cfg["bear_detector"] == "lower_channel"
    assert cfg["bull_strategy"] == "period_rebalance"
    assert cfg["bear_strategy"] == "defensive_atr"
    assert cfg["defensive_take_profit_pct"] == 0.05
    assert cfg["bull_atr_multiple"] == 1.0
    assert cfg["bear_atr_multiple"] == 1.0
    assert not any("weight" in key or "threshold" in key for key in cfg)


def test_cash_with_atr_is_a_valid_phase_strategy():
    cfg = scoring_config({
        "regime_scoring": {"stable_strategy": "cash_with_atr"},
    })
    assert cfg["stable_strategy"] == "cash_with_atr"
    assert not validate_scoring_config(cfg)


def test_decision_never_uses_current_candle():
    original = frame()
    shocked = original.copy()
    shocked.iloc[-1, shocked.columns.get_loc("close")] *= 0.01
    shocked.iloc[-1, shocked.columns.get_loc("low")] *= 0.01
    a = build_regime_frame(original)
    b = build_regime_frame(shocked)
    columns = ["haltu_score", "log_macd_score", "breakout_score",
               "lower_channel_score", "direction_score", "regime", "risk_on"]
    pd.testing.assert_frame_equal(a.iloc[[-1]][columns], b.iloc[[-1]][columns])


def test_current_decision_uses_latest_completed_candle():
    original = frame()
    shocked = original.copy()
    shocked.iloc[-1, shocked.columns.get_loc("close")] *= 0.01
    shocked.iloc[-1, shocked.columns.get_loc("low")] *= 0.01
    assert current_regime_decision(original)["regime"] != current_regime_decision(
        shocked)["regime"]


def test_dual_ma_keeps_rising_long_ma_exception_below_the_line():
    close = pd.Series([1.0, 100.0, 1.0, 50.0, 999.0])
    result = _haltu_component(close, short_window=2, long_window=3)
    assert result.iloc[-1]["decision_close"] < result.iloc[-1]["ma_long"]
    assert bool(result.iloc[-1]["haltu_gate_a"])
    assert bool(result.iloc[-1]["haltu_gate_b"])
    assert result.iloc[-1]["haltu_score"] == 1.0


def test_log_macd_is_invariant_to_price_scale():
    a = build_regime_frame(frame(scale=1.0))
    b = build_regime_frame(frame(scale=1000.0))
    np.testing.assert_allclose(
        a["log_macd"].dropna(), b["log_macd"].dropna(), atol=1e-12)
    pd.testing.assert_series_equal(
        a["log_macd_score"], b["log_macd_score"], check_names=False)


def test_independent_detector_truth_table(monkeypatch):
    import regime_scoring

    values = {
        "dual_ma": pd.Series([1.0, -1.0, 1.0, 0.0]),
        "log_macd": pd.Series([0.0, -1.0, -1.0, 0.0]),
    }
    original = regime_scoring._detector_score

    def fake(parts, detector, slope_bars):
        series = values.get(detector, pd.Series([0.0] * len(parts))).copy()
        series.index = parts.index
        return series

    monkeypatch.setattr(regime_scoring, "_detector_score", fake)
    prices = frame(periods=4)
    result = build_regime_frame(prices, {"regime_scoring": {
        "bull_detector": "dual_ma", "bear_detector": "log_macd"}})
    assert result["regime"].tolist() == ["상승", "하락", "안정", "안정"]
    monkeypatch.setattr(regime_scoring, "_detector_score", original)


def test_warmup_is_unknown_not_stable():
    result = build_regime_frame(frame(periods=140))
    assert result.iloc[10]["regime"] == "판정 준비"
    assert pd.isna(result.iloc[10]["bull_signal"])
    assert pd.isna(result.iloc[10]["bear_signal"])


def test_lower_channel_uses_completed_close_and_prior_line():
    prices = frame()
    result = build_regime_frame(prices, {"regime_scoring": {
        "bull_detector": "dual_ma", "bear_detector": "lower_channel",
        "breakout_lower_window": 10, "channel_slope_bars": 2,
    }})
    changed = prices.copy()
    changed.iloc[-1, changed.columns.get_loc("low")] *= 0.01
    changed.iloc[-1, changed.columns.get_loc("close")] *= 0.01
    altered = build_regime_frame(changed, {"regime_scoring": {
        "bull_detector": "dual_ma", "bear_detector": "lower_channel",
        "breakout_lower_window": 10, "channel_slope_bars": 2,
    }})
    assert result.iloc[-1]["lower_channel_score"] == altered.iloc[-1]["lower_channel_score"]


def test_strategy_validation_rejects_invalid_methods_and_periods():
    errors = validate_scoring_config({
        "short_ma": 120, "long_ma": 60,
        "macd_fast": 60, "macd_slow": 30,
        "bull_detector": "unknown_bull",
        "bear_detector": "unknown",
        "decision_interval": "5d",
        "use_for_backtest": True,
    })
    assert len(errors) >= 5
    assert validate_scoring_config({"use_for_live": True}) == [
        "3국면 전략 라우팅은 현재 백테스트 전용입니다."]


def test_atr_up_and_down_thresholds_are_independent_and_effective():
    prices = frame(periods=90)
    prices["open"] = prices["close"]
    prices["high"] = prices["close"] * 1.04
    prices["low"] = prices["close"] * 0.96
    upward = build_regime_frame(prices, {"regime_scoring": {
        "breakout_atr_window": 5,
        "bull_atr_multiple": 0.1,
        "bear_atr_multiple": 10.0,
    }})
    downward = build_regime_frame(prices, {"regime_scoring": {
        "breakout_atr_window": 5,
        "bull_atr_multiple": 10.0,
        "bear_atr_multiple": 0.1,
    }})
    assert upward["breakout_upper_event"].fillna(False).any()
    assert not upward["breakout_lower_event"].fillna(False).any()
    assert downward["breakout_lower_event"].fillna(False).any()
    assert not downward["breakout_upper_event"].fillna(False).any()
    assert not upward["atr_upper_level"].equals(downward["atr_upper_level"])
    assert not upward["atr_lower_level"].equals(downward["atr_lower_level"])


def test_lower_channel_keeps_last_angle_on_straight_continuation():
    index = pd.date_range("2024-01-01", periods=5, freq="D")
    falling = _project_lower_channel(
        pd.Series([100.0, 90.0, 90.0, 90.0, 90.0], index=index), 1)
    assert (falling["lower_channel_slope"].iloc[1:] < 0).all()
    assert falling["lower_channel_line"].iloc[-1] < 90.0

    rising = _project_lower_channel(
        pd.Series([90.0, 100.0, 100.0, 100.0, 100.0], index=index), 1)
    assert (rising["lower_channel_slope"].iloc[1:] > 0).all()
    assert rising["lower_channel_line"].iloc[-1] > 100.0


def test_breakout_same_bar_is_conservatively_downside():
    prices = frame()
    prices.iloc[-2, prices.columns.get_loc("high")] *= 5
    prices.iloc[-2, prices.columns.get_loc("low")] *= 0.01
    result = build_regime_frame(prices)
    assert bool(result.iloc[-1]["breakout_ambiguous"])
    assert result.iloc[-1]["breakout_score"] == -1.0


def test_composite_helper_matches_selected_bull_phase():
    prices = frame()
    diagnostic = build_regime_frame(prices, {})
    pd.testing.assert_series_equal(
        composite_bull_regime(prices, {}),
        diagnostic["risk_on"].rename("composite_bull"))


def test_repository_timestamp_column_becomes_decision_index():
    prices = frame().reset_index(names="timestamp")
    result = build_regime_frame(prices)
    assert isinstance(result.index, pd.DatetimeIndex)
    assert result.index.tz is not None
    assert result.index[0] == prices.loc[0, "timestamp"]
