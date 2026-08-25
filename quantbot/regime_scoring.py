"""Transparent, look-ahead-safe BTC market-regime classification.

The bullish and bearish detectors are deliberately independent.  A date is
``상승`` only when the bullish detector is active alone, ``하락`` only when the
bearish detector is active alone, and ``안정`` when both agree or neither has a
direction.  Every decision at ``t`` uses candles completed before ``t``.
"""
from __future__ import annotations

from copy import deepcopy
from typing import Any, Dict, Mapping, Optional

import numpy as np
import pandas as pd


DEFAULT_REGIME_SCORE_CONFIG: Dict[str, Any] = {
    "enabled": True,
    "use_for_backtest": False,
    # Live trading is deliberately a separate opt-in.  A chart/backtest
    # experiment must never change real orders merely because it was saved.
    "use_for_live": False,
    # Each side uses exactly one detector.  Legacy weighted settings are read
    # during migration but no longer participate in classification.
    "bull_detector": "dual_ma",
    "bear_detector": "lower_channel",
    "decision_interval": "1d",
    "short_ma": 60,
    "long_ma": 120,
    "macd_fast": 30,
    "macd_slow": 60,
    "macd_signal": 9,
    "atr_multiple": 1.0,
    "bull_atr_window": 10,
    "bear_atr_window": 2,
    "breakout_lower_window": 10,
    "channel_slope_bars": 3,
    # Backtest-only strategy routing.  Live trading remains a separate opt-in.
    "bull_strategy": "period_rebalance",
    "stable_strategy": "volatility_breakout",
    "bear_strategy": "defensive_atr",
    "defensive_atr_multiple": 2.0,
    "defensive_entry_method": "atr",
    "defensive_probe_fraction": 0.25,
    # ATR lower orders are rebuilt from the latest completed candle every day.
    # Filled probe units are managed separately from core/breakout holdings.
    "defensive_take_profit_pct": 0.05,
    "defensive_stop_atr_multiple": 2.0,
    "defensive_cancel_buffer_atr": 0.25,
}


BULL_DETECTOR_IDS = {
    "dual_ma", "log_macd", "volatility_breakout", "lower_channel"}
BEAR_DETECTOR_IDS = {
    "dual_ma", "log_macd", "volatility_decline", "lower_channel"}
DECISION_INTERVAL_IDS = {
    "1m", "15m", "30m", "1h", "2h", "3h", "4h", "1d", "1w", "1mo"}
STRATEGY_IDS = {
    "period_rebalance", "volatility_breakout", "defensive_atr",
    "cash_with_atr", "cash",
}


REGIME_COLORS = {
    "폭등": "#10B981",
    "상승": "#34D399",
    "안정": "#94A3B8",
    "전환": "#F59E0B",
    "하락": "#FB7185",
    "폭락": "#EF4444",
    "판정 준비": "#64748B",
}


def scoring_config(config: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    """Return a validated flat scoring configuration.

    QuantBot stores these values inside ``regime_scoring``.  Flat legacy keys
    with the ``regime_score_`` prefix are accepted for easier migrations.
    """
    source = dict(config or {})
    nested = source.get("regime_scoring")
    raw = dict(nested) if isinstance(nested, Mapping) else {}
    for key in DEFAULT_REGIME_SCORE_CONFIG:
        legacy = f"regime_score_{key}"
        if legacy in source and key not in raw:
            raw[key] = source[legacy]
    result = deepcopy(DEFAULT_REGIME_SCORE_CONFIG)
    result.update(raw)

    if "short_ma" not in raw:
        result["short_ma"] = int(source.get("regime_short_ma", 60))
    if "long_ma" not in raw:
        result["long_ma"] = int(source.get("regime_long_ma", 120))
    result["short_ma"] = max(2, int(result.get("short_ma", 60)))
    result["long_ma"] = max(
        result["short_ma"] + 1, int(result.get("long_ma", 120)))

    # Migrate an old weighted setup to the strongest enabled market component.
    if "bull_detector" not in raw or "bear_detector" not in raw:
        legacy = []
        for prefix, detector in (("haltu", "dual_ma"), ("macd", "log_macd"),
                                 ("breakout", "volatility_breakout")):
            if bool(raw.get(f"{prefix}_enabled", False)):
                legacy.append((float(raw.get(f"{prefix}_weight", 0.0) or 0.0), detector))
        if legacy:
            selected = max(legacy)[1]
            if "bull_detector" not in raw:
                result["bull_detector"] = selected
            if "bear_detector" not in raw:
                result["bear_detector"] = selected
    if result.get("bull_detector") not in BULL_DETECTOR_IDS:
        result["bull_detector"] = "dual_ma"
    # The old shared breakout detector represented both directions.  The two
    # sides now have explicit meanings, so preserve an old bearish selection
    # as the downside ATR detector.
    if result.get("bear_detector") == "volatility_breakout":
        result["bear_detector"] = "volatility_decline"
    if result.get("bear_detector") not in BEAR_DETECTOR_IDS:
        result["bear_detector"] = "lower_channel"
    if result.get("decision_interval") not in DECISION_INTERVAL_IDS:
        result["decision_interval"] = "1d"
    result["macd_fast"] = max(2, int(result.get("macd_fast", 30)))
    result["macd_slow"] = max(
        result["macd_fast"] + 1, int(result.get("macd_slow", 60)))
    result["macd_signal"] = max(2, int(result.get("macd_signal", 9)))
    # 배수는 하나만 두고 상승·하락을 **ATR 기간**으로 가릅니다.  짧은 기간은
    # 최근 변동성에 민감해 하락을 빨리 잡고, 긴 기간은 상승을 늦게·확실하게 잡습니다.
    result["atr_multiple"] = float(np.clip(
        float(result.get("atr_multiple", 1.0) or 1.0), 0.1, 20.0))
    result["bull_atr_window"] = max(2, int(result.get("bull_atr_window", 10)))
    result["bear_atr_window"] = max(2, int(result.get("bear_atr_window", 2)))
    result["breakout_lower_window"] = max(
        2, int(result.get("breakout_lower_window", 10)))
    result["channel_slope_bars"] = max(
        1, int(result.get("channel_slope_bars", 3)))
    for phase in ("bull", "stable", "bear"):
        key = f"{phase}_strategy"
        if result.get(key) not in STRATEGY_IDS:
            result[key] = DEFAULT_REGIME_SCORE_CONFIG[key]
    multiple = float(result.get("defensive_atr_multiple", 2.0) or 2.0)
    result["defensive_atr_multiple"] = min((2.0, 4.0, 6.0, 8.0),
                                              key=lambda value: abs(value - multiple))
    if result.get("defensive_entry_method") not in {"atr", "lower_channel"}:
        result["defensive_entry_method"] = "atr"
    result["defensive_probe_fraction"] = float(np.clip(
        float(result.get("defensive_probe_fraction", 0.25) or 0.25), 0.01, 1.0))
    result["defensive_take_profit_pct"] = float(np.clip(
        float(result.get("defensive_take_profit_pct", 0.05) or 0.05), 0.001, 1.0))
    result["defensive_stop_atr_multiple"] = float(np.clip(
        float(result.get("defensive_stop_atr_multiple", 2.0) or 2.0), 0.1, 20.0))
    cancel_buffer = result.get("defensive_cancel_buffer_atr", 0.25)
    result["defensive_cancel_buffer_atr"] = float(np.clip(
        float(0.25 if cancel_buffer is None else cancel_buffer), 0.0, 10.0))
    return result


def validate_scoring_config(config: Optional[Mapping[str, Any]] = None,
                            strategy: Optional[bool] = None) -> list[str]:
    """Validate raw values without silently normalising contradictory inputs."""
    source = dict(config or {})
    nested = source.get("regime_scoring")
    raw = dict(nested) if isinstance(nested, Mapping) else source
    merged = deepcopy(DEFAULT_REGIME_SCORE_CONFIG)
    merged.update(raw)
    errors: list[str] = []
    try:
        if int(merged["long_ma"]) <= int(merged["short_ma"]):
            errors.append("장기 MA는 단기 MA보다 커야 합니다.")
        if int(merged["macd_slow"]) <= int(merged["macd_fast"]):
            errors.append("MACD 느림 기간은 빠름 기간보다 커야 합니다.")
        if int(merged["breakout_lower_window"]) < 2:
            errors.append("하방 채널 기간은 2봉 이상이어야 합니다.")
        if int(merged.get("bull_atr_window", 10)) < 2:
            errors.append("상승 ATR 기간은 2봉 이상이어야 합니다.")
        if int(merged.get("bear_atr_window", 2)) < 2:
            errors.append("하락 ATR 기간은 2봉 이상이어야 합니다.")
        if float(merged.get("atr_multiple", 1.0)) <= 0:
            errors.append("ATR 배수는 0보다 커야 합니다.")
        if float(merged["defensive_take_profit_pct"]) <= 0:
            errors.append("예약 체결분 익절률은 0보다 커야 합니다.")
        if float(merged["defensive_stop_atr_multiple"]) <= 0:
            errors.append("예약 체결분 ATR 손절 배수는 0보다 커야 합니다.")
        if float(merged["defensive_cancel_buffer_atr"]) < 0:
            errors.append("돌파 접근 예약취소 거리는 0 이상이어야 합니다.")
    except (KeyError, TypeError, ValueError):
        errors.append("국면 판정 숫자 설정을 확인해 주세요.")

    if str(merged.get("bull_detector", "")) not in BULL_DETECTOR_IDS:
        errors.append("상승 판정 방식을 선택해 주세요.")
    if str(merged.get("bear_detector", "")) not in BEAR_DETECTOR_IDS:
        errors.append("하락 판정 방식을 선택해 주세요.")
    if str(merged.get("decision_interval", "")) not in DECISION_INTERVAL_IDS:
        errors.append("장세 판정 시간 간격을 선택해 주세요.")
    if str(merged.get("defensive_entry_method", "atr")) not in {
            "atr", "lower_channel"}:
        errors.append("예약매수 방식을 선택해 주세요.")
    for phase, label in (("bull", "상승기"), ("stable", "안정기"),
                         ("bear", "하락기")):
        if str(merged.get(f"{phase}_strategy", "")) not in STRATEGY_IDS:
            errors.append(f"{label} 전략을 선택해 주세요.")

    applying = bool(merged.get("use_for_backtest", False)) if strategy is None else bool(strategy)
    if applying and not bool(merged.get("enabled", True)):
        errors.append("국면 판정을 켜야 백테스트 전략에 적용할 수 있습니다.")
    if bool(merged.get("use_for_live", False)):
        errors.append("3국면 전략 라우팅은 현재 백테스트 전용입니다.")
    return errors


def _numeric_ohlc(frame: pd.DataFrame) -> pd.DataFrame:
    required = ("open", "high", "low", "close")
    missing = [name for name in required if name not in frame]
    if missing:
        raise ValueError(f"국면 판정 OHLC 열이 없습니다: {', '.join(missing)}")
    out = frame.loc[:, required].copy()
    if "timestamp" in frame.columns:
        timestamps = pd.to_datetime(frame["timestamp"], utc=True, errors="coerce")
        out.index = pd.DatetimeIndex(timestamps)
    for name in required:
        out[name] = pd.to_numeric(out[name], errors="coerce")
    out = out.sort_index()
    out = out[~out.index.duplicated(keep="last")]
    return out


def _haltu_component(close: pd.Series, short_window: int,
                     long_window: int) -> pd.DataFrame:
    # The decision at date t sees the close completed at t-1.
    completed = close.shift(1)
    short_ma = completed.rolling(short_window, min_periods=short_window).mean()
    long_ma = completed.rolling(long_window, min_periods=long_window).mean()
    short_rising = short_ma > short_ma.shift(1)
    long_rising = long_ma > long_ma.shift(1)
    gate_a = (completed >= long_ma) | long_rising
    gate_b = (completed >= short_ma) | short_rising
    ready = long_ma.notna()

    score = pd.Series(0.0, index=close.index)
    score.loc[ready & gate_a & gate_b] = 1.0
    # A is false only while price is below the long MA *and* that MA is not
    # rising.  Do not turn every below-MA close into a hard exit: that would
    # erase the published ``price above OR MA rising`` branch entirely.
    hard_exit = ready & ~gate_a
    score.loc[hard_exit] = -1.0
    score.loc[~ready] = np.nan
    return pd.DataFrame({
        "haltu_score": score,
        "decision_close": completed,
        "ma_short": short_ma,
        "ma_long": long_ma,
        "haltu_gate_a": gate_a.where(ready),
        "haltu_gate_b": gate_b.where(ready),
        "haltu_hard_exit": hard_exit.where(ready),
    })


def _log_macd_component(close: pd.Series, fast: int, slow: int,
                        signal_window: int) -> pd.DataFrame:
    log_close = np.log(close.where(close > 0)).shift(1)
    fast_ema = log_close.ewm(span=fast, adjust=False, min_periods=fast).mean()
    slow_ema = log_close.ewm(span=slow, adjust=False, min_periods=slow).mean()
    macd = fast_ema - slow_ema
    signal = macd.ewm(
        span=signal_window, adjust=False, min_periods=signal_window).mean()
    histogram = macd - signal
    ready = signal.notna()
    state = pd.Series(0.0, index=close.index, dtype=float)
    state.loc[ready & (macd > 0) & (macd > signal)] = 1.0
    state.loc[ready & (macd < 0) & (macd < signal)] = -1.0
    state = state.where(ready)
    return pd.DataFrame({
        "log_macd_score": state,
        "log_macd": macd,
        "log_macd_signal": signal,
        "log_macd_histogram": histogram,
    })


def _breakout_component(frame: pd.DataFrame, bull_atr_window: int,
                        bear_atr_window: int, lower_window: int,
                        atr_multiple: float) -> pd.DataFrame:
    candle_range = (frame["high"] - frame["low"]).replace(0, np.nan)
    noise = 1.0 - (frame["close"] - frame["open"]).abs() / candle_range
    dynamic_k = noise.shift(1).rolling(20, min_periods=20).mean().fillna(0.5)
    previous_range = candle_range.shift(1)
    target = frame["open"] + previous_range * dynamic_k

    previous_close = frame["close"].shift(1)
    true_range = pd.concat([
        frame["high"] - frame["low"],
        (frame["high"] - previous_close).abs(),
        (frame["low"] - previous_close).abs(),
    ], axis=1).max(axis=1)
    bull_atr = true_range.rolling(
        bull_atr_window, min_periods=bull_atr_window).mean().shift(1)
    bear_atr = true_range.rolling(
        bear_atr_window, min_periods=bear_atr_window).mean().shift(1)
    lower_window = max(2, int(lower_window))
    exit_level = frame["low"].shift(1).rolling(
        lower_window, min_periods=lower_window).min()
    # Directional ATR levels are separate.  Both are known at the bar open:
    # the anchor is the previous close and ATR contains completed bars only.
    atr_upper_level = previous_close + bull_atr * float(atr_multiple)
    atr_lower_level = previous_close - bear_atr * float(atr_multiple)
    upper_event = frame["high"] >= atr_upper_level
    lower_event = frame["low"] <= atr_lower_level
    # A daily bar cannot reveal intrabar ordering; downside wins conservatively.
    event = pd.Series(np.nan, index=frame.index, dtype=float)
    ready = bull_atr.notna() & bear_atr.notna() & target.notna() & exit_level.notna()
    event.loc[ready] = 0.0
    event.loc[ready & upper_event] = 1.0
    event.loc[ready & lower_event] = -1.0
    state = []
    current = 0.0
    for value in event.shift(1):
        if pd.notna(value) and float(value) != 0.0:
            current = float(value)
        state.append(current if pd.notna(value) else np.nan)
    score = pd.Series(state, index=frame.index, dtype=float)
    return pd.DataFrame({
        "breakout_score": score,
        "dynamic_k": dynamic_k,
        "buy_target": target,
        "exit_level": exit_level,
        "atr_upper_level": atr_upper_level,
        "atr_lower_level": atr_lower_level,
        "breakout_upper_event": upper_event.shift(1).where(ready.shift(1)),
        "breakout_lower_event": lower_event.shift(1).where(ready.shift(1)),
        "breakout_ambiguous": (upper_event & lower_event).shift(1).where(ready.shift(1)),
        "atr": bull_atr,
        "bull_atr": bull_atr,
        "bear_atr": bear_atr,
    })


def _detector_score(parts: pd.DataFrame, detector: str,
                    slope_bars: int) -> pd.Series:
    """Return a causal direction in [-1, 1] for one selected detector."""
    if detector == "dual_ma":
        return pd.to_numeric(parts["haltu_score"], errors="coerce")
    if detector == "log_macd":
        return pd.to_numeric(parts["log_macd_score"], errors="coerce")
    if detector == "volatility_breakout":
        return pd.to_numeric(parts["breakout_score"], errors="coerce")
    if detector == "volatility_decline":
        return pd.to_numeric(parts["breakout_score"], errors="coerce")

    # The lower boundary joins causal rolling-low pivots.  Its last non-zero
    # angle remains in force along the straight continuation until a new pivot
    # changes that angle.  This makes a rising/falling channel persist instead
    # of becoming neutral merely because the rolling low is temporarily flat.
    lower = pd.to_numeric(parts["lower_channel_line"], errors="coerce").where(
        pd.to_numeric(parts["lower_channel_line"], errors="coerce") > 0)
    slope = pd.to_numeric(parts["lower_channel_slope"], errors="coerce")
    score = pd.Series(0.0, index=parts.index, dtype=float)
    completed = pd.to_numeric(parts["decision_close"], errors="coerce")
    ready = lower.notna() & slope.notna() & completed.notna()
    score.loc[ready & (slope > 0) & (completed >= lower)] = 1.0
    score.loc[ready & ((slope < 0) | (completed < lower))] = -1.0
    return score.where(ready)


def _project_lower_channel(raw_lower: pd.Series,
                           slope_bars: int) -> pd.DataFrame:
    """Join each new lower pivot and extend its last angle causally."""
    raw = pd.to_numeric(raw_lower, errors="coerce").where(
        pd.to_numeric(raw_lower, errors="coerce") > 0)
    bars = max(1, int(slope_bars))
    measured = np.log(raw).diff(bars) / bars
    changed = raw.ne(raw.shift(1)) & raw.notna() & raw.shift(1).notna()
    measured = measured.where(changed & measured.ne(0.0))
    continuing_slope = measured.ffill()
    projected = pd.Series(np.nan, index=raw.index, dtype=float)
    last_value = np.nan
    last_slope = np.nan
    previous_raw = np.nan
    for stamp, value in raw.items():
        if pd.isna(value):
            continue
        slope_value = continuing_slope.loc[stamp]
        pivot = pd.isna(previous_raw) or not np.isclose(float(value), float(previous_raw))
        if pivot or not np.isfinite(last_value):
            last_value = float(value)
        elif pd.notna(last_slope):
            last_value = float(last_value) * float(np.exp(last_slope))
        if pd.notna(slope_value):
            last_slope = float(slope_value)
        projected.loc[stamp] = last_value
        previous_raw = float(value)
    return pd.DataFrame({
        "lower_channel_line": projected,
        "lower_channel_slope": continuing_slope,
    })


def _classify_independent(bull_score: pd.Series,
                          bear_score: pd.Series) -> pd.DataFrame:
    """Classify without weights or arbitrary score thresholds."""
    ready = bull_score.notna() & bear_score.notna()
    bull_active = ready & (bull_score > 0)
    bear_active = ready & (bear_score < 0)
    bull_signal = bull_active.astype("boolean").where(ready)
    bear_signal = bear_active.astype("boolean").where(ready)
    labels = pd.Series("판정 준비", index=bull_score.index, dtype=object)
    labels.loc[ready] = "안정"
    labels.loc[bull_active & ~bear_active] = "상승"
    labels.loc[bear_active & ~bull_active] = "하락"
    direction = pd.Series(np.nan, index=bull_score.index, dtype=float)
    direction.loc[ready] = (
        bull_active.loc[ready].astype(float)
        - bear_active.loc[ready].astype(float)) * 100.0
    return pd.DataFrame({
        "bull_signal": bull_signal,
        "bear_signal": bear_signal,
        "direction_score": direction,
        "coverage": ready.astype(float) * 100.0,
        "breadth": np.where(ready, np.where(
            bull_active == bear_active, 0.0, 100.0), np.nan),
        "regime": labels,
    }, index=bull_score.index)


def build_regime_frame(frame: pd.DataFrame,
                       config: Optional[Mapping[str, Any]] = None) -> pd.DataFrame:
    """Calculate transparent regime diagnostics for every decision date."""
    prices = _numeric_ohlc(frame)
    cfg = scoring_config(config)
    short_window = int(cfg["short_ma"])
    long_window = int(cfg["long_ma"])
    haltu = _haltu_component(prices["close"], short_window, long_window)
    macd = _log_macd_component(
        prices["close"], cfg["macd_fast"], cfg["macd_slow"], cfg["macd_signal"])
    breakout = _breakout_component(
        prices, cfg["bull_atr_window"], cfg["bear_atr_window"],
        cfg["breakout_lower_window"], cfg["atr_multiple"])
    out = prices.join(haltu).join(macd).join(breakout)
    channel = _project_lower_channel(
        out["exit_level"], int(cfg["channel_slope_bars"]))
    out = out.join(channel)
    out["lower_channel_score"] = _detector_score(
        out, "lower_channel", int(cfg["channel_slope_bars"]))

    if bool(cfg.get("enabled", True)):
        bull_score = _detector_score(
            out, str(cfg["bull_detector"]), int(cfg["channel_slope_bars"]))
        bear_score = _detector_score(
            out, str(cfg["bear_detector"]), int(cfg["channel_slope_bars"]))
        classified = _classify_independent(bull_score, bear_score)
    else:
        empty = pd.Series(np.nan, index=out.index, dtype=float)
        classified = _classify_independent(empty, empty)
    out["bull_detector_score"] = bull_score if bool(
        cfg.get("enabled", True)) else np.nan
    out["bear_detector_score"] = bear_score if bool(
        cfg.get("enabled", True)) else np.nan
    out = out.join(classified)
    out["direction_raw"] = out["direction_score"]
    out["expansion_score"] = (
        out["atr"] / out["decision_close"].where(out["decision_close"] > 0)
        * 100.0).clip(lower=0.0)
    out["regime_raw"] = out["regime"]
    # Liquidation is a strategy-transition policy, not a property of every
    # bearish bar.  The backtest router decides when a phase change requires it.
    out["hard_exit"] = False
    out["regime_color"] = out["regime"].map(REGIME_COLORS)
    out["risk_on"] = out["regime"].eq("상승")
    return out


def current_regime_decision(frame: pd.DataFrame,
                            config: Optional[Mapping[str, Any]] = None) -> pd.Series:
    """Evaluate the next decision using the latest completed candle.

    ``build_regime_frame`` deliberately shifts one candle because a decision
    made at a candle's open may only use the previously completed candle.  A
    repository contains no row for the next open yet, so selecting its last
    diagnostic row would add an accidental extra interval of delay.  Append a
    harmless placeholder row and return that next, still-causal decision.
    """
    prices = _numeric_ohlc(frame)
    if prices.empty:
        raise ValueError("현재 장세를 판정할 확정봉이 없습니다.")
    if len(prices.index) >= 2:
        deltas = prices.index.to_series().diff().dropna()
        positive = deltas[deltas > pd.Timedelta(0)]
        step = positive.median() if not positive.empty else pd.Timedelta(days=1)
    else:
        step = pd.Timedelta(days=1)
    next_index = prices.index[-1] + step
    placeholder = prices.iloc[[-1]].copy()
    placeholder.index = pd.DatetimeIndex([next_index])
    extended = pd.concat([prices, placeholder])
    return build_regime_frame(extended, config).iloc[-1]


def composite_bull_regime(frame: pd.DataFrame,
                          config: Optional[Mapping[str, Any]] = None) -> pd.Series:
    """Boolean strategy gate from the selected independent detectors."""
    diagnostic = build_regime_frame(frame, config)
    return diagnostic["risk_on"].rename("composite_bull")
