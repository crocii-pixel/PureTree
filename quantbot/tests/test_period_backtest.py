import numpy as np
import pandas as pd
import pytest

from tools.backtest_period import run_period_backtest


def market_frame(scale=1.0):
    index = pd.date_range("2020-01-01", periods=180, freq="D")
    close = np.linspace(100.0, 300.0, len(index)) * scale
    return pd.DataFrame({
        "open": close * 0.995,
        "high": close * 1.02,
        "low": close * 0.98,
        "close": close,
        "N": close * 0.02,
        "target": close * 0.99,
        "above_ma10": True,
        "above_ma3": True,
        "auto_selected": True,
    }, index=index)


def test_period_backtest_runs_with_strategy_metadata():
    data = {"BTC": market_frame(), "ETH": market_frame(0.5)}
    ctx = pd.DataFrame({"explosive": False, "bull": True}, index=data["BTC"].index)
    result = run_period_backtest({
        "exchange": "bithumb",
        "investment_strategy": "period_rebalance",
        "regime_short_ma": 10,
        "regime_long_ma": 20,
        "regime_entry_confirm_days": 2,
        "regime_exit_confirm_days": 1,
        "ma_window": 10,
        "bear_exit_ma_window": 3,
        "risk_per_trade": 0.01,
        "atr_stop_multiple": 2.0,
        "_fee_info": {"buy_rate": 0.0004, "sell_rate": 0.0004},
        "backtest_slippage_rate": 0.0005,
    }, data, ctx)
    assert result["investment_strategy"] == "period_rebalance"
    assert result["최종자산"] > 0
    assert result["매수주문"] > 0
    assert result["regime_switches"] >= 0


def test_period_regime_uses_global_context_when_available():
    data = {"BTC": market_frame(), "ETH": market_frame(0.5)}
    index = data["BTC"].index
    ctx = pd.DataFrame({
        "explosive": False,
        "bull": True,
        "regime_close": np.linspace(100.0, 500.0, len(index)),
    }, index=index)
    result = run_period_backtest({
        "exchange": "bithumb",
        "investment_strategy": "period_rebalance",
        "regime_short_ma": 10,
        "regime_long_ma": 20,
        "regime_entry_confirm_days": 2,
        "regime_exit_confirm_days": 1,
        "ma_window": 10,
        "bear_exit_ma_window": 3,
        "risk_per_trade": 0.01,
        "atr_stop_multiple": 2.0,
        "_fee_info": {"buy_rate": 0.0004, "sell_rate": 0.0004},
        "backtest_slippage_rate": 0.0005,
    }, data, ctx)
    assert result["regime_source"] == "global_btc_usd"
    assert result["매수주문"] > 0


def test_composite_regime_is_explicit_opt_in():
    data = {"BTC": market_frame(), "ETH": market_frame(0.5)}
    index = data["BTC"].index
    ctx = pd.DataFrame({
        "explosive": False,
        "bull": True,
        "regime_close": np.linspace(500.0, 100.0, len(index)),
        "regime_label": "상승",
    }, index=index)
    result = run_period_backtest({
        "exchange": "bithumb",
        "investment_strategy": "period_rebalance",
        "regime_scoring": {"use_for_backtest": True},
        "regime_short_ma": 10,
        "regime_long_ma": 20,
        "ma_window": 10,
        "bear_exit_ma_window": 3,
        "risk_per_trade": 0.01,
        "atr_stop_multiple": 2.0,
        "_fee_info": {"buy_rate": 0.0004, "sell_rate": 0.0004},
        "backtest_slippage_rate": 0.0005,
    }, data, ctx)
    assert result["regime_engine"] == "independent_detectors"
    assert result["investment_strategy"] == "regime_routed"
    assert result["매수주문"] > 0


def test_composite_opt_in_fails_when_diagnostic_columns_are_missing():
    data = {"BTC": market_frame(), "ETH": market_frame(0.5)}
    ctx = pd.DataFrame({
        "explosive": False, "bull": True,
        "regime_close": np.linspace(100.0, 500.0, len(data["BTC"])),
    }, index=data["BTC"].index)
    with pytest.raises(RuntimeError, match="장세 판정 백테스트 입력"):
        run_period_backtest({
            "investment_strategy": "period_rebalance",
            "regime_scoring": {"use_for_backtest": True},
        }, data, ctx)


def test_same_strategy_across_phases_does_not_force_liquidation():
    data = {"BTC": market_frame(), "ETH": market_frame(0.5)}
    index = data["BTC"].index
    labels = pd.Series("상승", index=index)
    labels.iloc[60:120] = "안정"
    labels.iloc[120:] = "하락"
    ctx = pd.DataFrame({
        "explosive": False, "bull": True, "regime_label": labels,
    }, index=index)
    config = {
        "exchange": "bithumb", "investment_strategy": "period_rebalance",
        "regime_scoring": {
            "use_for_backtest": True,
            "bull_strategy": "volatility_breakout",
            "stable_strategy": "volatility_breakout",
            "bear_strategy": "volatility_breakout",
        },
        "ma_window": 10, "bear_exit_ma_window": 3,
        "risk_per_trade": 0.01, "atr_stop_multiple": 2.0,
        "_fee_info": {"buy_rate": 0.0004, "sell_rate": 0.0004},
    }
    result = run_period_backtest(config, data, ctx)
    assert result["regime_switches"] == 0
    assert not any(t["reason"] == "strategy_switch" for t in result["_trades"])


def test_atr_lower_multiple_changes_defensive_fills():
    base = market_frame()
    base["low"] = base["open"] - base["N"] * 3.0
    data = {"BTC": base.copy(), "ETH": base * 0.5}
    for ticker, raw in data.items():
        raw["auto_selected"] = True
        raw["above_ma10"] = True
        raw["above_ma3"] = True
        raw["target"] = raw["open"] + raw["N"] * 20.0  # disable K breakout
    index = base.index
    ctx = pd.DataFrame({
        "explosive": False, "bull": False, "regime_label": "하락",
    }, index=index)

    def run(multiple):
        return run_period_backtest({
            "exchange": "bithumb", "investment_strategy": "period_rebalance",
            "regime_scoring": {
                "use_for_backtest": True,
                "bear_strategy": "defensive_atr",
                "defensive_atr_multiple": multiple,
                "defensive_probe_fraction": 0.25,
            },
            "ma_window": 10, "bear_exit_ma_window": 3,
            "risk_per_trade": 0.01, "atr_stop_multiple": 2.0,
            "_fee_info": {"buy_rate": 0.0004, "sell_rate": 0.0004},
        }, data, ctx)

    near, far = run(2.0), run(8.0)
    assert near["atr_lower_buys"] > 0
    assert far["atr_lower_buys"] == 0


def test_rebalance_values_position_when_ticker_has_no_candle_that_day():
    btc = market_frame()
    eth = market_frame(0.5).drop(index=market_frame().index[19:24])
    data = {"BTC": btc, "ETH": eth}
    ctx = pd.DataFrame({"explosive": False, "bull": True}, index=btc.index)
    result = run_period_backtest({
        "exchange": "bithumb", "investment_strategy": "period_rebalance",
        "regime_short_ma": 5, "regime_long_ma": 10,
        "ma_window": 10, "bear_exit_ma_window": 3,
        "risk_per_trade": 0.01, "atr_stop_multiple": 2.0,
        "_fee_info": {"buy_rate": 0.0004, "sell_rate": 0.0004},
    }, data, ctx)
    assert result["최종자산"] > 0


def test_routed_rebalance_respects_compounding_cap():
    data = {"BTC": market_frame(), "ETH": market_frame(0.5)}
    index = data["BTC"].index
    ctx = pd.DataFrame({
        "explosive": False, "bull": True, "regime_label": "상승",
    }, index=index)
    base = {
        "exchange": "bithumb", "investment_strategy": "period_rebalance",
        "regime_scoring": {"use_for_backtest": True},
        "ma_window": 10, "bear_exit_ma_window": 3,
        "risk_per_trade": 0.01, "atr_stop_multiple": 2.0,
        "_fee_info": {"buy_rate": 0.0004, "sell_rate": 0.0004},
    }
    unlimited = run_period_backtest(base, data, ctx)
    capped = run_period_backtest(
        {**base, "sizing_equity_cap_krw": 1_000_000.0}, data, ctx)
    assert capped["최종자산"] < unlimited["최종자산"]
    assert capped["최종자산"] > 9_000_000


def test_routed_rebalance_applies_btc_minimum_weight():
    btc = market_frame()
    btc[["open", "high", "low", "close", "target"]] = 100.0
    data = {"BTC": btc, **{
        ticker: market_frame(0.2 + offset * 0.05)
        for offset, ticker in enumerate(("ETH", "SOL", "XRP", "ADA", "LINK"))}}
    index = btc.index
    ctx = pd.DataFrame({
        "explosive": False, "bull": True, "regime_label": "상승",
    }, index=index)
    base = {
        "exchange": "bithumb", "investment_strategy": "period_rebalance",
        "regime_scoring": {"use_for_backtest": True},
        "ma_window": 10, "bear_exit_ma_window": 3,
        "_fee_info": {"buy_rate": 0.0, "sell_rate": 0.0},
    }
    equal = run_period_backtest({**base, "btc_min_weight": 0.0}, data, ctx)
    weighted = run_period_backtest({**base, "btc_min_weight": 0.5}, data, ctx)
    assert weighted["최종자산"] < equal["최종자산"]


def test_routed_exit_respects_selection_drop_checkbox():
    frame = market_frame()
    frame.loc[frame.index[60]:, "auto_selected"] = False
    data = {"BTC": frame}
    ctx = pd.DataFrame({
        "explosive": False, "bull": True, "regime_label": "안정",
    }, index=frame.index)
    base = {
        "exchange": "bithumb", "investment_strategy": "period_rebalance",
        "additional_selection_enabled": True,
        "additional_selection_mode": "auto",
        "regime_scoring": {"use_for_backtest": True},
        "ma_window": 10, "bear_exit_ma_window": 3,
        "risk_per_trade": 0.01, "atr_stop_multiple": 2.0,
        "_fee_info": {"buy_rate": 0.0, "sell_rate": 0.0},
    }
    enabled = run_period_backtest(
        {**base, "exit_on_selection_drop": True}, data, ctx)
    disabled = run_period_backtest(
        {**base, "exit_on_selection_drop": False}, data, ctx)
    assert enabled["selection_drop_exits"] > 0
    assert disabled["selection_drop_exits"] == 0


def test_routed_exit_respects_intraday_ma_timing():
    frame = market_frame()
    frame["ma10"] = frame["open"] * 0.99
    data = {"BTC": frame}
    ctx = pd.DataFrame({
        "explosive": False, "bull": True, "regime_label": "안정",
    }, index=frame.index)
    base = {
        "exchange": "bithumb", "investment_strategy": "period_rebalance",
        "regime_scoring": {"use_for_backtest": True},
        "ma_window": 10, "bear_exit_ma_window": 3,
        "risk_per_trade": 0.01, "atr_stop_multiple": 2.0,
        "_fee_info": {"buy_rate": 0.0, "sell_rate": 0.0},
    }
    daily = run_period_backtest({**base, "exit_timing": "daily"}, data, ctx)
    intraday = run_period_backtest({**base, "exit_timing": "intraday"}, data, ctx)
    assert not any(t["reason"].startswith("ma_intraday") for t in daily["_trades"])
    assert any(t["reason"].startswith("ma_intraday") for t in intraday["_trades"])
