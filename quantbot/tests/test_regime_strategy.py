import pandas as pd

from regime_strategy import (confirmed_bull_regime, current_regime_from_config,
                             is_period_rebalance)


def series(values):
    return pd.Series(values, dtype=float,
                     index=pd.date_range("2024-01-01", periods=len(values)))


def test_period_strategy_is_opt_in():
    assert not is_period_rebalance({})
    assert not is_period_rebalance({"investment_strategy": "volatility_breakout"})
    assert is_period_rebalance({"investment_strategy": "period_rebalance"})


def test_entry_requires_two_completed_bull_closes():
    regime = confirmed_bull_regime(
        series([1, 2, 3, 4, 5, 6]), short_window=2, long_window=3,
        entry_days=2, exit_days=1)
    assert not bool(regime.iloc[3])
    assert bool(regime.iloc[4])


def test_exit_uses_one_completed_close_below_short_ma():
    regime = confirmed_bull_regime(
        series([1, 2, 3, 4, 5, 1, 1]), short_window=2, long_window=3,
        entry_days=2, exit_days=1)
    assert bool(regime.iloc[5])
    assert not bool(regime.iloc[6])


def test_decision_never_uses_current_day_close():
    original = series([1, 2, 3, 4, 5, 6])
    shocked = original.copy()
    shocked.iloc[-1] = 0.01
    a = confirmed_bull_regime(original, 2, 3, 2, 1)
    b = confirmed_bull_regime(shocked, 2, 3, 2, 1)
    pd.testing.assert_series_equal(a, b)


def test_current_regime_uses_latest_completed_close_without_extra_day_delay():
    close = series([1, 2, 3, 4, 5, 1])
    config = {
        "regime_short_ma": 2, "regime_long_ma": 3,
        "regime_entry_confirm_days": 2, "regime_exit_confirm_days": 1,
    }
    historical = confirmed_bull_regime(close, 2, 3, 2, 1)
    assert bool(historical.iloc[-1])
    assert not current_regime_from_config(close, config)
