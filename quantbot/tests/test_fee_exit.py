from pathlib import Path

import pandas as pd

import reference_data
from fee_manager import normalize_fee_info, resolve_fee_info, save_fee_info
from tools.backtest_config import run_backtest


def test_fee_decimal_is_not_percent_again(tmp_path: Path):
    info = normalize_fee_info("bithumb", {
        "buy_rate": 0.0004, "sell_rate": 0.0004,
        "maker_rate": 0.0004, "taker_rate": 0.0004,
    })
    assert info["buy_rate"] == 0.0004
    assert info["buy_rate"] * 100 == 0.04


def test_saved_fee_snapshot_is_resolved(tmp_path: Path):
    path = tmp_path / "fees.json"
    save_fee_info({
        "exchange": "upbit", "buy_rate": 0.00045, "sell_rate": 0.00055,
        "maker_rate": 0.0004, "taker_rate": 0.00055,
        "source": "test_api", "checked_at": "2026-08-24T00:00:00+00:00",
    }, path)
    info = resolve_fee_info({"exchange": "upbit"}, path)
    assert info["buy_rate"] == 0.00045
    assert info["sell_rate"] == 0.00055
    assert info["source"] == "test_api"


def _frames():
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
    data["ma10"] = 100.0
    data["ma5"] = 100.0
    data.loc[index[30], "low"] = 90.0
    ctx = pd.DataFrame(index=index)
    ctx["bull"] = True
    ctx["explosive"] = False
    ctx["btc_broke"] = True
    ctx["btc_declining"] = False
    return {"BTC": data}, ctx


def test_intraday_exit_uses_daily_low_while_daily_mode_waits():
    data, ctx = _frames()
    base = {
        "exchange": "bithumb", "tickers": ["BTC"], "ma_window": 10,
        "bear_exit_ma_window": 5, "position_sizing": "equal",
        "backtest_slippage_rate": 0.001,
        "_fee_info": {"exchange": "bithumb", "buy_rate": 0.0004,
                      "sell_rate": 0.0004, "maker_rate": 0.0004,
                      "taker_rate": 0.0004, "source": "test"},
    }
    daily = run_backtest({**base, "exit_timing": "daily"}, data, ctx)
    immediate = run_backtest({**base, "exit_timing": "intraday"}, data, ctx)
    assert len(daily["_trades"]) == 1
    assert len(immediate["_trades"]) > len(daily["_trades"])
    assert immediate["fee_info"]["sell_rate"] == 0.0004
    assert immediate["slippage_rate"] == 0.001


def test_binance_daily_and_price_endpoints_stay_separate(monkeypatch):
    calls = []

    class Response:
        def __init__(self, url):
            self.url = url
        def raise_for_status(self):
            pass
        def json(self):
            if "ticker/price" in self.url:
                return {"price": "123.45"}
            return [[1_700_000_000_000, "100", "110", "90", "105", "1"]]

    def fake_get(url, **kwargs):
        calls.append(url)
        return Response(url)

    monkeypatch.setattr(reference_data.requests, "get", fake_get)
    assert reference_data.fetch_binance_price("BTC") == 123.45
    assert reference_data.fetch_binance_daily("BTC") is not None
    assert calls == [reference_data.PRICE_URL, reference_data.BASE_URL]
