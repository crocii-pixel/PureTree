"""BTC regime helpers shared by live trading and backtests."""
from __future__ import annotations

from typing import Any, Dict

import pandas as pd


def is_period_rebalance(config: Dict[str, Any]) -> bool:
    return str(config.get("investment_strategy", "volatility_breakout")) == "period_rebalance"


def confirmed_bull_regime(close: pd.Series, short_window: int = 60,
                          long_window: int = 120, entry_days: int = 2,
                          exit_days: int = 1) -> pd.Series:
    """Decision-day regime using only closes completed before that day.

    Enter after ``entry_days`` consecutive closes above both averages.  Once in,
    leave after ``exit_days`` consecutive closes below the short average.
    """
    short_window = max(2, int(short_window))
    long_window = max(short_window + 1, int(long_window))
    entry_days = max(1, int(entry_days))
    exit_days = max(1, int(exit_days))
    completed = pd.to_numeric(close, errors="coerce").shift(1)
    short_ma = completed.rolling(short_window, min_periods=short_window).mean()
    long_ma = completed.rolling(long_window, min_periods=long_window).mean()
    above = (completed > short_ma) & (completed > long_ma)
    below_short = completed < short_ma
    state = False
    entry_count = exit_count = 0
    values = []
    for date in completed.index:
        if not state:
            entry_count = entry_count + 1 if bool(above.get(date, False)) else 0
            if entry_count >= entry_days:
                state = True
                exit_count = 0
        else:
            exit_count = exit_count + 1 if bool(below_short.get(date, False)) else 0
            if exit_count >= exit_days:
                state = False
                entry_count = 0
        values.append(state)
    return pd.Series(values, index=completed.index, dtype=bool, name="period_bull")


def regime_from_config(close: pd.Series, config: Dict[str, Any]) -> pd.Series:
    return confirmed_bull_regime(
        close,
        config.get("regime_short_ma", 60),
        config.get("regime_long_ma", 120),
        config.get("regime_entry_confirm_days", 2),
        config.get("regime_exit_confirm_days", 1),
    )


def current_regime_from_config(close: pd.Series,
                               config: Dict[str, Any]) -> bool:
    """Return the next-session state using the latest completed close."""
    values = pd.to_numeric(close, errors="coerce").dropna().sort_index()
    if values.empty:
        raise ValueError("현재 국면을 판정할 확정 종가가 없습니다.")
    if len(values.index) >= 2:
        deltas = values.index.to_series().diff().dropna()
        positive = deltas[deltas > pd.Timedelta(0)]
        step = positive.median() if not positive.empty else pd.Timedelta(days=1)
    else:
        step = pd.Timedelta(days=1)
    extended = pd.concat([
        values,
        pd.Series([values.iloc[-1]], index=[values.index[-1] + step]),
    ])
    return bool(regime_from_config(extended, config).iloc[-1])
