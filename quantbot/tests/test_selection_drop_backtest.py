import pandas as pd

from tools.backtest_config import run_backtest


def _inputs():
    index = pd.date_range("2024-01-01", periods=50, freq="D")
    data = pd.DataFrame(index=index)
    data["open"] = 100.0
    data["high"] = 110.0
    data["low"] = 99.0
    data["close"] = 108.0
    data["target"] = 105.0
    data["N"] = 5.0
    data["above_ma10"] = True
    data["above_ma5"] = True
    data["ma10"] = 90.0
    data["ma5"] = 90.0
    data["auto_selected"] = True
    data.loc[index[30]:, "auto_selected"] = False

    ctx = pd.DataFrame(index=index)
    ctx["bull"] = True
    ctx["explosive"] = False
    ctx["btc_broke"] = True
    ctx["btc_declining"] = False
    return {"ALT": data}, ctx, index


def _config(exit_on_drop):
    return {
        "exchange": "bithumb",
        "fixed_selection_enabled": False,
        "fixed_tickers": [],
        "additional_selection_enabled": True,
        "additional_selection_mode": "auto",
        "auto_selection_count": 1,
        "tickers": [],
        "ma_window": 10,
        "bear_exit_ma_window": 5,
        "position_sizing": "equal",
        "exit_on_selection_drop": exit_on_drop,
        "backtest_slippage_rate": 0.001,
        "_fee_info": {
            "exchange": "bithumb", "buy_rate": 0.0004,
            "sell_rate": 0.0004, "maker_rate": 0.0004,
            "taker_rate": 0.0004, "source": "test",
        },
    }


def test_drop_option_exits_at_first_deselected_session_open():
    data, ctx, index = _inputs()
    result = run_backtest(_config(True), data, ctx)

    drop_trade = next(
        trade for trade in result["_trades"]
        if trade.get("reason") == "selection_drop")
    assert drop_trade["date"] == index[30]
    assert result["selection_drop_exits"] == 1


def test_drop_option_off_keeps_position_until_normal_exit_or_end():
    data, ctx, _ = _inputs()
    result = run_backtest(_config(False), data, ctx)

    assert result["selection_drop_exits"] == 0
    assert all(trade.get("reason") != "selection_drop"
               for trade in result["_trades"])
